"""One-time installation for a paired Pi, run from the release checkout as its desktop user."""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import managed_updater as updater


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--existing-pid", type=int)
    args = parser.parse_args()
    source = Path(__file__).parent
    root = updater.ROOT
    if (root / "state.json").exists():
        raise SystemExit("Updater is already installed. Use the owner platform for releases.")
    manifest = json.loads((source / "releases/stable.json").read_text())
    version = manifest["version"]
    document = json.loads((source / "releases" / (version + ".json")).read_text())
    key = bytes.fromhex((source / "release-public-key.hex").read_text().strip())
    release = updater.verify_release(document, key)
    updater.connection_details()
    env = {}
    if args.existing_pid:
        proc = Path(f"/proc/{args.existing_pid}")
        command = proc.joinpath("cmdline").read_bytes().split(b"\0")
        if b"run_native_till.py" not in command or proc.joinpath("cwd").resolve() != updater.SHARED:
            raise SystemExit("The specified process is not the native Salon Max till.")
        env = dict(part.decode().split("=", 1) for part in proc.joinpath("environ").read_bytes().split(b"\0") if b"=" in part)
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    (root / "releases").mkdir(exist_ok=True)
    shutil.copy2(source / "managed_updater.py", root / "managed_updater.py")
    shutil.copy2(source / "release-public-key.hex", root / "release-public-key.hex")
    updater.unpack((source / "releases" / (version + ".zip")).read_bytes(), release, root / "releases" / version)
    state = {"installed": version, "available": version, "state": "current", "message": "",
             "installed_at": "", "previous": version, "trying": ""}
    updater.write_json(root / "state.json", state)
    updater.backup_database(version)
    environment = {name: env.get(name, default) for name, default in {
        "DISPLAY": ":0", "XAUTHORITY": str(Path.home() / ".Xauthority"),
        "WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"
    }.items()}
    for value in environment.values():
        if any(char in value for char in ('\n', '\r', '"', '%', '\\')):
            raise SystemExit("Unsupported desktop environment value")
    unit_dir = Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    settings = "\n".join(f'Environment="{name}={value}"' for name, value in environment.items())
    # --adopt-pid prevents a second GUI while the old till finishes its current session.
    adopt = f" --adopt-pid {args.existing_pid}" if args.existing_pid else ""
    unit = f"""[Unit]
Description=Salon Max managed native till
After=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 %h/.local/share/salonmax-updater/managed_updater.py{adopt}
WorkingDirectory=%h/Salon-Max
Restart=on-failure
RestartSec=15
UMask=0077
{settings}

[Install]
WantedBy=default.target
"""
    (unit_dir / "salonmax-native-till.service").write_text(unit)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "salonmax-native-till.service"], check=True)
    print("Installed the signed till release and managed startup service.")
    if args.existing_pid:
        print("The supervisor will take over after the existing till app exits.")


if __name__ == "__main__":
    main()
