"""Validate release signatures, archive boundaries and the cooperative restart gate."""
import importlib.util
import io
import json
import sys
import types
import zipfile
from pathlib import Path

import pytest


@pytest.fixture
def updater(monkeypatch):
    if sys.platform == "win32":
        monkeypatch.setitem(sys.modules, "fcntl", types.SimpleNamespace())
    path = Path(__file__).parents[1] / "pi/managed_updater.py"
    spec = importlib.util.spec_from_file_location("managed_update_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_signed_release_and_tampered_manifest(updater, tmp_path):
    source = Path(__file__).parents[1] / "pi"
    version = json.loads((source / "releases/stable.json").read_text())["version"]
    document = json.loads((source / "releases" / (version + ".json")).read_text())
    key = bytes.fromhex((source / "release-public-key.hex").read_text())
    release = updater.verify_release(document, key)
    archive = (source / "releases" / (version + ".zip")).read_bytes()
    updater.unpack(archive, release, tmp_path / "valid")
    assert (tmp_path / "valid/VERSION").read_text() == version
    with pytest.raises(ValueError):
        updater.unpack(archive + b"tampered", release, tmp_path / "bad")
    document["release"]["version"] = "9999"
    with pytest.raises(Exception):
        updater.verify_release(document, key)


def test_archive_cannot_write_outside_release(updater, tmp_path):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("../outside.py", "pass")
    release = {"sha256": updater.hashlib.sha256(data.getvalue()).hexdigest()}
    with pytest.raises(ValueError):
        updater.unpack(data.getvalue(), release, tmp_path / "release")
    assert not (tmp_path / "outside.py").exists()


def test_failed_install_restores_previous_release(updater, monkeypatch, tmp_path):
    launched = []
    stopped = []
    backups = []
    monkeypatch.setattr(updater, "launch", lambda version: launched.append(version) or version)
    monkeypatch.setattr(updater, "healthy", lambda process, version: version == "1.0")
    monkeypatch.setattr(updater, "stop_failed", lambda process: stopped.append(process))
    monkeypatch.setattr(updater, "backup_database", lambda version: backups.append(version))
    state = {"installed": "1.0"}
    process, version = updater.install_pending(state, {"version": "2.0", "request_id": "retry"},
                                              "1.0", tmp_path / "state.json")
    assert launched == ["2.0", "1.0"] and stopped == ["2.0"] and backups == ["2.0"]
    assert process == version == state["installed"] == "1.0"
    assert state["state"] == "rolled_back" and state["failed"] == "2.0:retry"


def test_restart_gate_waits_for_logout_beds_and_queue(monkeypatch, tmp_path):
    path = Path(__file__).parents[1] / "pi/till-source/native_till_app/update_runtime.py"
    monkeypatch.syspath_prepend(str(path.parent.parent))
    spec = importlib.util.spec_from_file_location("native_till_app.update_runtime", path)
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    monkeypatch.setenv("SALONMAX_UPDATE_STATE", str(tmp_path))
    monkeypatch.setenv("SALONMAX_APP_VERSION", "test")
    app = types.SimpleNamespace(quit=lambda: exits.append(True))
    window = types.SimpleNamespace(session=types.SimpleNamespace(staff_row={"role": "staff"}), runtime_state={})
    exits = []
    (tmp_path / "restart-request.json").write_text(json.dumps({"pid": runtime.os.getpid(), "timestamp": runtime.time.time()}))
    runtime.update_tick(window, app)
    assert not exits
    window.session.staff_row = None
    for state in ({"bed_states": {1: {"current_phase": "tanning"}}},
                  {"bed_queues": {1: ["customer"]}}, {"sale_baskets": {1: ["sale"]}}):
        window.runtime_state = state
        runtime.update_tick(window, app)
        assert not exits
    window.runtime_state = {}
    runtime.update_tick(window, app)
    assert exits == [True]
    assert json.loads((tmp_path / "heartbeat.json").read_text())["restarting"]
