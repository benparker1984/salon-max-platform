"""User-service supervisor for signed native till releases. No database migrations."""
import argparse
import base64
import fcntl
import hashlib
import io
import json
import os
import py_compile
import re
import sqlite3
import stat
import subprocess
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ROOT = Path.home() / ".local/share/salonmax-updater"
SHARED = Path.home() / "Salon-Max"
DATABASE = Path.home() / "till-build/till_backoffice.db"
MAX_ARCHIVE = 20 * 1024 * 1024


def write_json(path, data):
    temp = path.with_suffix(".next")
    temp.write_text(json.dumps(data, sort_keys=True))
    temp.replace(path)


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def verify_release(document, public_key):
    release = document["release"]
    serialized = json.dumps(release, sort_keys=True, separators=(",", ":")).encode()
    Ed25519PublicKey.from_public_bytes(public_key).verify(base64.b64decode(document["signature"], validate=True), serialized)
    if (not re.fullmatch(r"[0-9][0-9A-Za-z._-]{0,63}", release["version"])
            or release["schema"] != 1 or release["channel"] != "stable"
            or not re.fullmatch(r"[a-f0-9]{64}", release["sha256"])):
        raise ValueError("Unsupported release manifest")
    return release


def unpack(archive, release, destination):
    if len(archive) > MAX_ARCHIVE or hashlib.sha256(archive).hexdigest() != release["sha256"]:
        raise ValueError("Release checksum mismatch")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        names = bundle.namelist()
        if len(names) != len(set(names)) or len(names) > 100:
            raise ValueError("Invalid archive entries")
        if sum(item.file_size for item in bundle.infolist()) > MAX_ARCHIVE:
            raise ValueError("Release too large")
        for item in bundle.infolist():
            name = Path(item.filename)
            mode = item.external_attr >> 16
            if (name.is_absolute() or ".." in name.parts or "\\" in item.filename
                    or stat.S_ISLNK(mode) or item.is_dir()
                    or not (item.filename == "run_native_till.py"
                            or re.fullmatch(r"native_till_app/[a-z_]+\.py", item.filename))):
                raise ValueError("Unsupported release file")
        if not {"run_native_till.py", "native_till_app/app.py", "native_till_app/update_runtime.py"}.issubset(names):
            raise ValueError("Incomplete till release")
        destination.mkdir(parents=True, exist_ok=True)
        for item in bundle.infolist():
            target = destination / item.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bundle.read(item))
            py_compile.compile(str(target), doraise=True)
    (destination / "VERSION").write_text(release["version"])


def connection_details():
    with sqlite3.connect(DATABASE, timeout=10) as db:
        db.row_factory = sqlite3.Row
        settings = db.execute("select sync_api_base_url,sync_stub_enabled from business_settings where id=1").fetchone()
        identity = db.execute("select * from local_device_identity order by id desc limit 1").fetchone()
        licence = db.execute("select signed_token from local_licence_lease order by id desc limit 1").fetchone()
    if not settings or settings["sync_stub_enabled"] or not identity or not licence:
        raise RuntimeError("Till is not paired for hosted updates")
    base = settings["sync_api_base_url"].rstrip("/")
    if base != "https://www.salonmax.co.uk/v1":
        raise RuntimeError("Untrusted update API")
    return base, {"Authorization": "Bearer " + licence["signed_token"],
                  "X-SalonMax-Business-Id": identity["business_account_public_id"],
                  "X-SalonMax-Device-Id": identity["terminal_device_public_id"],
                  "Content-Type": "application/json"}


def api(path, body=None, binary=False):
    base, headers = connection_details()
    data = None if body is None else json.dumps(body).encode()
    with urlopen(Request(base + path, headers=headers, data=data), timeout=12) as response:
        raw = response.read(MAX_ARCHIVE + 1)
    if len(raw) > MAX_ARCHIVE:
        raise ValueError("API response exceeds limit")
    return raw if binary else json.loads(raw)


def launch(version):
    heartbeat = ROOT / "heartbeat.json"
    heartbeat.unlink(missing_ok=True)
    environment = os.environ.copy()
    environment.update(SALONMAX_SHARED_ROOT=str(SHARED), SALONMAX_APP_VERSION=version,
                       SALONMAX_UPDATE_STATE=str(ROOT))
    with (ROOT / "till.log").open("ab") as log:
        return subprocess.Popen(["/usr/bin/python3", "run_native_till.py"],
                                cwd=ROOT / "releases" / version, env=environment,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log)


def healthy(process, version, timeout=60):
    deadline = time.monotonic() + timeout
    first_healthy = None
    while time.monotonic() < deadline and process.poll() is None:
        heartbeat = read_json(ROOT / "heartbeat.json", {})
        if (heartbeat.get("pid") == process.pid and heartbeat.get("version") == version
                and 0 <= time.time() - heartbeat.get("timestamp", 0) < 10):
            first_healthy = first_healthy or time.monotonic()
            if time.monotonic() - first_healthy >= 20:
                return True
        else:
            first_healthy = None
        time.sleep(1)
    return False


def backup_database(version):
    directory = ROOT / "backups"
    directory.mkdir(exist_ok=True)
    path = directory / (version + "-" + str(time.time_ns()) + ".sqlite")
    path.touch(mode=0o600)
    with sqlite3.connect(DATABASE, timeout=10) as source, sqlite3.connect(path) as target:
        source.backup(target)


def stop_failed(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def install_pending(state, pending, old, state_path):
    version = pending["version"]
    backup_database(version)
    state.update(previous=old, trying=version, trying_request=pending["request_id"],
                 state="installing", message="")
    write_json(state_path, state)
    process = launch(version)
    if healthy(process, version):
        state.update(installed=version, trying="", state="current", failed="", message="",
                     installed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    else:
        stop_failed(process)
        state.update(failed=version + ":" + pending["request_id"], trying="", state="rolled_back",
                     message="Startup check failed. Previous release restored.", installed=old)
        version = old
        process = launch(old)
        if not healthy(process, old):
            stop_failed(process)
            raise RuntimeError("Rollback failed startup check")
    return process, version


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--adopt-pid", type=int)
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (ROOT / "supervisor.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.adopt_pid:
            last_report = 0
            while existing_till(args.adopt_pid):
                if time.monotonic() - last_report > 60:
                    last_report = time.monotonic()
                    try:
                        state = read_json(ROOT / "state.json")
                        api("/updates/report", {"installed": "", "available": state["installed"],
                            "state": "waiting_idle", "message": "Initial installation awaits the existing till app closing"})
                    except Exception:
                        pass
                time.sleep(2)
        supervise()


def existing_till(pid):
    proc = Path(f"/proc/{pid}")
    try:
        return (proc.joinpath("cwd").resolve() == SHARED
                and b"run_native_till.py" in proc.joinpath("cmdline").read_bytes().split(b"\0")
                and not proc.joinpath("stat").read_text().split(") ", 1)[1].startswith("Z "))
    except (OSError, IndexError):
        return False


def supervise():
    state_path = ROOT / "state.json"
    state = read_json(state_path)
    version = state["installed"]
    # A service interruption during an unconfirmed install selects the last known working release.
    if state.get("trying"):
        version = state["previous"]
        state.update(installed=version, failed=state["trying"] + ":" + state.get("trying_request", ""),
                     trying="", state="rolled_back", message="Interrupted installation recovered")
    public_key = bytes.fromhex((ROOT / "release-public-key.hex").read_text().strip())
    process = launch(version)
    if not healthy(process, version):
        stop_failed(process)
        raise RuntimeError("Installed till did not pass its startup check")
    if not state.get("installed_at"):
        state["installed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        write_json(state_path, state)
    next_check = 0
    pending = None
    while True:
        if process.poll() is not None:
            acknowledgement = read_json(ROOT / "heartbeat.json", {})
            if (pending and acknowledgement.get("pid") == process.pid
                    and acknowledgement.get("restarting") is True
                    and 0 <= time.time() - acknowledgement.get("timestamp", 0) < 180):
                process, version = install_pending(state, pending, version, state_path)
                pending = None
                (ROOT / "restart-request.json").unlink(missing_ok=True)
                write_json(state_path, state)
                next_check = 0
            else:
                time.sleep(5)
                process = launch(version)
                if not healthy(process, version):
                    stop_failed(process)
                    raise RuntimeError("Till restart failed startup check")
        if time.monotonic() >= next_check:
            next_check = time.monotonic() + 60
            try:
                manifest = api("/updates/manifest")
                release = verify_release(manifest, public_key)
                target = release["version"]
                request_id = manifest.get("request_id", "")
                state["available"] = target
                if pending and pending["version"] != target:
                    pending = None
                    (ROOT / "restart-request.json").unlink(missing_ok=True)
                if target != version and state.get("failed") != target + ":" + request_id:
                    if not pending:
                        state.update(state="downloading", message="")
                        data = api("/updates/artifact/" + target, binary=True)
                        unpack(data, release, ROOT / "releases" / target)
                        pending = {"version": target, "request_id": request_id}
                    state.update(state="waiting_idle", message="Sign out when beds and queues are idle")
                elif target == version:
                    state.update(state="current", message="")
                write_json(state_path, state)
            except Exception as error:
                state.update(state="error", message="Update check failed (" + type(error).__name__ + ")")
                write_json(state_path, state)
            try:
                api("/updates/report", {key: state.get(key, "") for key in
                    ("installed", "available", "state", "message", "installed_at")})
            except Exception:
                pass
        if pending:
            write_json(ROOT / "restart-request.json", {"pid": process.pid, "timestamp": time.time()})
        time.sleep(2)


if __name__ == "__main__":
    main()
