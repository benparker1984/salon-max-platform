"""Cooperative restart boundary, called on the Qt application thread."""
import json
import os
import time
from pathlib import Path


def update_tick(window, application):
    directory = Path(os.environ["SALONMAX_UPDATE_STATE"])
    directory.mkdir(parents=True, exist_ok=True)
    from .groundhog import hardware_idle
    idle = hardware_idle(window)
    safe = window.session.staff_row is None and idle
    try:
        command = json.loads((directory / "restart-request.json").read_text())
    except (OSError, ValueError):
        command = {}
    restart = bool(safe and command.get("pid") == os.getpid()
                   and 0 <= time.time() - command.get("timestamp", 0) < 180)
    state = {"pid": os.getpid(), "timestamp": time.time(), "safe": safe,
             "version": os.environ["SALONMAX_APP_VERSION"], "restarting": restart, "hardware_idle": idle}
    temporary = directory / "heartbeat.next"
    temporary.write_text(json.dumps(state))
    temporary.replace(directory / "heartbeat.json")
    if restart:
        application.quit()
