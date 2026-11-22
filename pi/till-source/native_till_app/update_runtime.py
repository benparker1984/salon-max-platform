"""Cooperative restart boundary, called on the Qt application thread."""
import json
import os
import time
from pathlib import Path


def update_tick(window, application):
    directory = Path(os.environ["SALONMAX_UPDATE_STATE"])
    directory.mkdir(parents=True, exist_ok=True)
    safe = (window.session.staff_row is None
            and not any(window.runtime_state.get("bed_queues", {}).values())
            and not any(window.runtime_state.get("sale_baskets", {}).values())
            and all(state.get("current_phase", "ready") == "ready"
                    for state in window.runtime_state.get("bed_states", {}).values()))
    try:
        command = json.loads((directory / "restart-request.json").read_text())
    except (OSError, ValueError):
        command = {}
    restart = bool(safe and command.get("pid") == os.getpid()
                   and 0 <= time.time() - command.get("timestamp", 0) < 180)
    state = {"pid": os.getpid(), "timestamp": time.time(), "safe": safe,
             "version": os.environ["SALONMAX_APP_VERSION"], "restarting": restart}
    temporary = directory / "heartbeat.next"
    temporary.write_text(json.dumps(state))
    temporary.replace(directory / "heartbeat.json")
    if restart:
        application.quit()
