from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

try:
    import RPi.GPIO as PI_GPIO
except ImportError:
    PI_GPIO = None

try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QColor, QPainter, QPixmap
    from PySide6.QtWidgets import (
        QApplication,
        QButtonGroup,
        QComboBox,
        QDialog,
        QDoubleSpinBox,
        QFormLayout,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QProgressBar,
        QGraphicsDropShadowEffect,
        QStackedWidget,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
        QWidget,
    )
    try:
        from PySide6.QtSvg import QSvgRenderer
    except ImportError:
        QSvgRenderer = None
except ImportError:
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtGui import QColor, QPainter, QPixmap
    from PyQt6.QtWidgets import (
        QApplication,
        QButtonGroup,
        QComboBox,
        QDialog,
        QDoubleSpinBox,
        QFormLayout,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QProgressBar,
        QGraphicsDropShadowEffect,
        QStackedWidget,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
        QWidget,
    )
    try:
        from PyQt6.QtSvg import QSvgRenderer
    except ImportError:
        QSvgRenderer = None

from .database import TillRepository, TillSessionContext, default_database_path
from .styles import APP_STYLESHEET


GPIO_BED_MAP = {
    1: {"enable_bcm": 5, "start_bcm": 17, "feedback_bcm": 2},
    2: {"enable_bcm": 12, "start_bcm": 22, "feedback_bcm": 3},
    3: {"enable_bcm": 16, "start_bcm": 24, "feedback_bcm": 4},
    4: {"enable_bcm": 20, "start_bcm": 26, "feedback_bcm": 7},
    5: {"enable_bcm": 6, "start_bcm": 27, "feedback_bcm": 8},
    6: {"enable_bcm": 13, "start_bcm": 23, "feedback_bcm": 9},
    7: {"enable_bcm": 19, "start_bcm": 25, "feedback_bcm": 10},
    8: {"enable_bcm": 21, "start_bcm": 11, "feedback_bcm": 18},
}
GPIO_FEEDBACK_ACTIVE_LOW = True
NATIVE_APP_ROOT = Path(os.environ.get('SALONMAX_SHARED_ROOT') or Path(__file__).resolve().parent.parent)
LOCAL_SYNC_STUB_LOG_PATH = NATIVE_APP_ROOT / "sync_stub.log"
LOCAL_SYNC_HTTP_LOG_PATH = NATIVE_APP_ROOT / "sync_http.log"
FORCE_PAIRING_MODE_FLAG_PATH = NATIVE_APP_ROOT / "force_pairing_mode.flag"
_FEEDBACK_GPIO_READY: set[int] = set()
ROOM_READY_ARM_MIN_TIMEOUT_SECONDS = 300
EXTERNAL_START_FEEDBACK_TIMEOUT_SECONDS = 30


def run_system_command(command: str) -> bool:
    try:
        completed = subprocess.run(command, shell=True, capture_output=True, text=True)
    except Exception:
        return False
    return completed.returncode == 0


def open_cash_drawer() -> bool:
    return run_system_command(r"""printf '\a' | lp -d star_tsp100 -o raw""")


def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def raspberry_pi_serial() -> str:
    candidates = [Path("/proc/device-tree/serial-number"), Path("/sys/firmware/devicetree/base/serial-number")]
    for path in candidates:
        try:
            if path.exists():
                value = path.read_text(encoding="utf-8", errors="ignore").strip().strip("\x00")
                if value:
                    return value
        except Exception:
            continue
    try:
        cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"^Serial\s*:\s*(.+)$", cpuinfo, re.MULTILINE)
        if match:
            value = match.group(1).strip()
            if value:
                return value
    except Exception:
        pass
    return socket.gethostname().strip() or "unknown-device"


def hardware_fingerprint_for_terminal(serial_text: str, site_id: int, terminal_id: int) -> str:
    raw = f"{serial_text}|site:{site_id}|terminal:{terminal_id}|salonmax"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def bootstrap_local_platform_identity(
    repo: TillRepository,
    *,
    site_id: int,
    terminal_id: int,
    terminal_name: str,
) -> None:
    sync_settings = repo.sync_settings()
    site_row = repo.fetch_one("select * from sites where id = ?", (int(site_id),))
    serial_text = raspberry_pi_serial()
    existing_identity = repo.local_device_identity()
    business_account_public_id = str(sync_settings.get("salonmax_business_account_public_id") or "").strip()
    if not business_account_public_id and existing_identity is not None:
        business_account_public_id = str(existing_identity["business_account_public_id"] or "").strip()
    business_account_public_id = business_account_public_id or "biz_local_dev"
    site_public_id = str(sync_settings.get("salonmax_site_public_id") or "").strip()
    if not site_public_id and existing_identity is not None:
        site_public_id = str(existing_identity["site_public_id"] or "").strip()
    if not site_public_id:
        site_code = str(site_row["code"] if site_row is not None and "code" in site_row.keys() else "").strip().lower()
        site_public_id = f"site_{site_code}" if site_code else f"site_{int(site_id)}"
    terminal_public_id = ""
    if existing_identity is not None:
        terminal_public_id = str(existing_identity["terminal_device_public_id"] or "").strip()
    if not terminal_public_id:
        terminal_public_id = f"term_{int(site_id)}_{int(terminal_id)}"
    fingerprint = hardware_fingerprint_for_terminal(serial_text, int(site_id), int(terminal_id))
    repo.save_local_device_identity(
        business_account_public_id=business_account_public_id,
        site_public_id=site_public_id,
        terminal_device_public_id=terminal_public_id,
        device_serial=serial_text,
        hardware_fingerprint=fingerprint,
    )

    current_lease = repo.current_licence_lease()
    if current_lease is None:
        now_text = utc_now_text()
        expires_text = datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        expires_dt = datetime.strptime(expires_text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        future_dt = expires_dt.timestamp() + (30 * 24 * 60 * 60)
        future_text = datetime.fromtimestamp(future_dt, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        repo.store_local_licence_lease(
            terminal_device_public_id=terminal_public_id,
            licence_status="local_dev",
            signed_token=f"local-dev:{terminal_public_id}:{serial_text}",
            issued_at=now_text,
            expires_at=future_text,
            last_verified_at=now_text,
        )


def process_local_sync_stub(repo: TillRepository, log_path: Path = LOCAL_SYNC_STUB_LOG_PATH) -> int:
    settings = repo.sync_settings()
    if int(settings.get("sync_stub_enabled") or 0) != 1:
        return 0
    processed = 0
    pending_rows = repo.pending_sync_events(limit=50)
    if not pending_rows:
        return 0

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        for row in pending_rows:
            local_event_uuid = str(row["local_event_uuid"])
            event_type = str(row["event_type"])
            payload_text = str(row["payload_json"] or "{}")
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                payload = {"raw_payload": payload_text}

            repo.mark_sync_event_attempt(local_event_uuid, failed=False)
            handle.write(
                json.dumps(
                    {
                        "stub_processed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "local_event_uuid": local_event_uuid,
                        "event_type": event_type,
                        "payload": payload,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            )
            repo.mark_sync_event_acknowledged(local_event_uuid)
            processed += 1
    return processed


def process_sync_push_http(repo: TillRepository, log_path: Path = LOCAL_SYNC_HTTP_LOG_PATH) -> int:
    settings = repo.sync_settings()
    base_url = str(settings.get("sync_api_base_url") or "").strip().rstrip("/")
    if not base_url:
        return 0

    pending_rows = repo.pending_sync_events(limit=50)
    if not pending_rows:
        return 0

    device_identity = repo.local_device_identity()
    current_lease = repo.current_licence_lease()
    business_account_id = ""
    terminal_device_id = ""
    token = ""
    if device_identity is not None:
        business_account_id = str(device_identity["business_account_public_id"] or "").strip()
        terminal_device_id = str(device_identity["terminal_device_public_id"] or "").strip()
    if current_lease is not None:
        token = str(current_lease["signed_token"] or "").strip()

    events = []
    event_ids = []
    for row in pending_rows:
        local_event_uuid = str(row["local_event_uuid"])
        event_ids.append(local_event_uuid)
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except json.JSONDecodeError:
            payload = {"raw_payload": str(row["payload_json"] or "")}
        events.append(
            {
                "local_event_id": local_event_uuid,
                "event_type": str(row["event_type"] or ""),
                "created_at": payload.get("created_at") or utc_now_text(),
                "business_account_id": business_account_id,
                "terminal_device_id": terminal_device_id,
                "payload": payload,
            }
        )

    for local_event_uuid in event_ids:
        repo.mark_sync_event_attempt(local_event_uuid, failed=False)

    request_body = {
        "terminal_device_id": terminal_device_id,
        "events": events,
        "sync_health": repo.sync_outbox_health(),
    }
    request_bytes = json.dumps(request_body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
    }
    if business_account_id:
        headers["X-SalonMax-Business-Id"] = business_account_id
    if terminal_device_id:
        headers["X-SalonMax-Device-Id"] = terminal_device_id
    if token:
        headers["Authorization"] = f"Bearer {token}"

    log_path.parent.mkdir(parents=True, exist_ok=True)
    request_url = f"{base_url}/sync/events/push"
    try:
        request = Request(request_url, data=request_bytes, headers=headers, method="POST")
        with urlopen(request, timeout=15) as response:
            response_text = response.read().decode("utf-8", errors="replace")
        response_json = json.loads(response_text or "{}")
        accepted = response_json.get("data", {}).get("accepted", [])
        accepted_ids = {
            str(item.get("local_event_id") or "")
            for item in accepted
            if str(item.get("local_event_id") or "").strip()
        }
        processed = 0
        for local_event_uuid in event_ids:
            if local_event_uuid in accepted_ids:
                repo.mark_sync_event_acknowledged(local_event_uuid)
                processed += 1
            else:
                repo.mark_sync_event_attempt(local_event_uuid, failed=True)

        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "attempted_at": utc_now_text(),
                        "request_url": request_url,
                        "sent_event_count": len(events),
                        "accepted_event_ids": sorted(accepted_ids),
                        "response": response_json,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            )
        return processed
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        for local_event_uuid in event_ids:
            repo.mark_sync_event_attempt(local_event_uuid, failed=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "attempted_at": utc_now_text(),
                        "request_url": request_url,
                        "sent_event_count": len(events),
                        "error": str(exc),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            )
        return 0


def fetch_cloud_licence_lease(repo: TillRepository, *, app_version: str = "native-till-dev") -> dict | None:
    settings = repo.sync_settings()
    base_url = str(settings.get("sync_api_base_url") or "").strip().rstrip("/")
    device_identity = repo.local_device_identity()
    current_lease = repo.current_licence_lease()
    if not base_url or device_identity is None or current_lease is None:
        return None

    terminal_device_id = str(device_identity["terminal_device_public_id"] or "").strip()
    business_account_id = str(device_identity["business_account_public_id"] or "").strip()
    token = str(current_lease["signed_token"] or "").strip()
    if not terminal_device_id:
        return None

    request_url = f"{base_url}/licence/check-in"
    body = {
        "terminal_device_id": terminal_device_id,
        "app_version": os.environ.get("SALONMAX_APP_VERSION", app_version),
        "last_seen_at": utc_now_text(),
        "last_sync_at": utc_now_text(),
        "sync_health": repo.sync_outbox_health(),
    }
    headers = {
        "Content-Type": "application/json",
        "X-SalonMax-Device-Id": terminal_device_id,
    }
    if business_account_id:
        headers["X-SalonMax-Business-Id"] = business_account_id
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        request_url,
        data=json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            response_text = response.read().decode("utf-8", errors="replace")
        response_json = json.loads(response_text or "{}")
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    if response_json.get("ok") is not True:
        return None
    data = response_json.get("data")
    return data if isinstance(data, dict) else None


def pair_cloud_device(repo: TillRepository, *, pairing_code: str, terminal_name: str = "Front Desk Till") -> dict | None:
    settings = repo.sync_settings()
    base_url = str(settings.get("sync_api_base_url") or "").strip().rstrip("/")
    if not base_url:
        return None

    serial_text = raspberry_pi_serial()
    previous_identity = repo.local_device_identity()
    previous_business_account_id = ""
    if previous_identity is not None:
        previous_business_account_id = str(previous_identity["business_account_public_id"] or "").strip()
    request_url = f"{base_url}/devices/pair"
    body = {
        "pairing_code": str(pairing_code or "").strip().upper(),
        "device_serial": serial_text,
        "terminal_name": str(terminal_name or "").strip() or "Front Desk Till",
    }
    request = Request(
        request_url,
        data=json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            response_text = response.read().decode("utf-8", errors="replace")
        response_json = json.loads(response_text or "{}")
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    if response_json.get("ok") is not True:
        return None
    data = response_json.get("data")
    if not isinstance(data, dict):
        return None

    business_account_id = str(data.get("business_account_public_id") or "").strip()
    business_name = str(data.get("business_name") or "").strip()
    site_public_id = str(data.get("site_public_id") or "").strip()
    site_name = str(data.get("site_name") or "").strip()
    terminal_device_id = str(data.get("terminal_device_public_id") or "").strip()
    paired_terminal_name = str(data.get("terminal_name") or "").strip() or terminal_name
    install_mode = str(data.get("install_mode") or "fresh_install").strip() or "fresh_install"
    if not business_account_id or not site_public_id or not terminal_device_id:
        return None

    fresh_install_mode = install_mode != "replacement_pi"
    if fresh_install_mode and (FORCE_PAIRING_MODE_FLAG_PATH.exists() or not previous_business_account_id or previous_business_account_id != business_account_id):
        repo.reset_for_fresh_salon_install(
            business_name=business_name or "Your Salon",
            site_name=site_name or "Main Site",
            terminal_name=paired_terminal_name,
        )
    elif install_mode == "replacement_pi" and previous_business_account_id and previous_business_account_id != business_account_id:
        repo.reset_for_fresh_salon_install(
            business_name=business_name or "Your Salon",
            site_name=site_name or "Main Site",
            terminal_name=paired_terminal_name,
        )

    repo.update_sync_settings(
        business_account_public_id=business_account_id,
        site_public_id=site_public_id,
    )
    existing_identity = repo.local_device_identity()
    existing_fingerprint = ""
    if existing_identity is not None:
        existing_fingerprint = str(existing_identity["hardware_fingerprint"] or "").strip()
    repo.save_local_device_identity(
        business_account_public_id=business_account_id,
        site_public_id=site_public_id,
        terminal_device_public_id=terminal_device_id,
        device_serial=serial_text,
        hardware_fingerprint=existing_fingerprint or hashlib.sha256(serial_text.encode("utf-8")).hexdigest(),
    )
    now_text = utc_now_text()
    repo.store_local_licence_lease(
        terminal_device_public_id=terminal_device_id,
        licence_status=str(data.get("licence_status") or "active"),
        signed_token=str(data.get("signed_token") or ""),
        issued_at=str(data.get("issued_at") or now_text),
        expires_at=str(data.get("expires_at") or now_text),
        last_verified_at=now_text,
    )
    return data


def fetch_cloud_device_config(repo: TillRepository) -> dict | None:
    settings = repo.sync_settings()
    base_url = str(settings.get("sync_api_base_url") or "").strip().rstrip("/")
    device_identity = repo.local_device_identity()
    current_lease = repo.current_licence_lease()
    if not base_url or device_identity is None or current_lease is None:
        return None

    terminal_device_id = str(device_identity["terminal_device_public_id"] or "").strip()
    if not terminal_device_id:
        return None
    token = str(current_lease["signed_token"] or "").strip()
    request_url = f"{base_url}/devices/{terminal_device_id}/config"
    headers = {
        "X-SalonMax-Device-Id": terminal_device_id,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(request_url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=15) as response:
            response_text = response.read().decode("utf-8", errors="replace")
        response_json = json.loads(response_text or "{}")
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    if response_json.get("ok") is not True:
        return None
    data = response_json.get("data")
    return data if isinstance(data, dict) else None


def fetch_cloud_customer_corrections(repo: TillRepository) -> dict | None:
    settings = repo.sync_settings()
    base_url = str(settings.get("sync_api_base_url") or "").strip().rstrip("/")
    device_identity = repo.local_device_identity()
    current_lease = repo.current_licence_lease()
    checkpoint = repo.cloud_correction_checkpoint()
    if not base_url or device_identity is None or current_lease is None or checkpoint is None:
        return None

    terminal_device_id = str(device_identity["terminal_device_public_id"] or "").strip()
    business_account_id = str(device_identity["business_account_public_id"] or "").strip()
    token = str(current_lease["signed_token"] or "").strip()
    if not terminal_device_id or not business_account_id:
        return None

    since_minute_ledger_id = int(checkpoint["last_minute_ledger_id"] or 0)
    since_balance_ledger_id = int(checkpoint["last_balance_ledger_id"] or 0)
    request_url = (
        f"{base_url}/customers/corrections"
        f"?since_minute_ledger_id={since_minute_ledger_id}"
        f"&since_balance_ledger_id={since_balance_ledger_id}"
    )
    headers = {
        "X-SalonMax-Device-Id": terminal_device_id,
        "X-SalonMax-Business-Id": business_account_id,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(request_url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=15) as response:
            response_text = response.read().decode("utf-8", errors="replace")
        response_json = json.loads(response_text or "{}")
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    if response_json.get("ok") is not True:
        return None
    data = response_json.get("data")
    return data if isinstance(data, dict) else None


def fetch_cloud_customer_directory(repo: TillRepository) -> dict | None:
    settings = repo.sync_settings()
    base_url = str(settings.get("sync_api_base_url") or "").strip().rstrip("/")
    device_identity = repo.local_device_identity()
    current_lease = repo.current_licence_lease()
    checkpoint = repo.cloud_customer_directory_checkpoint()
    if not base_url or device_identity is None or current_lease is None or checkpoint is None:
        return None

    terminal_device_id = str(device_identity["terminal_device_public_id"] or "").strip()
    business_account_id = str(device_identity["business_account_public_id"] or "").strip()
    token = str(current_lease["signed_token"] or "").strip()
    if not terminal_device_id or not business_account_id:
        return None

    updated_since = str(checkpoint["last_customer_updated_at"] or "").strip()
    request_url = f"{base_url}/customers/directory"
    if updated_since:
        request_url += f"?updated_since={quote(updated_since, safe='')}"
    headers = {
        "X-SalonMax-Device-Id": terminal_device_id,
        "X-SalonMax-Business-Id": business_account_id,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(request_url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=15) as response:
            response_text = response.read().decode("utf-8", errors="replace")
        response_json = json.loads(response_text or "{}")
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    if response_json.get("ok") is not True:
        return None
    data = response_json.get("data")
    return data if isinstance(data, dict) else None


def print_receipt_text(receipt_text: str) -> bool:
    receipt_payload = str(receipt_text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not receipt_payload.endswith("\n\n\n"):
        receipt_payload = f"{receipt_payload}\n\n\n"
    try:
        completed = subprocess.run(
            ["lp", "-d", "star_tsp100"],
            input=receipt_payload,
            capture_output=True,
            text=True,
        )
    except Exception:
        return False
    return completed.returncode == 0


def device_pairing_required(repo: TillRepository) -> bool:
    if FORCE_PAIRING_MODE_FLAG_PATH.exists():
        return True
    identity = repo.local_device_identity()
    lease = repo.current_licence_lease()
    if identity is None or lease is None:
        return True
    business_id = str(identity["business_account_public_id"] or "").strip()
    site_id = str(identity["site_public_id"] or "").strip()
    terminal_id = str(identity["terminal_device_public_id"] or "").strip()
    token = str(lease["signed_token"] or "").strip()
    if not business_id or not site_id or not terminal_id:
        return True
    if not token.startswith("sm-lease:"):
        return True
    return False


def business_access_locked(repo: TillRepository) -> tuple[bool, str]:
    lease = repo.current_licence_lease()
    if lease is None:
        return False, ""
    licence_status = str(lease["licence_status"] or "").strip().lower()
    if licence_status in {"", "active", "local_dev"}:
        return False, ""
    if licence_status == "suspended":
        return True, "This salon business is suspended. Trading is locked until Salon Max reactivates the account."
    if licence_status == "paused":
        return True, "This salon business is paused. Trading is locked until the account is reactivated."
    if licence_status == "deleted":
        return True, "This salon business has been archived. Trading is locked."
    if licence_status == "site_suspended":
        return True, "This salon site is suspended. Trading is locked at this location until Salon Max reactivates it."
    if licence_status == "site_deleted":
        return True, "This salon site has been archived. Trading is locked at this location."
    if licence_status == "terminal_suspended":
        return True, "This terminal is suspended. Trading is locked on this till until Salon Max reactivates it."
    if licence_status == "terminal_retired":
        return True, "This terminal has been retired. Trading is locked on this till."
    if licence_status == "terminal_missing":
        return True, "This terminal is not provisioned correctly, so trading is locked."
    if licence_status == "site_missing":
        return True, "This site is not provisioned correctly, so trading is locked."
    return True, f"Trading is locked because the business licence is marked {licence_status}."


def staff_bootstrap_required(repo: TillRepository) -> bool:
    return repo.active_staff_count() <= 0


def clear_force_pairing_flag():
    try:
        if FORCE_PAIRING_MODE_FLAG_PATH.exists():
            FORCE_PAIRING_MODE_FLAG_PATH.unlink()
    except OSError:
        pass


def format_receipt_text(repo: TillRepository, transaction_ids: list[int]) -> str | None:
    if not transaction_ids:
        return None
    receipt_blocks = []
    for transaction_id in transaction_ids:
        receipt_data = repo.transaction_receipt_data(int(transaction_id))
        if receipt_data is not None:
            receipt_blocks.append(receipt_data)
    if not receipt_blocks:
        return None
    transaction = receipt_blocks[0]["transaction"]
    settings = receipt_blocks[0]["settings"]
    business_name = str(settings.get("business_name") or "Your Salon").strip()
    currency_symbol = str(settings.get("currency_symbol") or "GBP").strip() or "GBP"
    customer_name = " ".join(
        part for part in [str(transaction["first_name"] or "").strip(), str(transaction["last_name"] or "").strip()] if part
    ).strip() or "Customer"
    transaction_number = str(transaction["transaction_number"] or transaction["id"])
    site_name = str(transaction["site_name"] or "").strip()
    terminal_name = str(transaction["terminal_name"] or "").strip()
    created_at = format_timestamp_for_display(transaction["created_at"])
    payment_method = str(transaction["payment_method"] or "").replace("_", " ").title()
    total_amount = float(transaction["total_amount"] or 0)

    receipt_lines = [
        business_name,
        "Receipt",
        f"Txn: {transaction_number}",
        f"Date: {created_at or '-'}",
    ]
    if site_name:
        receipt_lines.append(f"Site: {site_name}")
    if terminal_name:
        receipt_lines.append(f"Till: {terminal_name}")
    receipt_lines.extend(
        [
            "-" * 32,
            f"Customer: {customer_name}",
        ]
    )
    grand_total = 0.0
    for block in receipt_blocks:
        for line in block["lines"]:
            description = str(line["description"] or "").strip() or "Item"
            quantity = float(line["quantity"] or 0)
            line_total = float(line["line_total"] or 0)
            minutes = int(line["minutes"] or 0)
            grand_total += line_total
            receipt_lines.append(description[:32])
            detail = f"x{int(quantity) if quantity.is_integer() else quantity:g}  {currency_symbol} {line_total:.2f}"
            if minutes > 0:
                detail += f"  ({minutes} min)"
            receipt_lines.append(detail)
    receipt_lines.extend(
        [
            "-" * 32,
            f"Paid: {payment_method}",
            f"Total: {currency_symbol} {grand_total or total_amount:.2f}",
            "",
            "Thank you",
            "\n\n\n",
        ]
    )
    return "\n".join(receipt_lines)


def print_receipt_for_transactions(parent: QWidget, repo: TillRepository, transaction_ids: list[int]) -> None:
    receipt_text = format_receipt_text(repo, transaction_ids)
    if not receipt_text or not print_receipt_text(receipt_text):
        QMessageBox.warning(parent, "Receipt Not Printed", "The receipt could not be printed.")


def set_gpio_output_high(bcm: int) -> bool:
    return run_system_command(f"pinctrl set {int(bcm)} op dh")


def set_gpio_output_low(bcm: int) -> bool:
    return run_system_command(f"pinctrl set {int(bcm)} op dl")


def enable_bcm_for_bed(bed_number: int) -> int | None:
    mapping = GPIO_BED_MAP.get(int(bed_number or 1))
    if not mapping:
        return None
    return int(mapping["enable_bcm"])


def start_bcm_for_bed(bed_number: int) -> int | None:
    mapping = GPIO_BED_MAP.get(int(bed_number or 1))
    if not mapping:
        return None
    return int(mapping["start_bcm"])


def relay_on_for_bed(bed_number: int) -> bool:
    enable_bcm = enable_bcm_for_bed(bed_number)
    if enable_bcm is None:
        return False
    return set_gpio_output_high(enable_bcm)


def relay_off_for_bed(bed_number: int) -> bool:
    enable_bcm = enable_bcm_for_bed(bed_number)
    start_bcm = start_bcm_for_bed(bed_number)
    results = []
    if enable_bcm is not None:
        results.append(set_gpio_output_low(enable_bcm))
    if start_bcm is not None:
        results.append(set_gpio_output_low(start_bcm))
    return all(results) if results else False


def relay_off_all() -> bool:
    results = []
    for bed_number in GPIO_BED_MAP:
        results.append(relay_off_for_bed(bed_number))
    return all(results) if results else True


def feedback_bcm_for_bed(bed_number: int) -> int | None:
    mapping = GPIO_BED_MAP.get(int(bed_number or 1))
    if not mapping:
        return None
    return int(mapping["feedback_bcm"])


def trigger_bcm_for_bed(bed_number: int) -> int | None:
    return start_bcm_for_bed(bed_number)


def uses_external_start_flow(bed_number: int) -> bool:
    return enable_bcm_for_bed(bed_number) is not None and start_bcm_for_bed(bed_number) is not None


def ensure_feedback_gpio_ready(bed_number: int) -> bool:
    if PI_GPIO is None:
        return False
    feedback_bcm = feedback_bcm_for_bed(bed_number)
    if feedback_bcm is None:
        return False
    if feedback_bcm in _FEEDBACK_GPIO_READY:
        return True
    try:
        PI_GPIO.setmode(PI_GPIO.BCM)
        PI_GPIO.setup(feedback_bcm, PI_GPIO.IN, pull_up_down=PI_GPIO.PUD_UP)
    except Exception:
        return False
    _FEEDBACK_GPIO_READY.add(feedback_bcm)
    return True


def read_feedback_gpio_high(bed_number: int) -> bool | None:
    if PI_GPIO is None:
        return None
    if not ensure_feedback_gpio_ready(bed_number):
        return None
    feedback_bcm = feedback_bcm_for_bed(bed_number)
    if feedback_bcm is None:
        return None
    try:
        return bool(PI_GPIO.input(feedback_bcm))
    except Exception:
        return None


def read_feedback_gpio_active(bed_number: int) -> bool | None:
    high = read_feedback_gpio_high(bed_number)
    if high is None:
        return None
    return not high if GPIO_FEEDBACK_ACTIVE_LOW else high


def trigger_pulse_for_bed(bed_number: int, seconds: float = 0.5) -> bool:
    trigger_bcm = trigger_bcm_for_bed(bed_number)
    if trigger_bcm is None:
        return False
    if not set_gpio_output_high(trigger_bcm):
        return False
    QTimer.singleShot(max(1, int(seconds * 1000)), lambda current_bcm=trigger_bcm: set_gpio_output_low(current_bcm))
    return True


def nudge_active_phase_display(state: dict):
    if int(state.get("phase_total_seconds") or 0) <= 0:
        return
    if int(state.get("time_left") or 0) > 0:
        state["time_left"] = max(0, int(state["time_left"]) - 1)
    state["phase_elapsed_seconds"] = min(
        int(state.get("phase_total_seconds") or 0),
        max(1, int(state.get("phase_elapsed_seconds") or 0)),
    )


def sunbed_display_text(row: dict, fallback_bed_number: int) -> str:
    custom_name = format_sunbed_name_for_display(str(row.get("custom_name") or "").strip()) if row else ""
    bed_number = int(row.get("bed_number") or fallback_bed_number) if row else fallback_bed_number
    if custom_name:
        return f"Bed {bed_number} | {custom_name}"
    return f"Bed {bed_number}"


def format_sunbed_name_for_display(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Za-z])(?=[0-9])", " ", text)
    text = re.sub(r"(?<=[0-9])(?=[A-Za-z])", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.title()


def resolve_local_image_path(image_path: str) -> str:
    text = str(image_path or "").strip().replace("\\", "/")
    if not text:
        return ""
    text = text.split("?", 1)[0].split("#", 1)[0]
    if text.startswith("http://") or text.startswith("https://"):
        return text

    candidates: list[Path] = []

    if "/static/uploads/" in text:
        upload_name = text.split("/static/uploads/", 1)[1].lstrip("/")
        candidates.append(NATIVE_APP_ROOT / "static" / "uploads" / upload_name)
    elif text.startswith("/static/"):
        candidates.append(NATIVE_APP_ROOT / text.lstrip("/"))
    elif text.startswith("static/"):
        candidates.append(NATIVE_APP_ROOT / text)

    path = Path(text)
    if path.is_absolute():
        candidates.append(path)
        if "/static/uploads/" in text:
            upload_name = text.split("/static/uploads/", 1)[1].lstrip("/")
            candidates.append(NATIVE_APP_ROOT / "static" / "uploads" / upload_name)
        else:
            candidates.append(NATIVE_APP_ROOT / "static" / "uploads" / path.name)
    else:
        candidates.append((NATIVE_APP_ROOT / text).resolve())
        candidates.append(NATIVE_APP_ROOT / "static" / "uploads" / path.name)

    seen = set()
    for candidate in candidates:
        candidate_text = str(candidate)
        if candidate_text in seen:
            continue
        seen.add(candidate_text)
        if candidate.exists():
            return candidate_text

    return str(candidates[0]) if candidates else ""


def cover_pixmap(pixmap: QPixmap, target_width: int, target_height: int) -> QPixmap:
    if pixmap.isNull():
        return pixmap
    scaled = pixmap.scaled(
        target_width,
        target_height,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    x_offset = max(0, int((scaled.width() - target_width) / 2))
    y_offset = max(0, int((scaled.height() - target_height) / 2))
    return scaled.copy(x_offset, y_offset, target_width, target_height)


def calculate_age_from_birthday_text(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            birthday = datetime.strptime(text, fmt).date()
            today = datetime.now().date()
            age = today.year - birthday.year - ((today.month, today.day) < (birthday.month, birthday.day))
            if age < 0:
                return ""
            return str(age)
        except ValueError:
            continue
    return ""


def format_age_label(value: str) -> str:
    age = calculate_age_from_birthday_text(value)
    return age if age else "-"


def format_timestamp_for_display(timestamp_text: str | None, *, include_seconds: bool = False) -> str:
    text = str(timestamp_text or "").strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            utc_dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            local_dt = utc_dt.astimezone()
            output_fmt = "%d/%m/%Y %H:%M:%S" if include_seconds else "%d/%m/%Y %H:%M"
            return local_dt.strftime(output_fmt)
        except ValueError:
            continue
    return text


def transaction_within_last_24_hours(created_at_text: str) -> bool:
    text = str(created_at_text or "").strip()
    if not text:
        return False
    try:
        created_at = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - created_at).total_seconds() <= 24 * 60 * 60


def build_logo_mark(repo: TillRepository, width: int = 178, height: int = 68) -> QLabel:
    branding = repo.branding_settings()
    logo_label = QLabel()
    logo_label.setFixedSize(width, height)
    logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    logo_label.setStyleSheet("background:transparent; border:none;")

    pixmap = QPixmap()
    logo_path = branding["logo_path"]
    if logo_path:
        try:
            logo_path_obj = Path(str(logo_path))
            if logo_path_obj.suffix.lower() == ".svg" and QSvgRenderer is not None:
                renderer = QSvgRenderer(str(logo_path_obj))
                if renderer.isValid():
                    svg_pixmap = QPixmap(width, height)
                    svg_pixmap.fill(Qt.GlobalColor.transparent)
                    painter = QPainter(svg_pixmap)
                    renderer.render(painter)
                    painter.end()
                    pixmap = svg_pixmap
                else:
                    pixmap = QPixmap()
            elif logo_path_obj.suffix.lower() == ".svg":
                png_fallback = logo_path_obj.with_suffix(".png")
                if png_fallback.exists():
                    pixmap.load(str(png_fallback))
                else:
                    pixmap = QPixmap()
            else:
                pixmap.load(str(logo_path_obj))
        except Exception:
            pixmap = QPixmap()

    if not pixmap.isNull():
        scaled = pixmap.scaled(
            width,
            height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        logo_label.setFixedSize(scaled.size())
        logo_label.setPixmap(scaled)
    else:
        logo_label.setText("SM")
        logo_label.setStyleSheet(
            "background:#eff5fa; border:1px solid #d3dfe9; border-radius:18px; color:#547086; font-size:28px; font-weight:800;"
        )
    return logo_label


def build_brand_header(repo: TillRepository, page_title: str, centered: bool = True) -> QWidget:
    wrapper = QWidget()
    layout = QHBoxLayout(wrapper)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)

    layout.addWidget(build_logo_mark(repo, 92, 92), 0, Qt.AlignmentFlag.AlignTop)

    page_label = QLabel(page_title)
    page_label.setProperty("role", "section_title")
    layout.addWidget(page_label, 1, Qt.AlignmentFlag.AlignVCenter)
    return wrapper


@dataclass
class AppSession:
    staff_row: object | None = None
    till_session: TillSessionContext | None = None


@dataclass
class PendingSale:
    customer_id: int
    customer_name: str
    customer_account: str
    minutes: int
    payment_method: str
    total_amount: float
    device_name: str
    device_id: int | None = None
    target_bed_number: int = 1
    remaining_seconds: int | None = None
    resume_phase: str | None = None
    tanning_seconds_remaining: int | None = None
    cooldown_seconds_remaining: int | None = None


class CardFrame(QFrame):
    def __init__(self, hero: bool = False):
        super().__init__()
        self.setObjectName("HeroCard" if hero else "Card")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28 if hero else 22)
        shadow.setOffset(0, 10 if hero else 8)
        shadow.setColor(QColor(14, 39, 61, 42))
        self.setGraphicsEffect(shadow)


def build_workflow_nav(current_key: str, items: list[tuple[str, str, object | None, bool]]) -> QWidget:
    shell = CardFrame()
    layout = QHBoxLayout(shell)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(10)

    for key, label, callback, enabled in items:
        button = QPushButton(label)
        button.setProperty("navtab", "true")
        button.setCheckable(True)
        button.setChecked(key == current_key)
        button.setEnabled(enabled)
        if callback is not None and enabled:
            button.clicked.connect(callback)
        layout.addWidget(button)

    layout.addStretch(1)
    return shell


def build_top_strip(
    repo: TillRepository,
    current_key: str,
    items: list[tuple[str, str, object | None, bool]],
    actions: list[tuple[str, object, str]] | None = None,
) -> QWidget:
    shell = CardFrame()
    layout = QHBoxLayout(shell)
    layout.setContentsMargins(10, 8, 10, 8)
    layout.setSpacing(10)

    layout.addWidget(build_logo_mark(repo, 86, 86), 0, Qt.AlignmentFlag.AlignTop)

    nav_shell = QWidget()
    nav_layout = QHBoxLayout(nav_shell)
    nav_layout.setContentsMargins(0, 0, 0, 0)
    nav_layout.setSpacing(8)
    for key, label, callback, enabled in items:
        button = QPushButton(label)
        button.setProperty("navtab", "true")
        button.setCheckable(True)
        button.setChecked(key == current_key)
        button.setEnabled(enabled)
        if callback is not None and enabled:
            button.clicked.connect(callback)
        nav_layout.addWidget(button)
    nav_layout.addStretch(1)
    layout.addWidget(nav_shell, 1)

    for label, callback, variant in actions or []:
        button = QPushButton(label)
        if variant:
            button.setProperty("variant", variant)
        button.clicked.connect(callback)
        layout.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)

    return shell


def build_muted_panel() -> QFrame:
    panel = QFrame()
    panel.setProperty("panel", "muted")
    return panel


class TransactionHistoryDialog(QDialog):
    def __init__(self, repo: TillRepository, customer_id: int, customer_name: str, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.customer_id = customer_id
        self.setWindowTitle(f"Transaction History - {customer_name}")
        self.resize(1100, 650)

        layout = QVBoxLayout(self)
        title = QLabel(f"Transaction History - {customer_name}")
        title.setProperty("role", "title")
        layout.addWidget(title)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Date", "Transaction #", "Type", "Payment", "Status", "Amount", "Notes"]
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)

        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button, alignment=Qt.AlignmentFlag.AlignRight)

        self.load_rows()

    def load_rows(self):
        rows = self.repo.recent_transactions(self.customer_id, limit=100)
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = [
                format_timestamp_for_display(row["created_at"]),
                row["transaction_number"],
                row["transaction_type"].replace("_", " ").title(),
                row["payment_method"].replace("_", " ").title(),
                row["status"].title(),
                f"GBP {float(row['total_amount'] or 0):.2f}",
                row["notes"] or "-",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                self.table.setItem(row_index, col, item)
        self.table.resizeColumnsToContents()


class CreateCustomerDialog(QDialog):
    def __init__(self, repo: TillRepository, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.created_customer_id: Optional[int] = None
        self.setWindowTitle("Create Customer")
        self.resize(520, 360)

        layout = QVBoxLayout(self)

        title = QLabel("Create Customer")
        title.setProperty("role", "title")
        layout.addWidget(title)

        form = QFormLayout()
        form.setSpacing(14)

        self.first_name_input = QLineEdit()
        self.last_name_input = QLineEdit()
        self.birthday_input = QLineEdit()
        self.birthday_input.setPlaceholderText("DD/MM/YYYY")
        self.birthday_input.textChanged.connect(self.update_age_preview)
        self.account_input = QLineEdit()
        self.phone_input = QLineEdit()
        self.email_input = QLineEdit()
        self.age_preview = QLineEdit()
        self.age_preview.setReadOnly(True)
        self.age_preview.setPlaceholderText("Age")

        form.addRow("First Name", self.first_name_input)
        form.addRow("Last Name", self.last_name_input)
        form.addRow("Date Of Birth", self.birthday_input)
        form.addRow("Age", self.age_preview)
        form.addRow("Account Number", QLabel("Assigned automatically"))
        form.addRow("Phone", self.phone_input)
        form.addRow("Email", self.email_input)
        layout.addLayout(form)

        hint = QLabel("DOB is important here because staff often search by date of birth.")
        hint.setProperty("role", "muted")
        layout.addWidget(hint)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setProperty("variant", "secondary")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)

        create = QPushButton("Create Customer")
        create.setProperty("variant", "success")
        create.clicked.connect(self.create_customer)
        actions.addWidget(create)
        layout.addLayout(actions)

        self.update_age_preview()

    def update_age_preview(self):
        self.age_preview.setText(format_age_label(self.birthday_input.text()))

    def create_customer(self):
        first_name = self.first_name_input.text().strip()
        last_name = self.last_name_input.text().strip()
        if not first_name or not last_name:
            QMessageBox.warning(self, "Missing Details", "Enter at least a first name and last name.")
            return

        self.created_customer_id = self.repo.create_customer(
            first_name=first_name,
            last_name=last_name,
            birthday=self.birthday_input.text(),
            account_number=self.account_input.text(),
            phone=self.phone_input.text(),
            email=self.email_input.text(),
        )
        self.accept()


class EditCustomerDialog(QDialog):
    def __init__(self, repo: TillRepository, customer_row, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.customer_id = int(customer_row["id"])
        self.setWindowTitle("Edit Customer")
        self.resize(520, 380)

        layout = QVBoxLayout(self)

        title = QLabel("Edit Customer")
        title.setProperty("role", "title")
        layout.addWidget(title)

        form = QFormLayout()
        form.setSpacing(14)

        self.first_name_input = QLineEdit(str(customer_row["first_name"] or ""))
        self.last_name_input = QLineEdit(str(customer_row["last_name"] or ""))
        self.birthday_input = QLineEdit(self.repo.birthday_for_display(customer_row["birthday"]))
        self.birthday_input.setPlaceholderText("DD/MM/YYYY")
        self.birthday_input.textChanged.connect(self.update_age_preview)
        self.account_input = QLineEdit(str(customer_row["account_number"] or ""))
        self.phone_input = QLineEdit(str(customer_row["phone"] or ""))
        self.email_input = QLineEdit(str(customer_row["email"] or ""))
        self.age_preview = QLineEdit()
        self.age_preview.setReadOnly(True)
        self.age_preview.setPlaceholderText("Age")

        form.addRow("First Name", self.first_name_input)
        form.addRow("Last Name", self.last_name_input)
        form.addRow("Date Of Birth", self.birthday_input)
        form.addRow("Age", self.age_preview)
        form.addRow("Account Number", self.account_input)
        form.addRow("Phone", self.phone_input)
        form.addRow("Email", self.email_input)
        layout.addLayout(form)

        hint = QLabel("Update any missing details here and save them straight back to the customer record.")
        hint.setProperty("role", "muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        actions = QHBoxLayout()
        actions.addStretch(1)

        cancel = QPushButton("Cancel")
        cancel.setProperty("variant", "secondary")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)

        save = QPushButton("Save Changes")
        save.setProperty("variant", "success")
        save.clicked.connect(self.save_customer)
        actions.addWidget(save)
        layout.addLayout(actions)

        self.update_age_preview()

    def update_age_preview(self):
        self.age_preview.setText(format_age_label(self.birthday_input.text()))

    def save_customer(self):
        first_name = self.first_name_input.text().strip()
        last_name = self.last_name_input.text().strip()
        if not first_name or not last_name:
            QMessageBox.warning(self, "Missing Details", "Enter at least a first name and last name.")
            return

        self.repo.update_customer(
            customer_id=self.customer_id,
            first_name=first_name,
            last_name=last_name,
            birthday=self.birthday_input.text(),
            account_number=self.account_input.text(),
            phone=self.phone_input.text(),
            email=self.email_input.text(),
        )
        self.accept()


class CardPaymentDialog(QDialog):
    def __init__(self, amount: float, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Card Payment")
        self.resize(420, 220)

        layout = QVBoxLayout(self)
        layout.setSpacing(14)

        title = QLabel("Offer Card Payment")
        title.setProperty("role", "title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        detail = QLabel(f"Charge GBP {amount:.2f} by card?")
        detail.setProperty("role", "section_title")
        detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(detail)

        note = QLabel("Only continue once the card payment has been accepted.")
        note.setProperty("role", "muted")
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(note)

        buttons = QHBoxLayout()
        decline = QPushButton("Decline")
        decline.setProperty("variant", "secondary")
        decline.clicked.connect(self.reject)
        buttons.addWidget(decline)

        accept = QPushButton("Accepted")
        accept.setProperty("variant", "success")
        accept.clicked.connect(self.accept)
        buttons.addWidget(accept)
        layout.addLayout(buttons)


class CashPaymentDialog(QDialog):
    def __init__(self, amount: float, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Cash Payment")
        self.resize(420, 240)

        layout = QVBoxLayout(self)
        layout.setSpacing(14)

        title = QLabel("Take Cash Payment")
        title.setProperty("role", "title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        detail = QLabel(f"Take GBP {amount:.2f} cash from the customer.")
        detail.setProperty("role", "section_title")
        detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(detail)

        note = QLabel("Back returns to the tanning sale screen without starting a bed.")
        note.setProperty("role", "muted")
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(note)

        buttons = QHBoxLayout()
        back = QPushButton("Back")
        back.setProperty("variant", "secondary")
        back.clicked.connect(self.reject)
        buttons.addWidget(back)

        confirm = QPushButton("Cash Confirmed")
        confirm.setProperty("variant", "success")
        confirm.clicked.connect(self.accept)
        buttons.addWidget(confirm)
        layout.addLayout(buttons)


class ChangeMachineDialog(QDialog):
    def __init__(self, machine_choices: list[tuple[int, str]], current_bed_number: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Change Machine")
        self.resize(460, 220)
        self.selected_bed_number = current_bed_number

        layout = QVBoxLayout(self)
        layout.setSpacing(14)

        title = QLabel("Change Sunbed")
        title.setProperty("role", "title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        detail = QLabel("Choose the machine this queued customer should move to.")
        detail.setProperty("role", "muted")
        detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(detail)

        self.machine_combo = QComboBox()
        for bed_number, label in machine_choices:
            self.machine_combo.addItem(label, bed_number)
            if bed_number == current_bed_number:
                self.machine_combo.setCurrentIndex(self.machine_combo.count() - 1)
        layout.addWidget(self.machine_combo)

        buttons = QHBoxLayout()
        cancel = QPushButton("Cancel")
        cancel.setProperty("variant", "secondary")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)

        confirm = QPushButton("Move Customer")
        confirm.setProperty("variant", "success")
        confirm.clicked.connect(self.confirm_choice)
        buttons.addWidget(confirm)
        layout.addLayout(buttons)

    def confirm_choice(self):
        self.selected_bed_number = int(self.machine_combo.currentData())
        self.accept()


class GpioTestDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GPIO Relay Test")
        self.resize(900, 620)

        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        title = QLabel("GPIO Relay Test")
        title.setProperty("role", "title")
        layout.addWidget(title)

        note = QLabel(
            "Use this only while wiring and testing. Enable holds the room-live relay. "
            "Start sends the separate start relay pulse."
        )
        note.setProperty("role", "muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.status_label = QLabel("Ready for relay testing.")
        self.status_label.setStyleSheet("font-size:15px; font-weight:700; color:#0f4c6e;")
        layout.addWidget(self.status_label)

        table = QTableWidget(len(GPIO_BED_MAP), 5)
        table.setHorizontalHeaderLabels(["Bed", "Enable GPIO", "Start GPIO", "Feedback GPIO", "Test"])
        table.horizontalHeader().setStretchLastSection(True)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        table.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        for row_index, bed_number in enumerate(sorted(GPIO_BED_MAP)):
            mapping = GPIO_BED_MAP[bed_number]
            table.setItem(row_index, 0, QTableWidgetItem(f"Bed {bed_number}"))
            table.setItem(row_index, 1, QTableWidgetItem(str(mapping["enable_bcm"])))
            table.setItem(row_index, 2, QTableWidgetItem(str(mapping["start_bcm"])))
            table.setItem(row_index, 3, QTableWidgetItem(str(mapping["feedback_bcm"])))

            row_actions = QWidget()
            row_layout = QHBoxLayout(row_actions)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)

            enable_button = QPushButton("Enable 1.2s")
            enable_button.setProperty("variant", "success")
            enable_button.clicked.connect(
                lambda _checked=False, current_bed=bed_number: self.pulse_enable(current_bed)
            )
            row_layout.addWidget(enable_button)

            start_button = QPushButton("Start Pulse")
            start_button.setProperty("variant", "success")
            start_button.clicked.connect(
                lambda _checked=False, current_bed=bed_number: self.pulse_start(current_bed)
            )
            row_layout.addWidget(start_button)

            off_button = QPushButton("Relay Off")
            off_button.setProperty("variant", "secondary")
            off_button.clicked.connect(
                lambda _checked=False, current_bed=bed_number: self.turn_off_relay(current_bed)
            )
            row_layout.addWidget(off_button)
            row_layout.addStretch(1)

            table.setCellWidget(row_index, 4, row_actions)

        table.resizeColumnsToContents()
        layout.addWidget(table, 1)

        footer = QHBoxLayout()
        stop_all = QPushButton("Stop All Relays")
        stop_all.setProperty("variant", "danger")
        stop_all.clicked.connect(self.stop_all_relays)
        footer.addWidget(stop_all)
        footer.addStretch(1)

        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        footer.addWidget(close_button)
        layout.addLayout(footer)

    def pulse_enable(self, bed_number: int):
        relay_off_all()
        if not relay_on_for_bed(bed_number):
            self.status_label.setText(f"Bed {bed_number} enable relay did not trigger.")
            return
        self.status_label.setText(
            f"Bed {bed_number} enable relay pulsed on GPIO {GPIO_BED_MAP[bed_number]['enable_bcm']}."
        )
        QTimer.singleShot(1200, lambda current_bed=bed_number: relay_off_for_bed(current_bed))

    def pulse_start(self, bed_number: int):
        start_bcm = start_bcm_for_bed(bed_number)
        if start_bcm is None or not trigger_pulse_for_bed(bed_number, 0.5):
            self.status_label.setText(f"Bed {bed_number} start relay did not pulse.")
            return
        self.status_label.setText(f"Bed {bed_number} start relay pulsed on GPIO {start_bcm}.")

    def turn_off_relay(self, bed_number: int):
        relay_off_for_bed(bed_number)
        self.status_label.setText(f"Bed {bed_number} relays turned off.")

    def stop_all_relays(self):
        relay_off_all()
        self.status_label.setText("All relays turned off.")


class PairDeviceDialog(QDialog):
    def __init__(self, repo: TillRepository, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.paired_data: dict | None = None
        self.setWindowTitle("Pair Device")
        self.resize(480, 260)

        layout = QVBoxLayout(self)
        layout.setSpacing(14)

        title = QLabel("Pair Raspberry Pi To Salon Max")
        title.setProperty("role", "title")
        layout.addWidget(title)

        note = QLabel(
            "Use the pairing code from the Salon Max owner platform. This links this Pi to a pre-created business, site, and till."
        )
        note.setProperty("role", "muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        form.setSpacing(12)

        self.terminal_name_input = QLineEdit()
        self.terminal_name_input.setText("Front Desk Till")
        form.addRow("Terminal Name", self.terminal_name_input)

        self.pairing_code_input = QLineEdit()
        self.pairing_code_input.setPlaceholderText("Enter pairing code")
        self.pairing_code_input.setMinimumHeight(42)
        form.addRow("Pairing Code", self.pairing_code_input)

        layout.addLayout(form)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setProperty("variant", "secondary")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)

        pair_button = QPushButton("Pair Device")
        pair_button.setProperty("variant", "success")
        pair_button.clicked.connect(self.try_pair)
        buttons.addWidget(pair_button)
        layout.addLayout(buttons)

    def try_pair(self):
        pairing_code = self.pairing_code_input.text().strip().upper()
        terminal_name = self.terminal_name_input.text().strip() or "Front Desk Till"
        if not pairing_code:
            QMessageBox.warning(self, "Pair Device", "Enter the pairing code from the Salon Max platform.")
            return
        paired = pair_cloud_device(self.repo, pairing_code=pairing_code, terminal_name=terminal_name)
        if not paired:
            QMessageBox.warning(
                self,
                "Pair Device",
                "Pairing failed. Check the code and confirm the Salon Max platform is reachable.",
            )
            return
        self.paired_data = paired
        QMessageBox.information(
            self,
            "Device Paired",
            f"Paired to {paired.get('business_name', 'business')} / {paired.get('site_name', 'site')} / {paired.get('terminal_name', 'terminal')}.",
        )
        self.accept()


class DeviceSetupPage(QWidget):
    def __init__(self, repo: TillRepository, on_paired, on_back_to_login):
        super().__init__()
        self.repo = repo
        self.on_paired = on_paired
        self.on_back_to_login = on_back_to_login

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 24)
        outer.setSpacing(14)

        card = CardFrame(hero=True)
        outer.addWidget(card, alignment=Qt.AlignmentFlag.AlignCenter)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(16)

        title = QLabel("Device Setup")
        title.setProperty("role", "title")
        layout.addWidget(title)

        note = QLabel(
            "Pair this Raspberry Pi to a terminal created in the Salon Max owner platform. "
            "Use this for first installs, replacement Pis, or when you need to move a till to a different salon."
        )
        note.setProperty("role", "muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        form.setSpacing(12)
        self.terminal_name_input = QLineEdit()
        self.terminal_name_input.setText("Front Desk Till")
        form.addRow("Terminal Name", self.terminal_name_input)
        self.pairing_code_input = QLineEdit()
        self.pairing_code_input.setPlaceholderText("Enter pairing code")
        self.pairing_code_input.setMinimumHeight(44)
        form.addRow("Pairing Code", self.pairing_code_input)
        layout.addLayout(form)

        self.status_label = QLabel("Waiting for pairing code.")
        self.status_label.setProperty("role", "muted")
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        self.back_button = QPushButton("Back To Login")
        self.back_button.setProperty("variant", "secondary")
        self.back_button.clicked.connect(self.on_back_to_login)
        buttons.addWidget(self.back_button)
        buttons.addStretch(1)
        pair_button = QPushButton("Pair Device")
        pair_button.setProperty("variant", "success")
        pair_button.clicked.connect(self.try_pair)
        buttons.addWidget(pair_button)
        layout.addLayout(buttons)
        outer.addStretch(1)

    def set_pairing_required_mode(self, required: bool):
        self.back_button.setVisible(not required)

    def try_pair(self):
        pairing_code = self.pairing_code_input.text().strip().upper()
        terminal_name = self.terminal_name_input.text().strip() or "Front Desk Till"
        if not pairing_code:
            QMessageBox.warning(self, "Device Setup", "Enter the pairing code from the Salon Max platform.")
            return
        self.status_label.setText("Attempting to pair this Raspberry Pi...")
        paired = pair_cloud_device(self.repo, pairing_code=pairing_code, terminal_name=terminal_name)
        if not paired:
            self.status_label.setText("Pairing failed.")
            QMessageBox.warning(
                self,
                "Device Setup",
                "Pairing failed. Check the code and make sure the Salon Max platform is reachable.",
            )
            return
        clear_force_pairing_flag()
        self.status_label.setText(
            f"Paired to {paired.get('business_name', 'business')} / {paired.get('site_name', 'site')}."
        )
        QMessageBox.information(
            self,
            "Device Paired",
            f"Paired to {paired.get('business_name', 'business')} / {paired.get('site_name', 'site')} / {paired.get('terminal_name', 'terminal')}.",
        )
        self.on_paired()


class StaffBootstrapPage(QWidget):
    def __init__(self, repo: TillRepository, on_created, on_back_to_setup):
        super().__init__()
        self.repo = repo
        self.on_created = on_created
        self.on_back_to_setup = on_back_to_setup

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 24)
        outer.setSpacing(14)

        card = CardFrame(hero=True)
        outer.addWidget(card, alignment=Qt.AlignmentFlag.AlignCenter)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(16)

        title = QLabel("Create First Staff Login")
        title.setProperty("role", "title")
        layout.addWidget(title)

        note = QLabel(
            "This salon is paired, but it does not have any active staff logins yet. "
            "Create the first owner or manager PIN so the till can be opened normally."
        )
        note.setProperty("role", "muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        form.setSpacing(12)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Owner or manager name")
        form.addRow("Staff Name", self.name_input)

        self.pin_input = QLineEdit()
        self.pin_input.setPlaceholderText("4 digit PIN")
        self.pin_input.setMaxLength(8)
        self.pin_input.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("PIN", self.pin_input)

        self.confirm_pin_input = QLineEdit()
        self.confirm_pin_input.setPlaceholderText("Confirm PIN")
        self.confirm_pin_input.setMaxLength(8)
        self.confirm_pin_input.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Confirm PIN", self.confirm_pin_input)
        layout.addLayout(form)

        self.status_label = QLabel("Waiting for first staff details.")
        self.status_label.setProperty("role", "muted")
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        back_button = QPushButton("Back To Device Setup")
        back_button.setProperty("variant", "secondary")
        back_button.clicked.connect(self.on_back_to_setup)
        buttons.addWidget(back_button)
        buttons.addStretch(1)

        create_button = QPushButton("Create First Staff User")
        create_button.setProperty("variant", "success")
        create_button.clicked.connect(self.create_first_staff)
        buttons.addWidget(create_button)
        layout.addLayout(buttons)
        outer.addStretch(1)

    def create_first_staff(self):
        name = self.name_input.text().strip()
        pin_code = self.pin_input.text().strip()
        confirm_pin = self.confirm_pin_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Create Staff Login", "Enter the first staff member's name.")
            return
        if len(pin_code) < 4 or not pin_code.isdigit():
            QMessageBox.warning(self, "Create Staff Login", "Enter a numeric PIN of at least 4 digits.")
            return
        if pin_code != confirm_pin:
            QMessageBox.warning(self, "Create Staff Login", "PIN confirmation does not match.")
            return
        self.repo.create_staff_user(name=name, pin_code=pin_code, role="owner")
        identity = self.repo.local_device_identity()
        if identity is not None:
            self.repo.enqueue_sync_event(
                event_type="staff.bootstrap_completed",
                payload={
                    "created_at": utc_now_text(),
                    "business_account_id": str(identity["business_account_public_id"] or "").strip(),
                    "site_public_id": str(identity["site_public_id"] or "").strip(),
                    "terminal_device_id": str(identity["terminal_device_public_id"] or "").strip(),
                    "staff_name": name,
                    "staff_role": "owner",
                },
            )
        self.status_label.setText(f"Created first staff login for {name}.")
        QMessageBox.information(
            self,
            "Staff Login Created",
            f"{name} can now log into the till with the PIN you just created.",
        )
        self.name_input.clear()
        self.pin_input.clear()
        self.confirm_pin_input.clear()
        self.on_created()


class SunbedBoardPage(QWidget):
    def __init__(
        self,
        repo: TillRepository,
        session: AppSession,
        runtime_state: dict,
        on_back,
        on_show_customer_search,
        on_show_totals,
        on_open_gpio_test,
    ):
        super().__init__()
        self.repo = repo
        self.session = session
        self.runtime_state = runtime_state
        self.on_back = on_back
        self.on_show_customer_search = on_show_customer_search
        self.on_show_totals = on_show_totals
        self.on_open_gpio_test = on_open_gpio_test
        self.feedback_last_states = self.runtime_state.setdefault("feedback_last_states", {})

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        layout.addWidget(
            build_top_strip(
                repo,
                "queue",
                [
                    ("search", "Client Search", self.on_show_customer_search, True),
                    ("queue", "Sunbed Queue", None, True),
                    ("cashup", "Cash Up", self.on_show_totals, True),
                ],
                [
                    ("Search Customer", self.on_show_customer_search, "warning"),
                    ("GPIO Test", self.on_open_gpio_test, "secondary"),
                    ("Back", self.on_back, "secondary"),
                    ("Global Emergency Stop", self.global_emergency_stop, "danger"),
                ],
            )
        )

        content_row = QHBoxLayout()
        content_row.setSpacing(8)

        left_card = CardFrame(hero=True)
        left_layout = QVBoxLayout(left_card)
        left_layout.setContentsMargins(10, 10, 10, 10)
        left_layout.setSpacing(4)
        left_title = QLabel("Bed Control")
        left_title.setProperty("role", "section_title")
        left_layout.addWidget(left_title)

        self.cards_container = QWidget()
        self.cards_layout = QGridLayout(self.cards_container)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setHorizontalSpacing(6)
        self.cards_layout.setVerticalSpacing(6)
        left_layout.addWidget(self.cards_container, 1)
        content_row.addWidget(left_card, 1)

        right_card = CardFrame(hero=True)
        right_layout = QVBoxLayout(right_card)
        right_layout.setContentsMargins(10, 10, 10, 10)
        right_layout.setSpacing(6)
        right_title = QLabel("Clients Queue")
        right_title.setProperty("role", "section_title")
        right_layout.addWidget(right_title)
        self.queue_summary_label = QLabel("Select a queued customer to start or move.")
        self.queue_summary_label.setProperty("role", "muted")
        self.queue_summary_label.setWordWrap(False)
        right_layout.addWidget(self.queue_summary_label)

        self.queue_table = QTableWidget(0, 6)
        self.queue_table.setHorizontalHeaderLabels(["Bed", "Customer", "Minutes", "Payment", "Wait", "Status"])
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.setAlternatingRowColors(True)
        self.queue_table.itemSelectionChanged.connect(self.update_queue_actions)
        right_layout.addWidget(self.queue_table, 1)

        self.selected_queue_label = QLabel("No queue selected")
        self.selected_queue_label.setStyleSheet("font-size:14px; font-weight:700; color:#0f4c6e;")
        right_layout.addWidget(self.selected_queue_label)

        queue_actions = QHBoxLayout()
        self.start_selected_button = QPushButton("Send To Room")
        self.start_selected_button.setProperty("variant", "success")
        self.start_selected_button.clicked.connect(self.start_selected_queue_item)
        queue_actions.addWidget(self.start_selected_button)

        self.move_selected_button = QPushButton("Move Selected")
        self.move_selected_button.setProperty("variant", "secondary")
        self.move_selected_button.clicked.connect(self.move_selected_queue_item)
        queue_actions.addWidget(self.move_selected_button)

        self.delete_selected_queue_button = QPushButton("Delete From Queue")
        self.delete_selected_queue_button.setProperty("variant", "danger")
        self.delete_selected_queue_button.clicked.connect(self.delete_selected_queue_item)
        queue_actions.addWidget(self.delete_selected_queue_button)
        right_layout.addLayout(queue_actions)

        content_row.addWidget(right_card, 1)
        layout.addLayout(content_row, 1)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        self.refresh_view()

    def manager_logged_in(self) -> bool:
        staff_row = self.session.staff_row
        if staff_row is None:
            return False
        return str(staff_row["role"]).strip().lower() == "manager"

    def bed_runtime(self, bed_number: int) -> dict:
        bed_states = self.runtime_state.setdefault("bed_states", {})
        return bed_states.setdefault(
            int(bed_number),
            {
                "current_phase": "ready",
                "time_left": 0,
                "phase_total_seconds": 0,
                "phase_elapsed_seconds": 0,
                "active_customer_name": "No Active Customer",
                "active_customer_account": "",
                "active_payment_method": "",
                "active_minutes": 0,
                "active_sale": None,
                "tanning_seconds_remaining": 0,
                "cooldown_seconds_remaining": 0,
                "cooldown_locked": False,
                "waiting_for_feedback_start": False,
                "feedback_wait_started_at": None,
                "relay_armed_started_at": None,
                "relay_fail_safe_deadline_at": None,
            },
        )

    def build_image_label(self, image_path: str, bed_type: str) -> QLabel:
        label = QLabel()
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        target_width = 118
        target_height = 74
        label.setFixedSize(target_width, target_height)
        label.setStyleSheet("background:#f3f7fb; border:1px solid #dbe8f5; border-radius:12px;")

        pixmap = QPixmap()
        resolved_image_path = resolve_local_image_path(image_path)
        if resolved_image_path:
            try:
                if resolved_image_path.startswith("http://") or resolved_image_path.startswith("https://"):
                    data = urlopen(resolved_image_path, timeout=3).read()
                    pixmap.loadFromData(data)
                else:
                    pixmap.load(resolved_image_path)
            except Exception:
                pixmap = QPixmap()

        if not pixmap.isNull():
            label.setPixmap(
                cover_pixmap(pixmap, target_width, target_height)
            )
        else:
            label.setText(str(bed_type).replace("_", " ").title())
            label.setProperty("role", "muted")
        return label

    def queue_wait_seconds(self, current_remaining_seconds, queue_items):
        total = current_remaining_seconds
        for item in queue_items:
            total += self.queue_item_total_seconds(item)
        return total

    def queue_item_total_seconds(self, item: dict) -> int:
        if item.get("remaining_seconds") not in (None, ""):
            total = int(item["remaining_seconds"])
            total += int(item.get("tanning_seconds_remaining") or 0)
            total += int(item.get("cooldown_seconds_remaining") or 0)
            return total
        prep_seconds = int(self.runtime_state.get("prep_minutes", 0)) * 60
        cooldown_seconds = int(self.runtime_state.get("cooldown_minutes", 0)) * 60
        return prep_seconds + (int(item["minutes"]) * 60) + cooldown_seconds

    def active_cycle_remaining_seconds(self, bed_number: int) -> int:
        state = self.bed_runtime(bed_number)
        phase = state["current_phase"]
        if phase == "ready":
            return 0
        total = int(state["time_left"] or 0)
        if phase == "prep":
            total += int(state.get("tanning_seconds_remaining") or 0)
            total += int(state.get("cooldown_seconds_remaining") or 0)
        elif phase == "tanning":
            total += int(state.get("cooldown_seconds_remaining") or 0)
        return total

    def bed_rows(self) -> list[dict]:
        return list(self.repo.sunbed_status_rows(self.session.till_session))

    def bed_number_for_device_id(self, device_id: int | None) -> int:
        if not device_id:
            return 1
        rows = self.bed_rows()
        for index, row in enumerate(rows, start=1):
            try:
                if int(row["id"]) == int(device_id):
                    return index
            except (TypeError, ValueError):
                continue
        return 1

    def bed_label_for_number(self, bed_number: int) -> str:
        rows = self.bed_rows()
        if 1 <= bed_number <= len(rows):
            return sunbed_display_text(dict(rows[bed_number - 1]), bed_number)
        return f"Bed {bed_number}"

    def bed_device_id_for_number(self, bed_number: int) -> int | None:
        rows = self.bed_rows()
        if 1 <= bed_number <= len(rows):
            try:
                return int(rows[bed_number - 1]["id"])
            except (TypeError, ValueError):
                return None
        return None

    def machine_choices(self) -> list[tuple[int, str]]:
        return [
            (index + 1, self.bed_label_for_number(index + 1))
            for index in range(max(8, len(self.bed_rows())))
        ]

    def bed_queue(self, bed_number: int) -> list[dict]:
        queues = self.runtime_state.setdefault("bed_queues", {index: [] for index in range(1, 9)})
        return queues.setdefault(bed_number, [])

    def queue_item_from_pending(self, pending_sale: PendingSale) -> dict:
        return {
            "customer_id": pending_sale.customer_id,
            "customer_name": pending_sale.customer_name,
            "customer_account": pending_sale.customer_account,
            "minutes": pending_sale.minutes,
            "payment_method": pending_sale.payment_method.replace("_", " ").title(),
            "payment_amount": pending_sale.total_amount,
            "device_id": pending_sale.device_id,
            "device_name": pending_sale.device_name,
            "target_bed_number": pending_sale.target_bed_number,
            "remaining_seconds": pending_sale.remaining_seconds,
            "resume_phase": pending_sale.resume_phase,
            "tanning_seconds_remaining": pending_sale.tanning_seconds_remaining,
            "cooldown_seconds_remaining": pending_sale.cooldown_seconds_remaining,
            "room_ready": False,
            "room_ready_activated_at": None,
        }

    def pending_sale_from_queue_item(self, queue_item: dict) -> PendingSale:
        return PendingSale(
            customer_id=queue_item["customer_id"],
            customer_name=queue_item["customer_name"],
            customer_account=queue_item["customer_account"],
            minutes=int(queue_item["minutes"]),
            payment_method=str(queue_item["payment_method"]).replace(" ", "_").lower(),
            total_amount=float(queue_item["payment_amount"]),
            device_id=int(queue_item["device_id"]) if queue_item.get("device_id") not in (None, "") else None,
            device_name=str(queue_item.get("device_name") or "Queued Booking"),
            target_bed_number=int(queue_item.get("target_bed_number") or 1),
            remaining_seconds=(
                int(queue_item["remaining_seconds"])
                if queue_item.get("remaining_seconds") not in (None, "")
                else None
            ),
            resume_phase=str(queue_item["resume_phase"])
            if queue_item.get("resume_phase") not in (None, "")
            else None,
            tanning_seconds_remaining=(
                int(queue_item["tanning_seconds_remaining"])
                if queue_item.get("tanning_seconds_remaining") not in (None, "")
                else None
            ),
            cooldown_seconds_remaining=(
                int(queue_item["cooldown_seconds_remaining"])
                if queue_item.get("cooldown_seconds_remaining") not in (None, "")
                else None
            ),
        )

    def queue_preview_text(self, queue_items: list[dict]) -> str:
        if not queue_items:
            return "No queue"
        return " | ".join(
            f"{index + 1}. {item['customer_name']} ({int(item['minutes'])} min)"
            for index, item in enumerate(queue_items[:3])
        )

    def clear_bed_runtime(self, bed_number: int):
        state = self.bed_runtime(bed_number)
        state["current_phase"] = "ready"
        state["time_left"] = 0
        state["phase_total_seconds"] = 0
        state["phase_elapsed_seconds"] = 0
        state["active_customer_name"] = "No Active Customer"
        state["active_customer_account"] = ""
        state["active_payment_method"] = ""
        state["active_minutes"] = 0
        state["active_sale"] = None
        state["tanning_seconds_remaining"] = 0
        state["cooldown_seconds_remaining"] = 0
        state["cooldown_locked"] = False
        state["waiting_for_feedback_start"] = False
        state["feedback_wait_started_at"] = None
        state["relay_armed_started_at"] = None
        state["relay_fail_safe_deadline_at"] = None

    def start_phase(self, bed_number: int, phase: str, seconds: int):
        seconds = max(0, int(seconds))
        state = self.bed_runtime(bed_number)
        state["current_phase"] = phase
        state["time_left"] = seconds
        state["phase_total_seconds"] = seconds
        state["phase_elapsed_seconds"] = 0

    def finish_bed_session(self, bed_number: int):
        relay_off_for_bed(bed_number)
        self.clear_bed_runtime(bed_number)

    def room_activation_fail_safe_timeout_seconds(self, booked_minutes: int) -> int:
        prep_seconds = int(self.runtime_state.get("prep_minutes", 0)) * 60
        cooldown_seconds = int(self.runtime_state.get("cooldown_minutes", 0)) * 60
        tanning_seconds = max(0, int(booked_minutes or 0)) * 60
        return max(
            ROOM_READY_ARM_MIN_TIMEOUT_SECONDS,
            prep_seconds + tanning_seconds + cooldown_seconds,
        )

    def activate_room_ready_booking_for_external_start(self, bed_number: int, *, send_trigger_pulse: bool):
        state = self.bed_runtime(bed_number)
        queue = self.bed_queue(bed_number)
        if state["current_phase"] != "ready" or not queue:
            return False
        first_item = queue[0]
        pending_sale = self.pending_sale_from_queue_item(queue.pop(0))
        active_bed_number = int(pending_sale.target_bed_number or bed_number or 1)
        state = self.bed_runtime(active_bed_number)

        if not relay_on_for_bed(active_bed_number):
            QMessageBox.warning(
                self,
                "Enable Not Sent",
                f"The room enable output for Bed {active_bed_number} did not run.",
            )
            self.finish_bed_session(active_bed_number)
            self.refresh_view()
            return False

        state["active_customer_name"] = pending_sale.customer_name
        state["active_customer_account"] = pending_sale.customer_account
        state["active_payment_method"] = pending_sale.payment_method.replace("_", " ").title()
        state["active_minutes"] = pending_sale.minutes
        state["active_sale"] = pending_sale
        state["waiting_for_feedback_start"] = True
        state["feedback_wait_started_at"] = None
        state["relay_armed_started_at"] = time.time()
        state["relay_fail_safe_deadline_at"] = state["relay_armed_started_at"] + self.room_activation_fail_safe_timeout_seconds(
            pending_sale.minutes
        )

        cooldown_seconds = int(self.runtime_state.get("cooldown_minutes", 0)) * 60
        tanning_seconds = pending_sale.minutes * 60
        state["tanning_seconds_remaining"] = tanning_seconds
        state["cooldown_seconds_remaining"] = cooldown_seconds
        prep_seconds = int(self.runtime_state.get("prep_minutes", 0)) * 60
        self.start_phase(active_bed_number, "prep", max(prep_seconds, 1))
        self.refresh_view()
        return True

    def send_external_start_trigger(self, bed_number: int) -> bool:
        state = self.bed_runtime(bed_number)
        if state["current_phase"] != "prep":
            return False
        if not bool(state.get("waiting_for_feedback_start")):
            return False
        if not trigger_pulse_for_bed(bed_number, 0.5):
            QMessageBox.warning(
                self,
                "Start Not Sent",
                f"The start pulse for Bed {bed_number} did not run. The room enable output remains active.",
            )
            return False
        state["waiting_for_feedback_start"] = False
        state["feedback_wait_started_at"] = None
        self.customer_started(bed_number)
        return True

    def advance_bed_to_next_phase(self, bed_number: int):
        state = self.bed_runtime(bed_number)
        phase = state["current_phase"]
        if phase == "prep":
            if bool(state.get("waiting_for_feedback_start")):
                return
            tanning_seconds = int(state.get("tanning_seconds_remaining") or 0)
            state["tanning_seconds_remaining"] = 0
            if tanning_seconds <= 0:
                cooldown_seconds = int(state.get("cooldown_seconds_remaining") or 0)
                state["cooldown_seconds_remaining"] = 0
                if cooldown_seconds > 0:
                    self.start_phase(bed_number, "cooldown", cooldown_seconds)
                else:
                    self.finish_bed_session(bed_number)
                return
            if not relay_on_for_bed(bed_number):
                QMessageBox.warning(
                    self,
                    "Relay Not Triggered",
                    f"Prep finished, but the Pi relay command for Bed {bed_number} did not run. The bed has not been started.",
                )
                self.finish_bed_session(bed_number)
                return
            self.start_phase(bed_number, "tanning", tanning_seconds)
            return

        if phase == "tanning":
            self.end_tanning_phase(bed_number)
            return

        if phase == "cooldown":
            self.finish_bed_session(bed_number)
            return

    def phase_label(self, phase):
        if phase == "prep":
            return "Prep"
        if phase == "tanning":
            return "Tanning"
        if phase == "cooldown":
            return "Cool Down"
        return "Ready"

    def phase_color(self, phase):
        if phase == "prep":
            return "#f57c00"
        if phase == "tanning":
            return "#d32f2f"
        if phase == "cooldown":
            return "#1976d2"
        return "#bdbdbd"

    def format_mmss(self, total_seconds):
        minutes = max(0, int(total_seconds)) // 60
        seconds = max(0, int(total_seconds)) % 60
        return f"{minutes:02d}:{seconds:02d}"

    def build_beds_data(self):
        rows = self.bed_rows()
        beds = []

        for index in range(8):
            row = rows[index] if index < len(rows) else None
            display_name = row["display_name"] if row else f"Bed {index + 1}"
            bed_type = row["bed_type"] if row else "lay_down"
            image_path = row["image_path"] if row else ""
            bed_number = index + 1
            queue_items = self.bed_queue(bed_number)
            state = self.bed_runtime(bed_number)
            current_phase = state["current_phase"]

            if state["phase_total_seconds"] > 0:
                progress_percent = int((state["phase_elapsed_seconds"] / state["phase_total_seconds"]) * 100)
                progress_percent = max(0, min(progress_percent, 100))
                if current_phase != "ready" and int(state.get("phase_elapsed_seconds") or 0) > 0:
                    progress_percent = max(1, progress_percent)
            else:
                progress_percent = 0

            beds.append(
                {
                    "display_name": display_name,
                    "custom_name": row["custom_name"] if row else "",
                    "bed_number": int(row["bed_number"]) if row else bed_number,
                    "bed_type": bed_type,
                    "image_path": image_path,
                    "status_text": self.phase_label(current_phase) if current_phase != "ready" else "Ready",
                    "wait_text": self.format_mmss(
                        self.queue_wait_seconds(self.active_cycle_remaining_seconds(bed_number), queue_items)
                    ),
                    "time_left": self.format_mmss(state["time_left"]),
                    "queue_count": len(queue_items),
                    "progress_percent": progress_percent,
                    "progress_color": self.phase_color(current_phase),
                    "active_customer": state["active_customer_name"],
                    "active_payment_method": state["active_payment_method"],
                    "pending_sale": None,
                    "queue_preview": self.queue_preview_text(queue_items),
                }
            )
        return beds

    def change_machine_for_queue_item(self, bed_number: int, queue_index: int):
        queue = self.bed_queue(bed_number)
        if queue_index < 0 or queue_index >= len(queue):
            return
        item = dict(queue[queue_index])
        dialog = ChangeMachineDialog(self.machine_choices(), bed_number, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_bed_number = int(dialog.selected_bed_number)
        if new_bed_number == bed_number:
            return
        queue.pop(queue_index)
        item["device_id"] = self.bed_device_id_for_number(new_bed_number)
        item["target_bed_number"] = new_bed_number
        item["device_name"] = self.bed_label_for_number(new_bed_number)
        self.bed_queue(new_bed_number).append(item)
        self.refresh_view()

    def rebuild_queue_rows(self, queue_items: list[dict]):
        return

    def queue_table_rows(self) -> list[dict]:
        rows = []
        for bed_number in range(1, 9):
            queue_items = self.bed_queue(bed_number)
            bed_wait = self.active_cycle_remaining_seconds(bed_number)
            for queue_index, item in enumerate(queue_items):
                wait_seconds = bed_wait
                for earlier in queue_items[:queue_index]:
                    wait_seconds += self.queue_item_total_seconds(earlier)
                rows.append(
                    {
                        "bed_number": bed_number,
                        "queue_index": queue_index,
                        "customer_name": str(item["customer_name"]),
                        "minutes": int(item["minutes"]),
                        "payment_method": str(item["payment_method"]),
                        "wait_text": self.format_mmss(wait_seconds),
                        "status_text": (
                            "Sent To Room"
                            if bool(item.get("room_ready"))
                            else ("Next" if queue_index == 0 else f"Queue {queue_index + 1}")
                        ),
                    }
                )
        return rows

    def selected_queue_reference(self) -> tuple[int, int] | None:
        row_index = self.queue_table.currentRow()
        if row_index < 0:
            return None
        item = self.queue_table.item(row_index, 0)
        if item is None:
            return None
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data:
            return None
        return int(data[0]), int(data[1])

    def update_queue_actions(self):
        ref = self.selected_queue_reference()
        if ref is None:
            self.selected_queue_label.setText("No queue selected")
            self.start_selected_button.setText("Send To Room")
            self.start_selected_button.setEnabled(False)
            self.move_selected_button.setEnabled(False)
            self.delete_selected_queue_button.setEnabled(False)
            return

        bed_number, queue_index = ref
        queue = self.bed_queue(bed_number)
        if queue_index >= len(queue):
            self.selected_queue_label.setText("Selected queue item is no longer available.")
            self.start_selected_button.setText("Send To Room")
            self.start_selected_button.setEnabled(False)
            self.move_selected_button.setEnabled(False)
            self.delete_selected_queue_button.setEnabled(False)
            return

        item = queue[queue_index]
        active_bed = self.bed_runtime(bed_number)["current_phase"] != "ready"
        self.selected_queue_label.setText(
            f"{self.bed_label_for_number(bed_number)} | {item['customer_name']} | {int(item['minutes'])} min"
        )
        room_ready = bool(item.get("room_ready"))
        self.start_selected_button.setText("Sent To Room" if room_ready else "Send To Room")
        self.start_selected_button.setEnabled(queue_index == 0 and not active_bed and not room_ready)
        self.move_selected_button.setEnabled(True)
        self.delete_selected_queue_button.setEnabled(True)

    def start_selected_queue_item(self):
        ref = self.selected_queue_reference()
        if ref is None:
            return
        bed_number, queue_index = ref
        if queue_index != 0:
            QMessageBox.information(self, "Next Customer Only", "Only the next queued customer for a bed can be sent to the room.")
            return
        queue = self.bed_queue(bed_number)
        if not queue:
            return
        if self.bed_runtime(bed_number)["current_phase"] != "ready":
            QMessageBox.information(self, "Bed Busy", "Wait for the current session to finish before sending the next customer to the room.")
            return
        if uses_external_start_flow(bed_number):
            self.activate_room_ready_booking_for_external_start(bed_number, send_trigger_pulse=False)
            return
        queue[0]["room_ready"] = True
        queue[0]["room_ready_activated_at"] = time.time()
        self.refresh_view()

    def move_selected_queue_item(self):
        ref = self.selected_queue_reference()
        if ref is None:
            return
        bed_number, queue_index = ref
        queue = self.bed_queue(bed_number)
        if queue_index < 0 or queue_index >= len(queue):
            return

        customer_name = str(queue[queue_index].get("customer_name") or "this customer")
        decision = QMessageBox(self)
        decision.setWindowTitle("Move Selected")
        decision.setText(f"What would you like to do with {customer_name}?")
        decision.setInformativeText("Move them to another bed or delete them from the queue.")
        move_button = decision.addButton("Move Bed", QMessageBox.ButtonRole.AcceptRole)
        delete_button = decision.addButton("Delete From Queue", QMessageBox.ButtonRole.DestructiveRole)
        decision.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        delete_button.setProperty("variant", "danger")
        decision.exec()

        clicked = decision.clickedButton()
        if clicked is move_button:
            self.change_machine_for_queue_item(bed_number, queue_index)
        elif clicked is delete_button:
            queue.pop(queue_index)
        self.refresh_view()

    def delete_selected_queue_item(self):
        ref = self.selected_queue_reference()
        if ref is None:
            return
        bed_number, queue_index = ref
        queue = self.bed_queue(bed_number)
        if queue_index < 0 or queue_index >= len(queue):
            return
        queue.pop(queue_index)
        self.refresh_view()

    def refresh_view(self):
        rows = self.build_beds_data()

        active_beds = [
            bed_number
            for bed_number in range(1, 9)
            if self.bed_runtime(bed_number)["current_phase"] != "ready"
        ]
        queued_total = sum(len(self.bed_queue(bed_number)) for bed_number in range(1, 9))
        queue_table_rows = self.queue_table_rows()

        if queue_table_rows:
            first = queue_table_rows[0]
            self.queue_summary_label.setText(
                f"{first['customer_name']} is next for {self.bed_label_for_number(first['bed_number'])}."
            )
        else:
            self.queue_summary_label.setText("No customers are currently waiting.")

        self.queue_table.setRowCount(len(queue_table_rows))
        for row_index, queue_row in enumerate(queue_table_rows):
            values = [
                self.bed_label_for_number(queue_row["bed_number"]),
                queue_row["customer_name"],
                str(queue_row["minutes"]),
                queue_row["payment_method"],
                queue_row["wait_text"],
                queue_row["status_text"],
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, (queue_row["bed_number"], queue_row["queue_index"]))
                self.queue_table.setItem(row_index, column, item)
        self.queue_table.resizeColumnsToContents()

        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for index, row in enumerate(rows):
            bed_number = index + 1
            card = CardFrame()
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(10, 10, 10, 10)
            card_layout.setSpacing(4)

            title = QLabel(sunbed_display_text(row, bed_number))
            title.setStyleSheet("font-size:15px; font-weight:800; color:#0f4c6e;")
            card_layout.addWidget(title)

            status_row = QHBoxLayout()
            status_row.setSpacing(6)
            for text in (
                f"Status: {row['status_text']}",
                f"Live: {row['time_left']}",
                f"Queue: {row['queue_count']}",
                f"Wait: {row['wait_text']}",
            ):
                pill = QLabel(text)
                pill.setStyleSheet(
                    "font-size:10px; color:#345167; background:#eef5fa; border:1px solid #d7e4ef; border-radius:10px; padding:4px 7px;"
                )
                status_row.addWidget(pill)
            status_row.addStretch(1)
            card_layout.addLayout(status_row)

            bed_queue_items = self.bed_queue(bed_number)
            room_ready_item = bed_queue_items[0] if bed_queue_items and bool(bed_queue_items[0].get("room_ready")) else None
            if row["pending_sale"] is not None:
                next_name = row["pending_sale"].customer_name if hasattr(row["pending_sale"], "customer_name") else ""
            elif room_ready_item is not None:
                next_name = str(room_ready_item["customer_name"])
            else:
                next_name = ""

            if next_name:
                next_up_label = QLabel(f"Next Up: {next_name}")
                next_up_label.setStyleSheet("font-size:12px; font-weight:700; color:#0f766e;")
                card_layout.addWidget(next_up_label)

            if room_ready_item is not None and row["queue_preview"] and row["queue_preview"] != "No queue":
                queue_preview = QLabel(row["queue_preview"])
                queue_preview.setStyleSheet("font-size:10px; color:#7b8794;")
                queue_preview.setWordWrap(True)
                card_layout.addWidget(queue_preview)

            image_progress_row = QHBoxLayout()
            image_progress_row.setSpacing(8)

            image_label = self.build_image_label(row["image_path"], row["bed_type"])
            image_progress_row.addWidget(image_label, 1)

            progress_bar = QProgressBar()
            progress_bar.setOrientation(Qt.Orientation.Vertical)
            progress_bar.setRange(0, 100)
            progress_bar.setValue(int(row["progress_percent"]))
            progress_bar.setFormat(f"{row['progress_percent']}%")
            progress_bar.setFixedSize(34, 88)
            progress_bar.setStyleSheet(
                f"QProgressBar::chunk {{ background: {row['progress_color']}; border-radius: 8px; }}"
            )
            image_progress_row.addWidget(progress_bar)
            card_layout.addLayout(image_progress_row, 1)

            is_active_bed = self.bed_runtime(bed_number)["current_phase"] != "ready"
            action_shell = QWidget()
            action_shell.setFixedHeight(40)
            action_row = QHBoxLayout(action_shell)
            action_row.setContentsMargins(0, 0, 0, 0)
            action_row.setSpacing(8)

            if is_active_bed:
                started_button_text = "Customer Start"
                started_button_variant = "warning"
                started_handler = lambda _checked=False, current_bed=bed_number: self.customer_started(current_bed)
                started_visible = self.bed_runtime(bed_number)["current_phase"] == "prep"
                if uses_external_start_flow(bed_number):
                    waiting_for_feedback_start = bool(self.bed_runtime(bed_number).get("waiting_for_feedback_start"))
                    feedback_wait_started_at = self.bed_runtime(bed_number).get("feedback_wait_started_at")
                    if waiting_for_feedback_start and not feedback_wait_started_at:
                        started_button_text = "Start Bed"
                        started_button_variant = "success"
                        started_handler = lambda _checked=False, current_bed=bed_number: self.send_external_start_trigger(current_bed)
                        started_visible = True
                    else:
                        started_visible = False
                started_button = QPushButton(started_button_text)
                started_button.setProperty("variant", started_button_variant)
                started_button.setProperty("compact", "true")
                started_button.setMaximumWidth(112)
                started_button.setVisible(started_visible)
                started_button.clicked.connect(started_handler)
                stop_button = QPushButton("Stop Bed")
                stop_button.setProperty("variant", "danger")
                stop_button.setProperty("compact", "true")
                stop_button.setMaximumWidth(102)
                stop_button.setEnabled(not bool(self.bed_runtime(bed_number).get("cooldown_locked")))
                stop_button.clicked.connect(
                    lambda _checked=False, current_bed=bed_number: self.stop_room_to_cooldown(current_bed)
                )
                action_row.addWidget(started_button)
                action_row.addWidget(stop_button)
            elif row["queue_count"] > 0 and room_ready_item is not None:
                start_button = QPushButton("Start")
                start_button.setProperty("variant", "success")
                start_button.setProperty("compact", "true")
                start_button.setMaximumWidth(82)
                start_button.setEnabled(self.bed_runtime(bed_number)["current_phase"] == "ready")
                start_button.clicked.connect(
                    lambda _checked=False, current_bed=bed_number: self.start_pending_booking(current_bed)
                )
                change_button = QPushButton("Change Bed")
                change_button.setProperty("variant", "secondary")
                change_button.setProperty("compact", "true")
                change_button.setMaximumWidth(108)
                change_button.clicked.connect(
                    lambda _checked=False, current_bed=bed_number: self.change_machine_for_queue_item(current_bed, 0)
                )
                action_row.addWidget(start_button)
                action_row.addWidget(change_button)
            elif row["queue_count"] > 0:
                waiting_label = QLabel("Waiting In Queue")
                waiting_label.setStyleSheet("font-size:11px; font-weight:700; color:#c26d00;")
                action_row.addWidget(waiting_label)
            else:
                placeholder = QLabel("")
                placeholder.setFixedHeight(1)
                action_row.addWidget(placeholder)

            action_row.addStretch(1)
            card_layout.addWidget(action_shell)

            columns = 2
            self.cards_layout.addWidget(card, index // columns, index % columns)

        self.update_queue_actions()

    def start_pending_booking(self, bed_number: int = 1):
        state = self.bed_runtime(bed_number)
        if state["current_phase"] != "ready":
            return
        if not self.bed_queue(bed_number):
            return
        first_item = self.bed_queue(bed_number)[0]
        if not bool(first_item.get("room_ready")):
            QMessageBox.information(self, "Send To Room First", "Use Send To Room in the client queue before starting this bed.")
            return
        if uses_external_start_flow(bed_number):
            self.activate_room_ready_booking_for_external_start(bed_number, send_trigger_pulse=False)
            return
        armed_started_at = float(first_item.get("room_ready_activated_at") or 0) or time.time()
        pending_sale = self.pending_sale_from_queue_item(self.bed_queue(bed_number).pop(0))
        active_bed_number = int(pending_sale.target_bed_number or bed_number or 1)
        state["active_customer_name"] = pending_sale.customer_name
        state["active_customer_account"] = pending_sale.customer_account
        state["active_payment_method"] = pending_sale.payment_method.replace("_", " ").title()
        state["active_minutes"] = pending_sale.minutes
        state["active_sale"] = pending_sale
        state["relay_armed_started_at"] = armed_started_at
        state["relay_fail_safe_deadline_at"] = armed_started_at + self.room_activation_fail_safe_timeout_seconds(
            pending_sale.minutes
        )

        prep_seconds = int(self.runtime_state.get("prep_minutes", 0)) * 60
        cooldown_seconds = int(self.runtime_state.get("cooldown_minutes", 0)) * 60
        tanning_seconds = pending_sale.minutes * 60

        if pending_sale.resume_phase:
            state["tanning_seconds_remaining"] = int(pending_sale.tanning_seconds_remaining or 0)
            state["cooldown_seconds_remaining"] = int(pending_sale.cooldown_seconds_remaining or 0)
            if pending_sale.resume_phase == "tanning":
                if not relay_on_for_bed(active_bed_number):
                    QMessageBox.warning(
                        self,
                        "Relay Not Triggered",
                        f"The sale is ready, but the Pi relay command for Bed {active_bed_number} did not run. The bed has not been started.",
                    )
                    return
            self.start_phase(active_bed_number, pending_sale.resume_phase, int(pending_sale.remaining_seconds or 0))
        else:
            state["tanning_seconds_remaining"] = tanning_seconds
            state["cooldown_seconds_remaining"] = cooldown_seconds
            if prep_seconds > 0:
                self.start_phase(active_bed_number, "prep", prep_seconds)
            else:
                if not relay_on_for_bed(active_bed_number):
                    QMessageBox.warning(
                        self,
                        "Relay Not Triggered",
                        f"The sale is ready, but the Pi relay command for Bed {active_bed_number} did not run. The bed has not been started.",
                    )
                    return
                state["tanning_seconds_remaining"] = 0
                self.start_phase(active_bed_number, "tanning", tanning_seconds)
        self.refresh_view()

    def start_room_ready_booking_from_feedback(self, bed_number: int):
        # Rev2 feedback confirms switched voltage state only. It must not start paid tanning time.
        return

    def customer_started(self, bed_number: int):
        state = self.bed_runtime(bed_number)
        if state["current_phase"] != "prep":
            return
        tanning_seconds = int(state.get("tanning_seconds_remaining") or 0)
        if tanning_seconds <= 0:
            self.advance_bed_to_next_phase(bed_number)
            self.refresh_view()
            return
        if not uses_external_start_flow(bed_number) and not relay_on_for_bed(bed_number):
            QMessageBox.warning(
                self,
                "Relay Not Triggered",
                f"The bed start signal was accepted, but the Pi relay command for Bed {bed_number} did not run.",
            )
            return
        state["waiting_for_feedback_start"] = False
        state["feedback_wait_started_at"] = None
        state["tanning_seconds_remaining"] = 0
        self.start_phase(bed_number, "tanning", tanning_seconds)
        nudge_active_phase_display(state)
        self.refresh_view()

    def end_tanning_phase(self, bed_number: int):
        relay_off_for_bed(bed_number)
        state = self.bed_runtime(bed_number)
        state["waiting_for_feedback_start"] = False
        state["feedback_wait_started_at"] = None
        cooldown_seconds = int(state.get("cooldown_seconds_remaining") or 0)
        state["cooldown_seconds_remaining"] = 0
        if cooldown_seconds > 0:
            self.start_phase(bed_number, "cooldown", cooldown_seconds)
        else:
            self.finish_bed_session(bed_number)
        self.refresh_view()

    def stop_room_to_cooldown(self, bed_number: int):
        state = self.bed_runtime(bed_number)
        if bool(state.get("cooldown_locked")):
            return
        current_phase = state.get("current_phase")
        if current_phase == "ready":
            return
        if current_phase == "cooldown":
            return

        if current_phase == "tanning":
            state["tanning_seconds_remaining"] = 0
            state["cooldown_locked"] = True
            state["cooldown_seconds_remaining"] = int(self.runtime_state.get("cooldown_minutes", 0)) * 60
            self.end_tanning_phase(bed_number)
            return
        state["tanning_seconds_remaining"] = 0
        cooldown_seconds = int(self.runtime_state.get("cooldown_minutes", 0)) * 60
        state["cooldown_seconds_remaining"] = 0
        state["cooldown_locked"] = True
        if cooldown_seconds > 0:
            self.start_phase(bed_number, "cooldown", cooldown_seconds)
        else:
            self.clear_bed_runtime(bed_number)
        self.refresh_view()

    def global_emergency_stop(self):
        relay_off_all()
        for bed_number in range(1, 9):
            self.clear_bed_runtime(bed_number)
        for bed_number in range(1, 9):
            queue = self.bed_queue(bed_number)
            if queue and bool(queue[0].get("room_ready")):
                queue[0]["room_ready"] = False
                queue[0]["room_ready_activated_at"] = None
        self.refresh_view()
        QMessageBox.warning(
            self,
            "Global Emergency Stop",
            "All 8 relays have been shut off immediately.",
        )

    def enforce_room_ready_safety(self):
        now = time.time()
        for bed_number in range(1, 9):
            queue = self.bed_queue(bed_number)
            if not queue:
                continue
            first_item = queue[0]
            if not bool(first_item.get("room_ready")):
                continue
            activated_at = float(first_item.get("room_ready_activated_at") or 0)
            if activated_at <= 0:
                first_item["room_ready_activated_at"] = now
                continue
            booked_minutes = max(0, int(first_item.get("minutes") or 0))
            room_ready_timeout_seconds = self.room_activation_fail_safe_timeout_seconds(booked_minutes)
            if now - activated_at < room_ready_timeout_seconds:
                continue
            relay_off_for_bed(bed_number)
            first_item["room_ready"] = False
            first_item["room_ready_activated_at"] = None
            self.refresh_view()
            QMessageBox.warning(
                self,
                "Room Timed Out",
                f"Bed {bed_number} was sent to room but not started within the allowed window. The relay has been shut off for safety.",
            )
            return

    def enforce_active_relay_fail_safe(self, bed_number: int) -> bool:
        state = self.bed_runtime(bed_number)
        if state["current_phase"] == "ready":
            return False
        fail_safe_deadline_at = float(state.get("relay_fail_safe_deadline_at") or 0)
        if fail_safe_deadline_at <= 0 or time.time() < fail_safe_deadline_at:
            return False
        relay_off_for_bed(bed_number)
        self.clear_bed_runtime(bed_number)
        self.refresh_view()
        QMessageBox.warning(
            self,
            "Relay Fail-Safe Shut Down",
            f"Bed {bed_number} stayed active beyond prep, tanning, and cooldown. The relay has been shut off for safety.",
        )
        return True

    def enforce_feedback_wait_safety(self, bed_number: int) -> bool:
        state = self.bed_runtime(bed_number)
        if not bool(state.get("waiting_for_feedback_start")):
            return False
        started_at = float(state.get("feedback_wait_started_at") or 0)
        if started_at <= 0:
            return False
        if time.time() - started_at < EXTERNAL_START_FEEDBACK_TIMEOUT_SECONDS:
            return False
        relay_off_for_bed(bed_number)
        self.clear_bed_runtime(bed_number)
        self.refresh_view()
        QMessageBox.warning(
            self,
            "Start Feedback Timeout",
            f"Bed {bed_number} did not confirm the start signal in time. The relay has been shut off for safety.",
        )
        return True

    def tick(self):
        self.enforce_room_ready_safety()
        any_active = False
        for bed_number in range(1, 9):
            state = self.bed_runtime(bed_number)
            previous_feedback_active = self.feedback_last_states.get(bed_number)
            current_feedback_active = read_feedback_gpio_active(bed_number)
            self.feedback_last_states[bed_number] = current_feedback_active

            if state["current_phase"] == "ready":
                continue
            any_active = True
            if self.enforce_active_relay_fail_safe(bed_number):
                continue
            if state["current_phase"] == "prep" and bool(state.get("waiting_for_feedback_start")):
                if current_feedback_active is True and previous_feedback_active is not True:
                    state["waiting_for_feedback_start"] = False
                    state["feedback_wait_started_at"] = None
                    self.customer_started(bed_number)
                    continue
                if state.get("feedback_wait_started_at") and self.enforce_feedback_wait_safety(bed_number):
                    continue
                continue
            if state["time_left"] > 0:
                state["time_left"] -= 1
                state["phase_elapsed_seconds"] += 1
            if state["time_left"] <= 0:
                self.advance_bed_to_next_phase(bed_number)
                continue
        if not any_active:
            return
        self.refresh_view()

class LoginPage(QWidget):
    def __init__(self, repo: TillRepository, on_login, on_device_setup):
        super().__init__()
        self.repo = repo
        self.on_login = on_login
        self.on_device_setup = on_device_setup
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        background = QWidget()
        background.setStyleSheet(
            "background:qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #f7fbff, stop:0.55 #eef5fb, stop:1 #e8f0f8);"
        )
        outer.addWidget(background)

        shell = QVBoxLayout(background)
        shell.setContentsMargins(44, 28, 44, 28)
        shell.setSpacing(18)

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(0)
        top_row.addWidget(build_logo_mark(repo, 220, 140), 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        top_row.addStretch(1)
        shell.addLayout(top_row)

        card_wrap = QWidget()
        card_wrap_layout = QVBoxLayout(card_wrap)
        card_wrap_layout.setContentsMargins(0, 0, 0, 0)
        card_wrap_layout.setSpacing(0)
        card_wrap_layout.addStretch(1)

        card = CardFrame(hero=True)
        card.setMaximumWidth(520)
        card.setMinimumWidth(440)
        card_wrap_layout.addWidget(card, 0, Qt.AlignmentFlag.AlignCenter)
        card_wrap_layout.addStretch(1)
        shell.addWidget(card_wrap, 1)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(34, 34, 34, 34)
        layout.setSpacing(18)

        self.title_label = QLabel()
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label.setStyleSheet("font-size:34px; font-weight:600; color:#102f45;")
        layout.addWidget(self.title_label)

        subtitle = QLabel("Staff Login")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet("font-size:24px; font-weight:700; color:#163d59;")
        layout.addWidget(subtitle)

        form = QVBoxLayout()
        form.setSpacing(14)
        self.pin_input = QLineEdit()
        self.pin_input.setPlaceholderText("Enter PIN")
        self.pin_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.pin_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pin_input.setMinimumHeight(58)
        self.pin_input.setStyleSheet("font-size:24px; font-weight:500; letter-spacing:2px;")
        self.pin_input.returnPressed.connect(self.try_login)
        form.addWidget(self.pin_input)

        login_button = QPushButton("Login")
        login_button.setProperty("variant", "success")
        login_button.setMinimumHeight(58)
        login_button.setCursor(Qt.CursorShape.PointingHandCursor)
        login_button.setDefault(True)
        login_button.setStyleSheet(
            "font-size:20px; font-weight:700; "
            "background:qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #3d9d58, stop:1 #28733d); "
            "border:1px solid #215f33; color:white;"
        )
        login_button.clicked.connect(lambda checked=False: self.try_login())
        form.addWidget(login_button)

        setup_button = QPushButton("Device Setup")
        setup_button.setProperty("variant", "secondary")
        setup_button.setMinimumHeight(48)
        setup_button.setCursor(Qt.CursorShape.PointingHandCursor)
        setup_button.clicked.connect(self.on_device_setup)
        form.addWidget(setup_button)
        layout.addLayout(form)
        layout.addStretch(1)
        self.refresh_branding()
        QTimer.singleShot(0, self.pin_input.setFocus)

    def refresh_branding(self):
        branding = self.repo.branding_settings()
        self.title_label.setText(str(branding.get("business_name") or "Salon Max"))

    def try_login(self):
        staff = self.repo.authenticate_staff(self.pin_input.text())
        if staff is None:
            self.pin_input.clear()
            QMessageBox.warning(self, "Login Failed", "Invalid PIN.")
            return
        self.pin_input.clear()
        self.on_login(staff)


class BusinessLockedPage(QWidget):
    def __init__(self, repo: TillRepository, on_refresh_status, on_device_setup):
        super().__init__()
        self.repo = repo
        self.on_refresh_status = on_refresh_status
        self.on_device_setup = on_device_setup
        self.auto_refresh_timer = QTimer(self)
        self.auto_refresh_timer.setInterval(15000)
        self.auto_refresh_timer.timeout.connect(self.on_refresh_status)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        background = QWidget()
        background.setStyleSheet(
            "background:qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2a0f12, stop:0.55 #46181d, stop:1 #1f2937);"
        )
        outer.addWidget(background)

        shell = QVBoxLayout(background)
        shell.setContentsMargins(44, 28, 44, 28)
        shell.setSpacing(18)

        top_row = QHBoxLayout()
        top_row.addWidget(build_logo_mark(repo, 220, 140), 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        top_row.addStretch(1)
        shell.addLayout(top_row)

        card_wrap = QWidget()
        card_wrap_layout = QVBoxLayout(card_wrap)
        card_wrap_layout.setContentsMargins(0, 0, 0, 0)
        card_wrap_layout.addStretch(1)

        card = CardFrame(hero=True)
        card.setMaximumWidth(620)
        card_wrap_layout.addWidget(card, 0, Qt.AlignmentFlag.AlignCenter)
        card_wrap_layout.addStretch(1)
        shell.addWidget(card_wrap, 1)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(34, 34, 34, 34)
        layout.setSpacing(18)

        self.title_label = QLabel("Trading Locked")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label.setStyleSheet("font-size:34px; font-weight:700; color:#7f1d1d;")
        layout.addWidget(self.title_label)

        self.business_label = QLabel()
        self.business_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.business_label.setStyleSheet("font-size:26px; font-weight:700; color:#163d59;")
        layout.addWidget(self.business_label)

        self.message_label = QLabel()
        self.message_label.setWordWrap(True)
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message_label.setStyleSheet("font-size:18px; color:#28465b; line-height:1.4;")
        layout.addWidget(self.message_label)

        refresh_button = QPushButton("Check Status Again")
        refresh_button.setProperty("variant", "warning")
        refresh_button.setMinimumHeight(54)
        refresh_button.clicked.connect(self.on_refresh_status)
        layout.addWidget(refresh_button)

        setup_button = QPushButton("Device Setup")
        setup_button.setProperty("variant", "secondary")
        setup_button.setMinimumHeight(48)
        setup_button.clicked.connect(self.on_device_setup)
        layout.addWidget(setup_button)
        layout.addStretch(1)

    def refresh_state(self, message: str = ""):
        branding = self.repo.branding_settings()
        self.business_label.setText(str(branding.get("business_name") or "Salon Max"))
        self.message_label.setText(message or "This salon cannot trade right now.")

    def showEvent(self, event):
        super().showEvent(event)
        if not self.auto_refresh_timer.isActive():
            self.auto_refresh_timer.start()

    def hideEvent(self, event):
        if self.auto_refresh_timer.isActive():
            self.auto_refresh_timer.stop()
        super().hideEvent(event)


class OpenTillPage(QWidget):
    def __init__(self, repo: TillRepository, session: AppSession, on_opened, on_logout, on_show_sunbed_status, on_check_access):
        super().__init__()
        self.repo = repo
        self.session = session
        self.on_opened = on_opened
        self.on_logout = on_logout
        self.on_show_sunbed_status = on_show_sunbed_status
        self.on_check_access = on_check_access

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 16, 24, 24)
        outer.setSpacing(10)

        outer.addWidget(
            build_top_strip(
                repo,
                "open",
                [],
                [
                    ("Pair Device", self.open_pair_device_dialog, "warning"),
                    ("Sunbed Status", self.on_show_sunbed_status, "accent"),
                    ("Logout", self.on_logout, "secondary"),
                ],
            )
        )

        card = CardFrame(hero=True)
        outer.addWidget(card, alignment=Qt.AlignmentFlag.AlignTop)
        layout = QFormLayout(card)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        self.opening_float = QDoubleSpinBox()
        self.opening_float.setMaximum(99999.99)
        self.opening_float.setPrefix("GBP ")
        self.opening_float.setDecimals(2)
        self.selected_site_id: Optional[int] = None
        self.selected_terminal_id: Optional[int] = None

        self.staff_name_label = QLabel("Not logged in")
        self.site_name_label = QLabel("Not set")
        self.terminal_name_label = QLabel("Not set")
        self.site_name_label.setProperty("role", "section_title")
        self.terminal_name_label.setProperty("role", "section_title")
        layout.addRow("Staff", self.staff_name_label)
        layout.addRow("Site", self.site_name_label)
        layout.addRow("Terminal", self.terminal_name_label)
        layout.addRow("Opening Float", self.opening_float)

        button = QPushButton("Open Till")
        button.setProperty("variant", "success")
        button.clicked.connect(self.open_till)
        layout.addRow("", button)

        self.refresh_till_allocation()
        self.refresh_staff_display()
        outer.addStretch(1)

    def owner_can_pair(self):
        staff = self.session.staff_row
        if staff is None:
            return False
        current = self.repo.fetch_one(
            "select role,is_active from staff_users where id=?", (int(staff["id"]),)
        )
        return bool(current and current["is_active"]
                    and str(current["role"]).strip().lower() == "owner")

    def refresh_pair_device_access(self):
        allowed = self.owner_can_pair()
        for button in self.findChildren(QPushButton):
            if button.text() == "Pair Device":
                button.setVisible(allowed)
                button.setEnabled(allowed)

    def open_pair_device_dialog(self):
        if not self.owner_can_pair():
            QMessageBox.warning(self, "Access restricted", "Device pairing requires an owner sign-in.")
            return
        dialog = PairDeviceDialog(self.repo, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.paired_data:
            return
        paired = dialog.paired_data
        QMessageBox.information(
            self,
            "Pairing Complete",
            f"This Pi is now linked to {paired.get('business_name', 'business')} / {paired.get('site_name', 'site')} / {paired.get('terminal_name', 'terminal')}.",
        )

    def refresh_staff_display(self):
        self.refresh_pair_device_access()
        if self.session.staff_row is not None:
            self.staff_name_label.setText(self.session.staff_row["name"])
        else:
            self.staff_name_label.setText("Not logged in")

    def refresh_till_allocation(self):
        site_row, terminal_row = self.repo.default_site_and_terminal()
        self.selected_site_id = int(site_row["id"]) if site_row else None
        self.selected_terminal_id = int(terminal_row["id"]) if terminal_row else None
        self.site_name_label.setText(site_row["name"] if site_row else "Not configured")
        self.terminal_name_label.setText(terminal_row["name"] if terminal_row else "Not configured")
        if self.selected_site_id and self.selected_terminal_id:
            self.opening_float.setValue(
                self.repo.opening_float_default(self.selected_site_id, self.selected_terminal_id)
            )
        else:
            self.opening_float.setValue(0.0)

    def open_till(self):
        site_id = self.selected_site_id
        terminal_id = self.selected_terminal_id
        if not site_id or not terminal_id:
            QMessageBox.warning(self, "Till Not Configured", "No active site/terminal is configured for this till.")
            return
        if not self.on_check_access():
            QMessageBox.warning(self, "Trading Locked", "This salon is suspended or paused, so the till cannot be opened.")
            return

        self.session.till_session = self.repo.open_till(
            int(site_id),
            int(terminal_id),
            int(self.session.staff_row["id"]),
            float(self.opening_float.value()),
        )
        bootstrap_local_platform_identity(
            self.repo,
            site_id=int(site_id),
            terminal_id=int(terminal_id),
            terminal_name=self.terminal_name_label.text(),
        )
        self.on_opened()


class CustomerSummaryPage(QWidget):
    def __init__(
        self,
        repo: TillRepository,
        session: AppSession,
        on_back,
        on_sell_tanning,
        on_sell_course,
        on_sell_products,
        on_show_history,
        on_show_sunbed_status,
        on_show_totals,
    ):
        super().__init__()
        self.repo = repo
        self.session = session
        self.on_back = on_back
        self.on_sell_tanning = on_sell_tanning
        self.on_sell_course = on_sell_course
        self.on_sell_products = on_sell_products
        self.on_show_history = on_show_history
        self.on_show_sunbed_status = on_show_sunbed_status
        self.on_show_totals = on_show_totals
        self.customer_id: Optional[int] = None
        self.current_customer = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        outer.addWidget(
            build_top_strip(
                repo,
                "customer",
                [
                    ("search", "Client Search", self.on_back, True),
                    ("customer", "Customer", None, True),
                    (
                        "tanning",
                        "Tanning Sale",
                        lambda: self.customer_id and self.on_sell_tanning(self.customer_id),
                        True,
                    ),
                    ("queue", "Sunbed Queue", self.on_show_sunbed_status, True),
                    ("cashup", "Cash Up", self.on_show_totals, True),
                ],
                [("Back To Search", self.on_back, "secondary")],
            )
        )

        content = QHBoxLayout()
        content.setSpacing(16)

        left_column = QVBoxLayout()
        left_column.setSpacing(16)

        self.details_card = CardFrame(hero=True)
        details_layout = QVBoxLayout(self.details_card)
        details_layout.setContentsMargins(20, 20, 20, 20)
        self.customer_name = QLabel("")
        self.customer_name.setProperty("role", "section_title")
        details_layout.addWidget(self.customer_name)
        self.customer_meta = QLabel("")
        self.customer_meta.setProperty("role", "muted")
        self.customer_meta.setWordWrap(True)
        details_layout.addWidget(self.customer_meta)
        self.account_snapshot = QLabel("")
        self.account_snapshot.setStyleSheet("font-size:14px; font-weight:700; color:#0f4c6e;")
        details_layout.addWidget(self.account_snapshot)
        left_column.addWidget(self.details_card)

        summary_row = QHBoxLayout()
        summary_row.setSpacing(12)

        self.minutes_label = QLabel("0")
        self.minutes_label.setProperty("role", "metric")
        self.last_sale_label = QLabel("None")
        self.last_sale_label.setProperty("role", "metric")

        summary_row.addWidget(self.metric_card("Account Minutes", self.minutes_label), 1)
        summary_row.addWidget(self.metric_card("Last Seen", self.last_sale_label), 1)

        info_card = CardFrame()
        info_layout = QVBoxLayout(info_card)
        info_layout.setContentsMargins(16, 16, 16, 16)
        info_layout.setSpacing(10)
        info_title = QLabel("Account Control")
        info_title.setProperty("role", "section_title")
        info_layout.addWidget(info_title)

        self.personal_data_label = QLabel("")
        self.personal_data_label.setProperty("role", "muted")
        self.personal_data_label.setWordWrap(True)
        info_layout.addWidget(self.personal_data_label)

        self.history_hint_label = QLabel("")
        self.history_hint_label.setStyleSheet("font-size:13px; color:#345167;")
        self.history_hint_label.setWordWrap(True)
        info_layout.addWidget(self.history_hint_label)

        self.recent_visit_label = QLabel("")
        self.recent_visit_label.setWordWrap(True)
        info_layout.addWidget(self.recent_visit_label)
        summary_row.addWidget(info_card, 2)
        left_column.addLayout(summary_row)

        actions_card = CardFrame()
        actions = QGridLayout(actions_card)
        actions.setContentsMargins(16, 16, 16, 16)
        actions.setHorizontalSpacing(10)
        actions.setVerticalSpacing(10)
        tanning_button = QPushButton("Sell Tanning")
        tanning_button.setProperty("variant", "secondary")
        tanning_button.clicked.connect(lambda: self.customer_id and self.on_sell_tanning(self.customer_id))
        actions.addWidget(tanning_button, 0, 0)

        course_button = QPushButton("Sell Sunbed Course")
        course_button.setProperty("variant", "secondary")
        course_button.clicked.connect(lambda: self.customer_id and self.on_sell_course(self.customer_id))
        actions.addWidget(course_button, 0, 1)

        products_button = QPushButton("Sell Creams / Products")
        products_button.setProperty("variant", "secondary")
        products_button.clicked.connect(lambda: self.customer_id and self.on_sell_products(self.customer_id))
        actions.addWidget(products_button, 1, 0)

        self.sunbed_button = QPushButton("Sunbed Status")
        self.sunbed_button.setProperty("variant", "secondary")
        self.sunbed_button.clicked.connect(self.on_show_sunbed_status)
        actions.addWidget(self.sunbed_button, 1, 1)

        history_button = QPushButton("Transaction History")
        history_button.setProperty("variant", "secondary")
        history_button.clicked.connect(lambda: self.customer_id and self.on_show_history(self.customer_id))
        actions.addWidget(history_button, 2, 0, 1, 2)

        edit_button = QPushButton("Edit Customer Details")
        edit_button.setProperty("variant", "secondary")
        edit_button.clicked.connect(self.edit_customer)
        actions.addWidget(edit_button, 3, 0, 1, 2)
        left_column.addWidget(actions_card)
        content.addLayout(left_column, 1)

        table_card = CardFrame()
        table_layout = QVBoxLayout(table_card)
        table_layout.setContentsMargins(16, 16, 16, 16)
        table_title = QLabel("Recent Transactions")
        table_title.setProperty("role", "section_title")
        table_layout.addWidget(table_title)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Transaction #", "Type", "Total", "Payment", "Date"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        table_layout.addWidget(self.table)
        content.addWidget(table_card, 1)
        outer.addLayout(content, 1)

    def metric_card(self, label_text: str, metric_label: QLabel):
        card = CardFrame()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 16, 16, 16)
        label = QLabel(label_text)
        label.setProperty("role", "muted")
        layout.addWidget(label)
        layout.addWidget(metric_label)
        return card

    def load_customer(self, customer_id: int):
        self.customer_id = customer_id
        customer = self.repo.get_customer(customer_id)
        if customer is None:
            QMessageBox.warning(self, "Missing Customer", "This customer no longer exists.")
            self.on_back()
            return
        self.current_customer = customer

        self.customer_name.setText(f"{customer['first_name']} {customer['last_name']}")
        birthday_display = self.repo.birthday_for_display(customer["birthday"]) or "-"
        age_display = format_age_label(str(customer["birthday"] or ""))
        self.customer_meta.setText(
            f"Customer #{customer['customer_number']} | DOB {birthday_display} | Age {age_display} | Account {customer['account_number'] or '-'}"
        )
        self.account_snapshot.setText(
            f"Course minutes ready: {int(customer['package_minutes'] or 0)}"
        )
        self.minutes_label.setText(str(int(customer["package_minutes"] or 0)))
        self.personal_data_label.setText(
            f"Age: {age_display}\nPhone: {customer['phone'] or '-'}\nEmail: {customer['email'] or '-'}"
        )

        rows = self.repo.recent_transactions(customer_id, limit=10)
        if rows:
            last_sale_display = format_timestamp_for_display(rows[0]["created_at"])
            self.last_sale_label.setText(last_sale_display or str(rows[0]["created_at"]))
            recent_visit = transaction_within_last_24_hours(rows[0]["created_at"])
            if recent_visit:
                self.last_sale_label.setStyleSheet("font-size:20px; font-weight:800; color:#c62828;")
                self.history_hint_label.setStyleSheet("font-size:13px; color:#c62828; font-weight:800;")
                self.recent_visit_label.setText("Customer visited within the last 24 hours.")
                self.recent_visit_label.setStyleSheet("font-size:14px; color:#c62828; font-weight:800;")
            else:
                self.last_sale_label.setStyleSheet("font-size:20px; font-weight:800; color:#0f4c6e;")
                self.history_hint_label.setStyleSheet("font-size:13px; color:#345167;")
                self.recent_visit_label.setText("")
                self.recent_visit_label.setStyleSheet("font-size:13px; color:#345167;")
            self.history_hint_label.setText(
                f"Last transaction was {rows[0]['transaction_type'].replace('_', ' ').title()} on {last_sale_display or rows[0]['created_at']}."
            )
        else:
            self.last_sale_label.setText("None")
            self.last_sale_label.setStyleSheet("font-size:20px; font-weight:800; color:#0f4c6e;")
            self.history_hint_label.setStyleSheet("font-size:13px; color:#345167;")
            self.history_hint_label.setText("No transactions yet. First sale can be tanning, course, or products.")
            self.recent_visit_label.setText("")
            self.recent_visit_label.setStyleSheet("font-size:13px; color:#345167;")
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = [
                row["transaction_number"],
                row["transaction_type"].replace("_", " ").title(),
                f"GBP {float(row['total_amount'] or 0):.2f}",
                row["payment_method"].replace("_", " ").title(),
                format_timestamp_for_display(row["created_at"]),
            ]
            for column, value in enumerate(values):
                self.table.setItem(row_index, column, QTableWidgetItem(str(value)))
        self.table.resizeColumnsToContents()

    def edit_customer(self):
        if self.current_customer is None or self.customer_id is None:
            return
        dialog = EditCustomerDialog(self.repo, self.current_customer, self)
        if dialog.exec():
            self.load_customer(self.customer_id)


class CustomerSearchPage(QWidget):
    def __init__(
        self,
        repo: TillRepository,
        session: AppSession,
        on_open_customer,
        on_show_totals,
        on_logout,
        on_show_sunbed_status,
    ):
        super().__init__()
        self.repo = repo
        self.session = session
        self.on_open_customer = on_open_customer
        self.on_show_totals = on_show_totals
        self.on_logout = on_logout
        self.on_show_sunbed_status = on_show_sunbed_status

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(18)

        outer.addWidget(
            build_top_strip(
                repo,
                "search",
                [
                    ("search", "Client Search", None, True),
                    ("queue", "Sunbed Queue", self.on_show_sunbed_status, True),
                    ("cashup", "Cash Up", self.on_show_totals, True),
                ],
                [("Logout", self.on_logout, "danger")],
            )
        )

        search_card = CardFrame(hero=True)
        search_layout = QVBoxLayout(search_card)
        search_layout.setContentsMargins(20, 20, 20, 20)
        search_layout.setSpacing(12)

        hint = QLabel("Search by DOB first when you can, then use the name/account search if needed.")
        hint.setProperty("role", "muted")
        search_layout.addWidget(hint)

        dob_row = QHBoxLayout()
        self.dob_box = QLineEdit()
        self.dob_box.setPlaceholderText("Date of birth DD/MM/YYYY")
        self.dob_box.returnPressed.connect(self.refresh)
        dob_row.addWidget(self.dob_box, 1)

        dob_search = QPushButton("Search DOB")
        dob_search.clicked.connect(self.refresh)
        dob_row.addWidget(dob_search)
        search_layout.addLayout(dob_row)

        name_row = QHBoxLayout()
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Name, customer number, or account number")
        self.search_box.returnPressed.connect(self.refresh)
        name_row.addWidget(self.search_box, 1)

        search_button = QPushButton("Search")
        search_button.clicked.connect(self.refresh)
        name_row.addWidget(search_button)
        search_layout.addLayout(name_row)
        outer.addWidget(search_card)

        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(self.open_selected)
        outer.addWidget(self.list_widget, 1)

        button_row = QHBoxLayout()
        create_button = QPushButton("Create Customer")
        create_button.setProperty("variant", "secondary")
        create_button.clicked.connect(self.create_customer)
        button_row.addWidget(create_button)
        button_row.addStretch(1)

        open_button = QPushButton("Open Customer")
        open_button.setProperty("variant", "success")
        open_button.clicked.connect(self.open_selected)
        button_row.addWidget(open_button)
        outer.addLayout(button_row)

        self.refresh()

    def refresh(self):
        rows = self.repo.search_customers(self.search_box.text(), self.dob_box.text())
        self.list_widget.clear()
        for row in rows:
            birthday = self.repo.birthday_for_display(row["birthday"])
            text = (
                f"{row['customer_number']} | {row['first_name']} {row['last_name']} | "
                f"DOB {birthday or '-'} | "
                f"Acc {row['account_number'] or '-'} | "
                f"GBP {float(row['account_balance'] or 0):.2f} | "
                f"{int(row['package_minutes'] or 0)} mins"
            )
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, int(row["id"]))
            self.list_widget.addItem(item)

    def create_customer(self):
        dialog = CreateCustomerDialog(self.repo, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.created_customer_id is None:
            return
        self.refresh()
        self.on_open_customer(dialog.created_customer_id)

    def open_selected(self):
        item = self.list_widget.currentItem()
        if item is None:
            QMessageBox.information(self, "Select Customer", "Choose a customer first.")
            return
        self.on_open_customer(int(item.data(Qt.ItemDataRole.UserRole)))


class SellTanningPage(QWidget):
    def __init__(
        self,
        repo: TillRepository,
        session: AppSession,
        on_back,
        on_show_customer_search,
        on_show_sell_course,
        on_show_sunbed_status,
        on_show_totals,
        on_sale_complete,
        get_sale_basket,
        clear_sale_basket,
    ):
        super().__init__()
        self.repo = repo
        self.session = session
        self.on_back = on_back
        self.on_show_customer_search = on_show_customer_search
        self.on_show_sell_course = on_show_sell_course
        self.on_show_sunbed_status = on_show_sunbed_status
        self.on_show_totals = on_show_totals
        self.on_sale_complete = on_sale_complete
        self.get_sale_basket = get_sale_basket
        self.clear_sale_basket = clear_sale_basket
        self.customer_id: Optional[int] = None
        self.current_customer = None
        self.selected_device_id: Optional[int] = None
        self.selected_bed_number: int = 1
        self.selected_bed_name: str = "Bed 1"
        self.selected_minutes = 6
        self.selected_payment_method = "cash"
        self.last_quote: dict | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(18)

        outer.addWidget(
            build_top_strip(
                repo,
                "tanning",
                [
                    ("search", "Client Search", self.on_show_customer_search, True),
                    ("customer", "Customer", self.on_back, True),
                    ("tanning", "Tanning Sale", None, True),
                    ("queue", "Sunbed Queue", self.on_show_sunbed_status, True),
                    ("cashup", "Cash Up", self.on_show_totals, True),
                ],
                [("Back To Summary", self.on_back, "secondary")],
            )
        )

        top = QHBoxLayout()
        self.title = QLabel("")
        self.title.setProperty("role", "section_title")
        top.addWidget(self.title)
        top.addStretch(1)
        outer.addLayout(top)

        self.summary_row = QHBoxLayout()
        self.summary_row.setSpacing(12)
        self.minutes_metric = QLabel("0")
        self.minutes_metric.setProperty("role", "metric")
        self.summary_row.addWidget(self.metric_card("Account Minutes", self.minutes_metric), 0)
        self.summary_row.addStretch(1)
        outer.addLayout(self.summary_row)

        content = QHBoxLayout()
        content.setSpacing(16)

        card = CardFrame(hero=True)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 18, 18, 18)
        card_layout.setSpacing(16)

        bed_title = QLabel("Select Sunbed")
        bed_title.setProperty("role", "section_title")
        card_layout.addWidget(bed_title)

        self.selected_bed_label = QLabel("No sunbed selected")
        self.selected_bed_label.setProperty("role", "muted")
        card_layout.addWidget(self.selected_bed_label)

        self.bed_buttons_layout = QGridLayout()
        self.bed_buttons_layout.setHorizontalSpacing(8)
        self.bed_buttons_layout.setVerticalSpacing(8)
        self.bed_buttons: dict[int, QPushButton] = {}
        self.device_rows: list[object] = []
        self.sunbed_rows: list[dict] = []
        card_layout.addLayout(self.bed_buttons_layout)

        minute_title = QLabel("Select Minutes")
        minute_title.setProperty("role", "section_title")
        card_layout.addWidget(minute_title)

        minute_row = QHBoxLayout()
        minute_row.setSpacing(8)
        self.minute_buttons = {}
        for minutes in [3, 6, 9, 12, 15]:
            button = QPushButton(f"{minutes} MIN")
            button.setProperty("compact", "true")
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, value=minutes: self.set_minutes(value))
            minute_row.addWidget(button)
            self.minute_buttons[minutes] = button
        card_layout.addLayout(minute_row)

        minute_adjust_row = QHBoxLayout()
        minute_adjust_row.setSpacing(8)
        minute_adjust_row.addStretch(1)

        minute_down_button = QPushButton("-1 Min")
        minute_down_button.setProperty("compact", "true")
        minute_down_button.clicked.connect(lambda _checked=False: self.adjust_minutes(-1))
        minute_adjust_row.addWidget(minute_down_button)

        self.custom_minutes_label = QLabel("")
        self.custom_minutes_label.setProperty("role", "section_title")
        minute_adjust_row.addWidget(self.custom_minutes_label)

        minute_up_button = QPushButton("+1 Min")
        minute_up_button.setProperty("compact", "true")
        minute_up_button.clicked.connect(lambda _checked=False: self.adjust_minutes(1))
        minute_adjust_row.addWidget(minute_up_button)

        minute_adjust_row.addStretch(1)
        card_layout.addLayout(minute_adjust_row)

        payment_title = QLabel("Payment Method")
        payment_title.setProperty("role", "section_title")
        card_layout.addWidget(payment_title)

        payment_row = QHBoxLayout()
        payment_row.setSpacing(8)
        self.payment_buttons = {}
        payment_labels = {
            "cash": "Cash",
            "card": "Card",
            "package_minutes": "Account Minutes",
        }
        for key, label in payment_labels.items():
            button = QPushButton(label)
            button.setProperty("compact", "true")
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, value=key: self.set_payment_method(value))
            payment_row.addWidget(button)
            self.payment_buttons[key] = button
        card_layout.addLayout(payment_row)

        self.price_preview = QLabel("Estimated sale total: GBP 0.00")
        self.price_preview.setProperty("role", "section_title")
        card_layout.addWidget(self.price_preview)

        self.payment_hint = QLabel("")
        self.payment_hint.setProperty("role", "muted")
        card_layout.addWidget(self.payment_hint)

        action_row = QHBoxLayout()
        action_row.addStretch(1)

        course_button = QPushButton("Buy A Course Instead")
        course_button.setProperty("variant", "secondary")
        course_button.clicked.connect(lambda: self.customer_id and self.on_show_sell_course(self.customer_id))
        action_row.addWidget(course_button)

        complete_button = QPushButton("Complete Tanning Sale")
        complete_button.setProperty("variant", "success")
        complete_button.clicked.connect(self.complete_sale)
        action_row.addWidget(complete_button)
        card_layout.addLayout(action_row)

        content.addWidget(card, 1)

        checkout_card = CardFrame()
        checkout_layout = QVBoxLayout(checkout_card)
        checkout_layout.setContentsMargins(18, 18, 18, 18)
        checkout_layout.setSpacing(12)
        checkout_title = QLabel("Sale Summary")
        checkout_title.setProperty("role", "section_title")
        checkout_layout.addWidget(checkout_title)

        self.checkout_customer_label = QLabel("No customer loaded")
        self.checkout_customer_label.setStyleSheet("font-size:14px; font-weight:700; color:#0f4c6e;")
        self.checkout_customer_label.setWordWrap(True)
        checkout_layout.addWidget(self.checkout_customer_label)

        self.checkout_bed_label = QLabel("Bed: -")
        self.checkout_bed_label.setProperty("role", "muted")
        checkout_layout.addWidget(self.checkout_bed_label)

        self.checkout_minutes_label = QLabel("Minutes: -")
        self.checkout_minutes_label.setProperty("role", "muted")
        checkout_layout.addWidget(self.checkout_minutes_label)

        self.checkout_payment_label = QLabel("Payment: -")
        self.checkout_payment_label.setProperty("role", "muted")
        checkout_layout.addWidget(self.checkout_payment_label)

        self.checkout_retail_label = QLabel("Retail Items: None")
        self.checkout_retail_label.setProperty("role", "muted")
        self.checkout_retail_label.setWordWrap(True)
        checkout_layout.addWidget(self.checkout_retail_label)

        breakdown = build_muted_panel()
        breakdown_layout = QVBoxLayout(breakdown)
        breakdown_layout.setContentsMargins(14, 14, 14, 14)
        breakdown_layout.setSpacing(8)
        breakdown_title = QLabel("Pricing Breakdown")
        breakdown_title.setProperty("role", "eyebrow")
        breakdown_layout.addWidget(breakdown_title)
        self.checkout_charge_label = QLabel("GBP 0.00")
        self.checkout_charge_label.setProperty("role", "metric")
        breakdown_layout.addWidget(self.checkout_charge_label)
        self.checkout_full_value_label = QLabel("Full session value: GBP 0.00")
        self.checkout_full_value_label.setProperty("role", "muted")
        breakdown_layout.addWidget(self.checkout_full_value_label)
        self.checkout_minutes_used_label = QLabel("Account minutes used: 0")
        self.checkout_minutes_used_label.setProperty("role", "muted")
        breakdown_layout.addWidget(self.checkout_minutes_used_label)
        self.checkout_retail_total_label = QLabel("Retail items total: GBP 0.00")
        self.checkout_retail_total_label.setProperty("role", "muted")
        breakdown_layout.addWidget(self.checkout_retail_total_label)
        checkout_layout.addWidget(breakdown)

        self.checkout_hint = QLabel("Select a bed, minutes, and payment method to prepare the sale.")
        self.checkout_hint.setProperty("role", "muted")
        self.checkout_hint.setWordWrap(True)
        checkout_layout.addWidget(self.checkout_hint)
        checkout_layout.addStretch(1)

        content.addWidget(checkout_card, 1)
        outer.addLayout(content)
        outer.addStretch(1)

        self.populate_site_choices()
        self.set_minutes(self.selected_minutes)
        self.set_payment_method(self.selected_payment_method)
        self.update_price_preview()

    def metric_card(self, label_text: str, metric_label: QLabel):
        card = CardFrame()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 16, 16, 16)
        label = QLabel(label_text)
        label.setProperty("role", "muted")
        layout.addWidget(label)
        layout.addWidget(metric_label)
        return card

    def populate_site_choices(self):
        while self.bed_buttons_layout.count():
            item = self.bed_buttons_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.bed_buttons = {}
        self.selected_device_id = None
        self.selected_bed_number = 1
        self.selected_bed_name = "Bed 1"

        self.device_rows = list(self.repo.current_site_devices(self.session.till_session))
        if not self.device_rows:
            self.device_rows = list(self.repo.active_devices())
        self.sunbed_rows = list(self.repo.sunbed_status_rows(self.session.till_session))
        if not self.sunbed_rows:
            self.sunbed_rows = list(self.repo.sunbed_status_rows(None))

        if self.device_rows:
            fallback_device_id = int(self.device_rows[0]["id"])
        else:
            fallback_device_id = None

        device_rows_by_number = {}
        for row in self.device_rows:
            try:
                device_rows_by_number[int(row["device_number"])] = row
            except (TypeError, ValueError, KeyError, IndexError):
                continue

        for index in range(1, 9):
            if index <= len(self.sunbed_rows):
                row = dict(self.sunbed_rows[index - 1])
                device_name = sunbed_display_text(row, index)
            else:
                device_name = f"Bed {index}"

            numbered_device = device_rows_by_number.get(index)
            if numbered_device is not None:
                device_id = int(numbered_device["id"])
            elif index <= len(self.device_rows):
                device_id = int(self.device_rows[index - 1]["id"])
            else:
                device_id = fallback_device_id

            button = QPushButton(self.sunbed_button_text(device_name))
            button.setCheckable(True)
            button.setProperty("bedcard", "true")
            button.clicked.connect(
                lambda _checked=False, device_id=device_id, bed_number=index, device_name=device_name: self.set_sunbed(
                    device_id,
                    bed_number,
                    device_name,
                )
            )
            self.bed_buttons[index] = button
            self.bed_buttons_layout.addWidget(button, (index - 1) // 4, (index - 1) % 4)

        if self.bed_buttons:
            first_bed_number = min(self.bed_buttons.keys())
            first_device_id = fallback_device_id
            first_device_name = (
                sunbed_display_text(dict(self.sunbed_rows[0]), 1)
                if self.sunbed_rows
                else "Bed 1"
            )
            if first_device_id is not None:
                self.set_sunbed(first_device_id, first_bed_number, first_device_name)
            else:
                self.selected_bed_label.setText("No active devices found for pricing.")
        else:
            self.selected_bed_label.setText("No active sunbeds found for this site.")
        self.update_price_preview()

    def sunbed_button_text(self, device_name: str) -> str:
        text = str(device_name or "").strip()
        if " | " in text:
            bed_label, custom_name = text.split(" | ", 1)
            return f"{bed_label}\n{format_sunbed_name_for_display(custom_name)}"
        return text

    def set_sunbed(self, device_id: int | None, bed_number: int, device_name: str):
        self.selected_device_id = int(device_id) if device_id is not None else None
        self.selected_bed_number = int(bed_number)
        self.selected_bed_name = str(device_name)
        for value, button in self.bed_buttons.items():
            button.setChecked(value == bed_number)
        self.selected_bed_label.setText(f"Selected: {device_name}")
        self.update_price_preview()

    def set_minutes(self, minutes: int):
        self.selected_minutes = max(1, min(int(minutes), 30))
        for value, button in self.minute_buttons.items():
            button.setChecked(value == self.selected_minutes)
        self.custom_minutes_label.setText(f"Selected: {self.selected_minutes} min")
        self.update_price_preview()

    def adjust_minutes(self, delta: int):
        self.set_minutes(self.selected_minutes + int(delta))

    def set_payment_method(self, payment_method: str):
        self.selected_payment_method = payment_method
        for value, button in self.payment_buttons.items():
            button.setChecked(value == payment_method)
        self.update_price_preview()

    def load_customer(self, customer_id: int):
        self.customer_id = customer_id
        customer = self.repo.get_customer(customer_id)
        if customer is None:
            QMessageBox.warning(self, "Missing Customer", "Customer not found.")
            self.on_back()
            return
        self.current_customer = customer

        self.title.setText(f"Sell Tanning - {customer['first_name']} {customer['last_name']}")
        self.minutes_metric.setText(str(int(customer["package_minutes"] or 0)))
        self.checkout_customer_label.setText(
            f"{customer['first_name']} {customer['last_name']} | Acc {customer['account_number'] or '-'}"
        )
        self.populate_site_choices()
        self.update_price_preview()

    def update_price_preview(self):
        site_id = self.session.till_session.site_id if self.session.till_session else None
        device_id = self.selected_device_id
        basket_items = self.get_sale_basket(int(self.customer_id)) if self.customer_id else []
        basket_total = round(sum(float(item["total_amount"]) for item in basket_items), 2)
        self.checkout_bed_label.setText(f"Bed: {self.selected_bed_name}")
        self.checkout_minutes_label.setText(f"Minutes: {self.selected_minutes}")
        payment_text = {
            "cash": "Cash",
            "card": "Card",
            "package_minutes": "Account Minutes",
        }.get(self.selected_payment_method, self.selected_payment_method.title())
        self.checkout_payment_label.setText(f"Payment: {payment_text}")
        if basket_items:
            basket_names = ", ".join(f"{item['name']} x{item['quantity']}" for item in basket_items)
            self.checkout_retail_label.setText(f"Retail Items: {basket_names}")
        else:
            self.checkout_retail_label.setText("Retail Items: None")

        package_allowed = basket_total <= 0
        self.payment_buttons["package_minutes"].setEnabled(package_allowed)
        if not package_allowed and self.selected_payment_method == "package_minutes":
            self.set_payment_method("card")
            payment_text = "Card"
            self.checkout_payment_label.setText(f"Payment: {payment_text}")
        if not site_id or not device_id or not self.customer_id:
            self.last_quote = None
            self.price_preview.setText("Estimated sale total: GBP 0.00")
            self.payment_hint.setText("")
            self.checkout_charge_label.setText("GBP 0.00")
            self.checkout_full_value_label.setText("Full session value: GBP 0.00")
            self.checkout_minutes_used_label.setText("Account minutes used: 0")
            self.checkout_retail_total_label.setText(f"Retail items total: GBP {basket_total:.2f}")
            self.checkout_hint.setText("Select a bed, minutes, and payment method to prepare the sale.")
            return
        try:
            quote = self.repo.quote_tanning_sale(
                customer_id=int(self.customer_id),
                site_id=int(site_id),
                device_id=int(device_id),
                minutes=int(self.selected_minutes),
                payment_method=self.selected_payment_method,
            )
        except ValueError as exc:
            self.last_quote = None
            self.price_preview.setText("Estimated sale total: GBP 0.00")
            self.payment_hint.setText(str(exc))
            self.checkout_charge_label.setText("GBP 0.00")
            self.checkout_full_value_label.setText("Full session value: GBP 0.00")
            self.checkout_minutes_used_label.setText("Account minutes used: 0")
            self.checkout_retail_total_label.setText(f"Retail items total: GBP {basket_total:.2f}")
            self.checkout_hint.setText(str(exc))
            return
        self.last_quote = quote
        combined_charge = round(float(quote["cash_or_card_amount"]) + basket_total, 2)
        combined_value = round(float(quote["full_session_value"]) + basket_total, 2)

        self.price_preview.setText(
            f"Charge now: GBP {combined_charge:.2f} | Session value: GBP {combined_value:.2f}"
        )
        self.checkout_charge_label.setText(f"GBP {combined_charge:.2f}")
        self.checkout_full_value_label.setText(f"Full session value: GBP {combined_value:.2f}")
        self.checkout_minutes_used_label.setText(f"Account minutes used: {quote['account_minutes_used']}")
        self.checkout_retail_total_label.setText(f"Retail items total: GBP {basket_total:.2f}")
        if self.selected_payment_method == "package_minutes":
            self.payment_hint.setText(
                f"Using {quote['account_minutes_used']} account mins for the full session."
            )
            self.checkout_hint.setText("Customer is using account minutes only for this session.")
        else:
            hint = f"Using {quote['account_minutes_used']} account mins first. On completion, card is offered first and cash is only used if card is declined."
            if basket_total > 0:
                hint += " Retail items will be charged in the same payment."
            self.payment_hint.setText(hint)
            self.checkout_hint.setText(
                "Account minutes are used first when available. Remaining tanning value and any retail items are settled together."
            )

    def confirm_checkout_method(self, charge_amount: float) -> Optional[str]:
        if charge_amount <= 0:
            return self.selected_payment_method

        card_dialog = CardPaymentDialog(charge_amount, self)
        if card_dialog.exec() == QDialog.DialogCode.Accepted:
            return "card"

        cash_dialog = CashPaymentDialog(charge_amount, self)
        if cash_dialog.exec() == QDialog.DialogCode.Accepted:
            return "cash"

        return None

    def complete_sale(self):
        if not self.customer_id:
            QMessageBox.warning(self, "Missing Customer", "Choose a customer first.")
            return
        site_id = int(self.session.till_session.site_id)
        if self.selected_device_id is None:
            QMessageBox.warning(self, "Missing Sunbed", "Choose a sunbed first.")
            return
        device_id = int(self.selected_device_id)
        try:
            quote = self.repo.quote_tanning_sale(
                customer_id=self.customer_id,
                site_id=site_id,
                device_id=device_id,
                minutes=int(self.selected_minutes),
                payment_method=self.selected_payment_method,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Sale Blocked", str(exc))
            return

        basket_items = self.get_sale_basket(int(self.customer_id)) if self.customer_id else []
        basket_total = round(sum(float(item["total_amount"]) for item in basket_items), 2)
        if basket_items and self.selected_payment_method == "package_minutes":
            QMessageBox.warning(
                self,
                "Payment Method Required",
                "Retail items cannot be settled with account minutes. Choose cash or card for the combined sale.",
            )
            return

        final_payment_method = self.selected_payment_method
        if self.selected_payment_method != "package_minutes":
            final_payment_method = self.confirm_checkout_method(round(float(quote["cash_or_card_amount"]) + basket_total, 2))
            if final_payment_method is None:
                return

        try:
            result = self.repo.record_tanning_sale(
                customer_id=self.customer_id,
                site_id=site_id,
                terminal_id=int(self.session.till_session.terminal_id),
                device_id=device_id,
                minutes=int(self.selected_minutes),
                payment_method=final_payment_method,
                staff_user_id=int(self.session.staff_row["id"]),
                till_session_id=self.session.till_session.id if self.session.till_session else None,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Sale Blocked", str(exc))
            return

        if final_payment_method == "cash" and not open_cash_drawer():
            QMessageBox.warning(
                self,
                "Cash Drawer Not Opened",
                "The sale completed, but the cash drawer command did not run.",
            )

        retail_results = []
        try:
            for item in basket_items:
                retail_results.append(
                    self.repo.record_retail_sale(
                        customer_id=self.customer_id,
                        site_id=int(self.session.till_session.site_id),
                        terminal_id=int(self.session.till_session.terminal_id),
                        staff_user_id=int(self.session.staff_row["id"]),
                        till_session_id=self.session.till_session.id if self.session.till_session else None,
                        product_id=int(item["product_id"]),
                        quantity=int(item["quantity"]),
                        payment_method=final_payment_method,
                    )
                )
        except ValueError as exc:
            QMessageBox.warning(self, "Retail Sale Blocked", str(exc))
            return
        self.clear_sale_basket(int(self.customer_id))

        summary_box = QMessageBox(self)
        summary_box.setWindowTitle("Sale Completed")
        summary_box.setText(
            (
                f"Tanning sale saved.\n\n"
                f"Device: {result['device_name']}\n"
                f"Minutes: {result['minutes']}\n"
                f"Account mins used: {result['account_minutes_used']}\n"
                f"Retail items: {len(retail_results)}\n"
                f"Charged now: GBP {round(float(result['total_amount']) + basket_total, 2):.2f}"
            )
        )
        print_button = summary_box.addButton("Print Receipt", QMessageBox.ButtonRole.AcceptRole)
        summary_box.addButton("Done", QMessageBox.ButtonRole.RejectRole)
        print_button.setProperty("variant", "secondary")
        summary_box.exec()
        if summary_box.clickedButton() is print_button:
            print_receipt_for_transactions(
                self,
                self.repo,
                [int(result["transaction_id"])] + [int(item["transaction_id"]) for item in retail_results],
            )
        customer = self.repo.get_customer(self.customer_id)
        self.on_sale_complete(
            PendingSale(
                customer_id=self.customer_id,
                customer_name=f"{customer['first_name']} {customer['last_name']}",
                customer_account=customer["account_number"] or "",
                minutes=result["minutes"],
                payment_method=result["payment_method"],
                total_amount=round(float(result["total_amount"]) + basket_total, 2),
                device_id=result["device_id"],
                device_name=self.selected_bed_name,
                target_bed_number=self.selected_bed_number,
            )
        )


class SellProductsPage(QWidget):
    def __init__(self, repo: TillRepository, session: AppSession, on_back, on_show_customer_search, on_show_sunbed_status, on_add_to_sale, on_continue_to_tanning):
        super().__init__()
        self.repo = repo
        self.session = session
        self.on_back = on_back
        self.on_show_customer_search = on_show_customer_search
        self.on_show_sunbed_status = on_show_sunbed_status
        self.on_add_to_sale = on_add_to_sale
        self.on_continue_to_tanning = on_continue_to_tanning
        self.customer_id: Optional[int] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(18)

        outer.addWidget(
            build_top_strip(
                repo,
                "products",
                [],
                [
                    ("Search Customer", self.on_show_customer_search, "secondary"),
                    ("Sunbed Status", self.on_show_sunbed_status, "accent"),
                    ("Back To Summary", self.on_back, "secondary"),
                ],
            )
        )

        top = QHBoxLayout()
        self.title = QLabel("")
        self.title.setProperty("role", "section_title")
        top.addWidget(self.title)
        top.addStretch(1)
        outer.addLayout(top)

        search_card = CardFrame(hero=True)
        search_layout = QHBoxLayout(search_card)
        search_layout.setContentsMargins(20, 20, 20, 20)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Scan barcode or search cream/product")
        self.search_box.returnPressed.connect(self.refresh_products)
        search_layout.addWidget(self.search_box, 1)
        search_button = QPushButton("Search")
        search_button.clicked.connect(self.refresh_products)
        search_layout.addWidget(search_button)
        outer.addWidget(search_card)

        form_card = CardFrame()
        form_layout = QFormLayout(form_card)
        form_layout.setContentsMargins(20, 20, 20, 20)
        form_layout.setSpacing(16)
        self.product_combo = QComboBox()
        self.quantity_combo = QComboBox()
        for qty in range(1, 7):
            self.quantity_combo.addItem(str(qty), qty)
        form_layout.addRow("Product", self.product_combo)
        form_layout.addRow("Quantity", self.quantity_combo)
        outer.addWidget(form_card)

        payment_card = CardFrame()
        payment_layout = QVBoxLayout(payment_card)
        payment_layout.setContentsMargins(20, 20, 20, 20)
        payment_layout.setSpacing(12)
        payment_title = QLabel("Current Sale")
        payment_title.setProperty("role", "section_title")
        payment_layout.addWidget(payment_title)
        basket_hint = QLabel("Creams and products are added to the current customer sale, then charged together when the tanning sale is completed.")
        basket_hint.setProperty("role", "muted")
        basket_hint.setWordWrap(True)
        payment_layout.addWidget(basket_hint)
        self.product_total_label = QLabel("Total: GBP 0.00")
        self.product_total_label.setProperty("role", "section_title")
        payment_layout.addWidget(self.product_total_label)
        outer.addWidget(payment_card)

        complete_button = QPushButton("Add To Current Sale")
        complete_button.setProperty("variant", "success")
        complete_button.clicked.connect(self.complete_sale)
        outer.addWidget(complete_button, alignment=Qt.AlignmentFlag.AlignRight)
        outer.addStretch(1)

        self.quantity_combo.currentIndexChanged.connect(self.update_total)
        self.product_combo.currentIndexChanged.connect(self.update_total)
        self.refresh_products()

    def load_customer(self, customer_id: int):
        self.customer_id = customer_id
        customer = self.repo.get_customer(customer_id)
        if customer is None:
            QMessageBox.warning(self, "Missing Customer", "Customer not found.")
            self.on_back()
            return
        self.title.setText(f"Serving {customer['first_name']} {customer['last_name']}")
        self.refresh_products()

    def refresh_products(self):
        rows = self.repo.active_retail_products(self.search_box.text())
        self.product_combo.clear()
        for row in rows:
            group = f"{row['group_name']} | " if row["group_name"] else ""
            label = f"{group}{row['name']} | GBP {float(row['price'] or 0):.2f} | Stock {int(row['stock_quantity'] or 0)}"
            self.product_combo.addItem(label, int(row["id"]))
        self.update_total()

    def update_total(self):
        product_id = self.product_combo.currentData()
        if not product_id:
            self.product_total_label.setText("Total: GBP 0.00")
            return
        product = self.repo.fetch_one("select * from retail_products where id = ?", (int(product_id),))
        if product is None:
            self.product_total_label.setText("Total: GBP 0.00")
            return
        quantity = int(self.quantity_combo.currentData())
        total = round(float(product["price"] or 0) * quantity, 2)
        self.product_total_label.setText(f"Total: GBP {total:.2f}")

    def complete_sale(self):
        if not self.customer_id:
            QMessageBox.warning(self, "Missing Customer", "Choose a customer first.")
            return
        product_id = self.product_combo.currentData()
        if not product_id:
            QMessageBox.warning(self, "Missing Product", "Choose a product first.")
            return
        product = self.repo.fetch_one("select * from retail_products where id = ? and is_active = 1", (int(product_id),))
        if product is None:
            QMessageBox.warning(self, "Missing Product", "This product is no longer available.")
            return
        quantity = int(self.quantity_combo.currentData())
        if int(product["stock_quantity"] or 0) < quantity:
            QMessageBox.warning(self, "Sale Blocked", "Not enough stock available.")
            return
        total = round(float(product["price"] or 0) * quantity, 2)
        self.on_add_to_sale(
            int(self.customer_id),
            {
                "product_id": int(product_id),
                "name": str(product["name"]),
                "quantity": quantity,
                "unit_price": float(product["price"] or 0),
                "total_amount": total,
            },
        )

        QMessageBox.information(
            self,
            "Added To Sale",
            (
                f"Product: {product['name']}\n"
                f"Quantity: {quantity}\n"
                f"Added to current sale: GBP {total:.2f}"
            ),
        )
        self.refresh_products()
        self.on_continue_to_tanning(int(self.customer_id))


class SellCoursePage(QWidget):
    def __init__(self, repo: TillRepository, session: AppSession, on_back, on_show_customer_search, on_show_sunbed_status):
        super().__init__()
        self.repo = repo
        self.session = session
        self.on_back = on_back
        self.on_show_customer_search = on_show_customer_search
        self.on_show_sunbed_status = on_show_sunbed_status
        self.customer_id: Optional[int] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(18)

        outer.addWidget(
            build_top_strip(
                repo,
                "course",
                [],
                [
                    ("Search Customer", self.on_show_customer_search, "secondary"),
                    ("Sunbed Status", self.on_show_sunbed_status, "accent"),
                    ("Back To Summary", self.on_back, "secondary"),
                ],
            )
        )

        top = QHBoxLayout()
        self.title = QLabel("")
        self.title.setProperty("role", "section_title")
        top.addWidget(self.title)
        top.addStretch(1)
        outer.addLayout(top)

        form_card = CardFrame(hero=True)
        form_layout = QFormLayout(form_card)
        form_layout.setContentsMargins(20, 20, 20, 20)
        form_layout.setSpacing(16)
        self.package_combo = QComboBox()
        form_layout.addRow("Course", self.package_combo)
        outer.addWidget(form_card)

        payment_card = CardFrame()
        payment_layout = QVBoxLayout(payment_card)
        payment_layout.setContentsMargins(20, 20, 20, 20)
        payment_layout.setSpacing(12)
        payment_title = QLabel("Payment Method")
        payment_title.setProperty("role", "section_title")
        payment_layout.addWidget(payment_title)
        row = QHBoxLayout()
        self.course_payment_buttons = {}
        for key, label in {"cash": "Cash", "card": "Card"}.items():
            button = QPushButton(label)
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, value=key: self.set_course_payment_method(value))
            row.addWidget(button)
            self.course_payment_buttons[key] = button
        payment_layout.addLayout(row)
        self.course_total_label = QLabel("Course Total: GBP 0.00")
        self.course_total_label.setProperty("role", "section_title")
        payment_layout.addWidget(self.course_total_label)
        self.course_minutes_label = QLabel("Minutes Added: 0")
        self.course_minutes_label.setProperty("role", "muted")
        payment_layout.addWidget(self.course_minutes_label)
        outer.addWidget(payment_card)

        complete_button = QPushButton("Complete Course Sale")
        complete_button.setProperty("variant", "success")
        complete_button.clicked.connect(self.complete_sale)
        outer.addWidget(complete_button, alignment=Qt.AlignmentFlag.AlignRight)
        outer.addStretch(1)

        self.selected_course_payment_method = "cash"
        self.package_combo.currentIndexChanged.connect(self.update_total)
        self.set_course_payment_method("cash")
        self.refresh_courses()

    def set_course_payment_method(self, payment_method: str):
        self.selected_course_payment_method = payment_method
        for value, button in self.course_payment_buttons.items():
            button.setChecked(value == payment_method)

    def load_customer(self, customer_id: int):
        self.customer_id = customer_id
        customer = self.repo.get_customer(customer_id)
        if customer is None:
            QMessageBox.warning(self, "Missing Customer", "Customer not found.")
            self.on_back()
            return
        self.title.setText(f"Serving {customer['first_name']} {customer['last_name']}")
        self.refresh_courses()

    def refresh_courses(self):
        rows = self.repo.active_package_products()
        self.package_combo.clear()
        for row in rows:
            label = f"{row['name']} | {int(row['minutes_included'] or 0)} mins | GBP {float(row['price'] or 0):.2f}"
            self.package_combo.addItem(label, int(row["id"]))
        self.update_total()

    def update_total(self):
        package_id = self.package_combo.currentData()
        if not package_id:
            self.course_total_label.setText("Course Total: GBP 0.00")
            self.course_minutes_label.setText("Minutes Added: 0")
            return
        package_row = self.repo.fetch_one("select * from package_products where id = ?", (int(package_id),))
        if package_row is None:
            self.course_total_label.setText("Course Total: GBP 0.00")
            self.course_minutes_label.setText("Minutes Added: 0")
            return
        self.course_total_label.setText(f"Course Total: GBP {float(package_row['price'] or 0):.2f}")
        self.course_minutes_label.setText(f"Minutes Added: {int(package_row['minutes_included'] or 0)}")

    def complete_sale(self):
        if not self.customer_id:
            QMessageBox.warning(self, "Missing Customer", "Choose a customer first.")
            return
        package_id = self.package_combo.currentData()
        if not package_id:
            QMessageBox.warning(self, "Missing Course", "Choose a course first.")
            return
        try:
            result = self.repo.record_package_sale(
                customer_id=self.customer_id,
                site_id=int(self.session.till_session.site_id),
                terminal_id=int(self.session.till_session.terminal_id),
                staff_user_id=int(self.session.staff_row["id"]),
                till_session_id=self.session.till_session.id if self.session.till_session else None,
                package_product_id=int(package_id),
                payment_method=self.selected_course_payment_method,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Sale Blocked", str(exc))
            return

        if self.selected_course_payment_method == "cash" and not open_cash_drawer():
            QMessageBox.warning(
                self,
                "Cash Drawer Not Opened",
                "The sale completed, but the cash drawer command did not run.",
            )

        summary_box = QMessageBox(self)
        summary_box.setWindowTitle("Course Sale Completed")
        summary_box.setText(
            (
                f"Course: {result['package_name']}\n"
                f"Minutes added: {result['minutes_included']}\n"
                f"Paid by: {result['payment_method'].title()}\n"
                f"Charged now: GBP {result['total_amount']:.2f}"
            )
        )
        print_button = summary_box.addButton("Print Receipt", QMessageBox.ButtonRole.AcceptRole)
        summary_box.addButton("Done", QMessageBox.ButtonRole.RejectRole)
        print_button.setProperty("variant", "secondary")
        summary_box.exec()
        if summary_box.clickedButton() is print_button:
            print_receipt_for_transactions(self, self.repo, [int(result["transaction_id"])])
        self.on_back()


class TotalsPage(QWidget):
    def __init__(self, repo: TillRepository, session: AppSession, on_back, on_show_sunbed_status, on_close_shift):
        super().__init__()
        self.repo = repo
        self.session = session
        self.on_back = on_back
        self.on_show_sunbed_status = on_show_sunbed_status
        self.on_close_shift = on_close_shift

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(18)

        outer.addWidget(
            build_top_strip(
                repo,
                "cashup",
                [
                    ("search", "Client Search", self.on_back, True),
                    ("queue", "Sunbed Queue", self.on_show_sunbed_status, True),
                    ("cashup", "Cash Up", None, True),
                ],
                [("Back", self.on_back, "secondary")],
            )
        )

        self.shift_state_card = CardFrame(hero=True)
        shift_state_layout = QHBoxLayout(self.shift_state_card)
        shift_state_layout.setContentsMargins(18, 18, 18, 18)
        shift_state_layout.setSpacing(16)
        shift_intro = QVBoxLayout()
        shift_intro.setSpacing(4)
        shift_title = QLabel("Shift Close Dashboard")
        shift_title.setProperty("role", "section_title")
        shift_intro.addWidget(shift_title)
        self.shift_context_label = QLabel("Open till session not loaded")
        self.shift_context_label.setProperty("role", "muted")
        self.shift_context_label.setWordWrap(True)
        shift_intro.addWidget(self.shift_context_label)
        shift_state_layout.addLayout(shift_intro, 1)
        self.shift_difference_label = QLabel("GBP 0.00")
        self.shift_difference_label.setProperty("role", "metric")
        shift_state_layout.addWidget(self.shift_difference_label)
        outer.addWidget(self.shift_state_card)

        content = QHBoxLayout()
        content.setSpacing(16)

        metrics_card = CardFrame()
        metrics_shell = QVBoxLayout(metrics_card)
        metrics_shell.setContentsMargins(18, 18, 18, 18)
        metrics_shell.setSpacing(14)
        metrics_title = QLabel("Shift And Day Totals")
        metrics_title.setProperty("role", "section_title")
        metrics_shell.addWidget(metrics_title)

        metrics = QGridLayout()
        self.sales_label = QLabel("GBP 0.00")
        self.sales_label.setProperty("role", "metric")
        self.cash_label = QLabel("GBP 0.00")
        self.cash_label.setProperty("role", "metric")
        self.card_label = QLabel("GBP 0.00")
        self.card_label.setProperty("role", "metric")
        self.count_label = QLabel("0")
        self.count_label.setProperty("role", "metric")
        self.day_sales_label = QLabel("GBP 0.00")
        self.day_sales_label.setProperty("role", "metric")
        self.day_cash_label = QLabel("GBP 0.00")
        self.day_cash_label.setProperty("role", "metric")
        self.day_card_label = QLabel("GBP 0.00")
        self.day_card_label.setProperty("role", "metric")
        self.day_count_label = QLabel("0")
        self.day_count_label.setProperty("role", "metric")

        metrics.addWidget(self.metric_card("Shift Sales", self.sales_label), 0, 0)
        metrics.addWidget(self.metric_card("Cash", self.cash_label), 0, 1)
        metrics.addWidget(self.metric_card("Card", self.card_label), 0, 2)
        metrics.addWidget(self.metric_card("Transactions", self.count_label), 0, 3)
        metrics.addWidget(self.metric_card("Day Running Total", self.day_sales_label), 1, 0)
        metrics.addWidget(self.metric_card("Day Cash", self.day_cash_label), 1, 1)
        metrics.addWidget(self.metric_card("Day Card", self.day_card_label), 1, 2)
        metrics.addWidget(self.metric_card("Day Transactions", self.day_count_label), 1, 3)
        metrics_shell.addLayout(metrics)
        content.addWidget(metrics_card, 2)

        cashup_card = CardFrame()
        cashup_layout = QFormLayout(cashup_card)
        cashup_layout.setContentsMargins(20, 20, 20, 20)
        cashup_layout.setSpacing(14)

        self.expected_cash_label = QLabel("GBP 0.00")
        self.expected_cash_label.setProperty("role", "section_title")
        self.shift_card_total_label = QLabel("GBP 0.00")
        self.shift_card_total_label.setProperty("role", "section_title")

        self.counted_cash_input = QDoubleSpinBox()
        self.counted_cash_input.setMaximum(99999.99)
        self.counted_cash_input.setPrefix("GBP ")
        self.counted_cash_input.setDecimals(2)
        self.counted_cash_input.valueChanged.connect(self.refresh_variance)

        self.variance_label = QLabel("GBP 0.00")
        self.variance_label.setProperty("role", "section_title")

        self.closing_notes_input = QLineEdit()
        self.closing_notes_input.setPlaceholderText("Optional closing notes")

        close_button = QPushButton("Close Till / Cash Up")
        close_button.setProperty("variant", "success")
        close_button.clicked.connect(self.close_shift)

        cashup_layout.addRow("Expected Cash", self.expected_cash_label)
        cashup_layout.addRow("Shift Card Total", self.shift_card_total_label)
        cashup_layout.addRow("Counted Cash", self.counted_cash_input)
        cashup_layout.addRow("Variance", self.variance_label)
        cashup_layout.addRow("Closing Notes", self.closing_notes_input)
        cashup_layout.addRow("", close_button)

        content.addWidget(cashup_card, 1)
        outer.addLayout(content)
        outer.addStretch(1)

    def metric_card(self, label_text: str, metric_label: QLabel):
        card = CardFrame()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 16, 16, 16)
        label = QLabel(label_text)
        label.setProperty("role", "muted")
        layout.addWidget(label)
        layout.addWidget(metric_label)
        return card

    def refresh(self):
        totals = self.repo.shift_summary(self.session.till_session.id if self.session.till_session else None)
        day_totals = self.repo.day_summary(self.session.till_session)
        self.sales_label.setText(f"GBP {totals['sales_total']:.2f}")
        self.cash_label.setText(f"GBP {totals['cash_total']:.2f}")
        self.card_label.setText(f"GBP {totals['card_total']:.2f}")
        self.count_label.setText(str(totals["transaction_count"]))
        self.day_sales_label.setText(f"GBP {day_totals['sales_total']:.2f}")
        self.day_cash_label.setText(f"GBP {day_totals['cash_total']:.2f}")
        self.day_card_label.setText(f"GBP {day_totals['card_total']:.2f}")
        self.day_count_label.setText(str(day_totals["transaction_count"]))

        expected_cash = float(self.session.till_session.expected_cash or 0) if self.session.till_session else 0.0
        self.expected_cash_label.setText(f"GBP {expected_cash:.2f}")
        self.shift_card_total_label.setText(f"GBP {totals['card_total']:.2f}")
        if abs(self.counted_cash_input.value()) < 0.001:
            self.counted_cash_input.setValue(expected_cash)
        if self.session.till_session is not None:
            self.shift_context_label.setText(
                f"{self.repo.till_site_name(self.session.till_session)} | "
                f"{self.repo.till_terminal_name(self.session.till_session)} | "
                f"Opened by {self.session.staff_row['name'] if self.session.staff_row else 'staff'}"
            )
        self.refresh_variance()

    def refresh_variance(self):
        expected_cash = float(self.session.till_session.expected_cash or 0) if self.session.till_session else 0.0
        variance = float(self.counted_cash_input.value()) - expected_cash
        self.variance_label.setText(f"GBP {variance:.2f}")
        self.shift_difference_label.setText(f"GBP {variance:.2f}")
        if variance < 0:
            style = "font-size:30px; font-weight:800; color:#b93028;"
        elif variance > 0:
            style = "font-size:30px; font-weight:800; color:#cb7b0a;"
        else:
            style = "font-size:30px; font-weight:800; color:#28733d;"
        self.shift_difference_label.setStyleSheet(style)

    def close_shift(self):
        if self.session.till_session is None:
            QMessageBox.warning(self, "No Open Till", "There is no open till session to close.")
            return

        try:
            result = self.repo.close_till(
                self.session.till_session.id,
                counted_cash=float(self.counted_cash_input.value()),
                closed_by_user_id=int(self.session.staff_row["id"]),
                closing_notes=self.closing_notes_input.text(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Cash Up Blocked", str(exc))
            return

        shift_totals = self.repo.shift_summary(self.session.till_session.id)
        email_sent, email_message = self.repo.send_shift_summary_email_for_session(self.session.till_session.id)

        QMessageBox.information(
            self,
            "Till Closed",
            (
                f"Shift closed successfully.\n\n"
                f"Expected cash: GBP {result['expected_cash']:.2f}\n"
                f"Shift card total: GBP {shift_totals['card_total']:.2f}\n"
                f"Counted cash: GBP {result['counted_cash']:.2f}\n"
                f"Variance: GBP {result['variance']:.2f}\n\n"
                f"Email: {email_message if email_message else ('Sent' if email_sent else 'Not sent')}"
            ),
        )
        self.on_close_shift()


class CustomerDisplayWindow(QWidget):
    def __init__(self, repo: TillRepository, session: AppSession, runtime_state: dict):
        super().__init__()
        self.repo = repo
        self.session = session
        self.runtime_state = runtime_state
        self.customer_display = repo.customer_display_settings()

        self.setWindowTitle("Sunbed Waiting Times")
        self.setStyleSheet("background:#eaf4ff;")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.BlankCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(18)

        outer.addWidget(build_brand_header(repo, "Sunbed Waiting Times", centered=False))

        subtitle = QLabel(str(self.customer_display["business_name"]))
        subtitle.setStyleSheet("font-size:20px; font-weight:700; color:#0f4c6e;")
        outer.addWidget(subtitle)

        self.grid_container = QWidget()
        self.grid = QGridLayout(self.grid_container)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(8)
        self.grid.setVerticalSpacing(8)
        outer.addWidget(self.grid_container, 1)

        footer = QLabel("Ask staff about happy hour prices and tanning creams")
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        footer.setStyleSheet("font-size:13px; color:#0f4c6e; font-weight:600;")
        outer.addWidget(footer)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_view)
        self.timer.start(1000)
        self.refresh_view()

    def format_mmss(self, total_seconds):
        minutes = max(0, int(total_seconds)) // 60
        seconds = max(0, int(total_seconds)) % 60
        return f"{minutes:02d}:{seconds:02d}"

    def queue_wait_seconds(self, current_remaining_seconds, queue_items):
        total = current_remaining_seconds
        for item in queue_items:
            total += self.queue_item_total_seconds(item)
        return total

    def queue_item_total_seconds(self, item: dict) -> int:
        if item.get("remaining_seconds") not in (None, ""):
            total = int(item["remaining_seconds"])
            total += int(item.get("tanning_seconds_remaining") or 0)
            total += int(item.get("cooldown_seconds_remaining") or 0)
            return total
        prep_seconds = int(self.runtime_state.get("prep_minutes", 0)) * 60
        cooldown_seconds = int(self.runtime_state.get("cooldown_minutes", 0)) * 60
        return prep_seconds + (int(item["minutes"]) * 60) + cooldown_seconds

    def active_cycle_remaining_seconds(self) -> int:
        phase = self.runtime_state["current_phase"]
        if phase == "ready":
            return 0
        total = int(self.runtime_state["time_left"] or 0)
        if phase == "prep":
            total += int(self.runtime_state.get("tanning_seconds_remaining") or 0)
            total += int(self.runtime_state.get("cooldown_seconds_remaining") or 0)
        elif phase == "tanning":
            total += int(self.runtime_state.get("cooldown_seconds_remaining") or 0)
        return total

    def bed_runtime(self, bed_number: int) -> dict:
        bed_states = self.runtime_state.get("bed_states", {})
        return bed_states.get(
            int(bed_number),
            {
                "current_phase": "ready",
                "time_left": 0,
                "phase_total_seconds": 0,
                "phase_elapsed_seconds": 0,
                "active_customer_name": "",
                "tanning_seconds_remaining": 0,
                "cooldown_seconds_remaining": 0,
            },
        )

    def active_cycle_remaining_seconds_for_bed(self, bed_number: int) -> int:
        state = self.bed_runtime(bed_number)
        phase = state["current_phase"]
        if phase == "ready":
            return 0
        total = int(state["time_left"] or 0)
        if phase == "prep":
            total += int(state.get("tanning_seconds_remaining") or 0)
            total += int(state.get("cooldown_seconds_remaining") or 0)
        elif phase == "tanning":
            total += int(state.get("cooldown_seconds_remaining") or 0)
        return total

    def phase_label(self, phase):
        if phase == "prep":
            return "Prep"
        if phase == "tanning":
            return "Tanning"
        if phase == "cooldown":
            return "Cool Down"
        return "Ready"

    def phase_color(self, phase):
        if phase == "prep":
            return "#f57c00"
        if phase == "tanning":
            return "#d32f2f"
        if phase == "cooldown":
            return "#1976d2"
        return "#bdbdbd"

    def build_image_label(self, image_path: str, bed_type: str) -> QLabel:
        label = QLabel()
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        target_width = 220
        target_height = 130
        label.setFixedSize(target_width, target_height)
        label.setStyleSheet("background:#f3f7fb; border:1px solid #dbe8f5; border-radius:14px;")

        pixmap = QPixmap()
        resolved_image_path = resolve_local_image_path(image_path)
        if resolved_image_path:
            try:
                if resolved_image_path.startswith("http"):
                    with urlopen(resolved_image_path) as response:
                        pixmap.loadFromData(response.read())
                else:
                    pixmap.load(resolved_image_path)
            except Exception:
                pixmap = QPixmap()

        if not pixmap.isNull():
            label.setPixmap(
                cover_pixmap(pixmap, target_width, target_height)
            )
        else:
            label.setText(str(bed_type).replace("_", " ").title())
            label.setStyleSheet(
                "background:#f3f7fb; border:1px solid #dbe8f5; border-radius:14px; color:#5e6d78; font-size:14px;"
            )
        return label

    def build_rows(self):
        source_rows = list(self.repo.sunbed_status_rows(self.session.till_session))

        rows = []
        for index in range(8):
            row = source_rows[index] if index < len(source_rows) else None
            display_name = row["display_name"] if row else f"Bed {index + 1}"
            bed_type = row["bed_type"] if row else "lay_down"
            image_path = row["image_path"] if row else ""
            bed_number = index + 1
            queue_items = list(self.runtime_state.get("bed_queues", {}).get(bed_number, []))
            state = self.bed_runtime(bed_number)
            current_phase = state["current_phase"]

            if state["phase_total_seconds"] > 0:
                progress_percent = int((state["phase_elapsed_seconds"] / state["phase_total_seconds"]) * 100)
                progress_percent = max(0, min(progress_percent, 100))
            else:
                progress_percent = 0

            ordered_queue = []
            for item in queue_items:
                ordered_queue.append(
                    {
                        "customer_name": item["customer_name"],
                        "minutes_text": f"{int(item['minutes'])} mins",
                    }
                )

            queue_preview = [
                f"{queue_index + 1}. {item['customer_name']} - {item['minutes_text']}"
                for queue_index, item in enumerate(ordered_queue[:5])
            ]
            next_up_name = ordered_queue[0]["customer_name"] if ordered_queue else ""
            rows.append(
                {
                    "display_name": display_name,
                    "custom_name": row["custom_name"] if row else "",
                    "bed_number": int(row["bed_number"]) if row else bed_number,
                    "status_text": self.phase_label(current_phase) if current_phase != "ready" else "Ready",
                    "time_left": self.format_mmss(state["time_left"]),
                    "queue_count": len(ordered_queue),
                    "wait_text": self.format_mmss(
                        self.queue_wait_seconds(self.active_cycle_remaining_seconds_for_bed(bed_number), queue_items)
                    ),
                    "image_path": image_path,
                    "bed_type": bed_type,
                    "progress_percent": progress_percent,
                    "progress_color": self.phase_color(current_phase),
                    "queue_preview": queue_preview,
                    "active_customer_name": state["active_customer_name"],
                    "next_up_name": next_up_name,
                }
            )
        return rows

    def refresh_view(self):
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for index, row in enumerate(self.build_rows()):
            card = CardFrame(hero=True)
            layout = QVBoxLayout(card)
            layout.setContentsMargins(12, 12, 12, 12)
            layout.setSpacing(6)

            title = QLabel(row["display_name"])
            title.setStyleSheet("font-size:21px; font-weight:700; color:#0f4c6e;")
            layout.addWidget(title)

            for text in (
                f"Status: {row['status_text']}",
                f"Time Left: {row['time_left']}",
                f"Queue: {row['queue_count']}",
                f"Wait: {row['wait_text']}",
            ):
                label = QLabel(text)
                label.setStyleSheet("font-size:13px; color:#334155;")
                layout.addWidget(label)

            if row["active_customer_name"] and row["active_customer_name"] != "No Active Customer":
                current_label = QLabel(f"Now In Bed: {row['active_customer_name']}")
                current_label.setStyleSheet("font-size:13px; font-weight:700; color:#d32f2f;")
                layout.addWidget(current_label)

            if row["next_up_name"]:
                next_label = QLabel(f"Next Up: {row['next_up_name']}")
                next_label.setStyleSheet("font-size:13px; font-weight:700; color:#0f766e;")
                layout.addWidget(next_label)

            queue_title = QLabel("Waiting Queue")
            queue_title.setStyleSheet("font-size:13px; font-weight:700; color:#0f4c6e;")
            layout.addWidget(queue_title)

            if row["queue_preview"]:
                for item_text in row["queue_preview"][:2]:
                    item_label = QLabel(item_text)
                    item_label.setStyleSheet("font-size:12px; color:#5e6d78;")
                    layout.addWidget(item_label)
            else:
                empty = QLabel("No queue")
                empty.setStyleSheet("font-size:12px; color:#7b8794;")
                layout.addWidget(empty)

            image_shell = QWidget()
            image_shell.setFixedHeight(178)
            image_shell_layout = QVBoxLayout(image_shell)
            image_shell_layout.setContentsMargins(0, 0, 0, 0)
            image_shell_layout.setSpacing(10)
            image_shell_layout.addWidget(self.build_image_label(row["image_path"], row["bed_type"]), 0, Qt.AlignmentFlag.AlignHCenter)

            progress_bar = QProgressBar()
            progress_bar.setOrientation(Qt.Orientation.Horizontal)
            progress_bar.setRange(0, 100)
            progress_bar.setValue(int(row["progress_percent"]))
            progress_bar.setFormat(f"{row['progress_percent']}%")
            progress_bar.setFixedSize(220, 28)
            progress_bar.setStyleSheet(
                f"QProgressBar::chunk {{ background: {row['progress_color']}; border-radius: 10px; }}"
            )
            image_shell_layout.addWidget(progress_bar, 0, Qt.AlignmentFlag.AlignHCenter)

            layout.addWidget(image_shell, 1)
            self.grid.addWidget(card, index // 4, index % 4)


class NativeTillWindow(QMainWindow):
    def __init__(self, repo: TillRepository):
        super().__init__()
        self.repo = repo
        self.session = AppSession()
        self.current_customer_id: Optional[int] = None
        self.runtime_state = {
            **{
                "prep_minutes": int(repo.bed_timing_settings().get("prep_minutes", 3)),
                "cooldown_minutes": int(repo.bed_timing_settings().get("cooldown_minutes", 3)),
            },
            "gpio_bed_map": GPIO_BED_MAP,
            "pending_sale": None,
            "sale_baskets": {},
            "bed_queues": {index: [] for index in range(1, 9)},
            "bed_states": {
                index: {
                    "current_phase": "ready",
                    "time_left": 0,
                    "phase_total_seconds": 0,
                    "phase_elapsed_seconds": 0,
                    "active_customer_name": "No Active Customer",
                    "active_customer_account": "",
                    "active_payment_method": "",
                    "active_minutes": 0,
                    "active_sale": None,
                    "tanning_seconds_remaining": 0,
                    "cooldown_seconds_remaining": 0,
                    "cooldown_locked": False,
                    "waiting_for_feedback_start": False,
                    "feedback_wait_started_at": None,
                    "relay_armed_started_at": None,
                    "relay_fail_safe_deadline_at": None,
                }
                for index in range(1, 9)
            },
        }
        self.sunbed_return_widget: Optional[QWidget] = None
        self.sync_settings = repo.sync_settings()

        self.setWindowTitle("Salon Max Native Till")
        self.resize(1500, 900)

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.login_page = LoginPage(repo, self.handle_login, self.show_device_setup)
        self.device_setup_page = DeviceSetupPage(repo, self.handle_device_paired, self.show_login_or_setup)
        self.staff_bootstrap_page = StaffBootstrapPage(repo, self.handle_staff_bootstrap_complete, self.show_device_setup)
        self.business_locked_page = BusinessLockedPage(repo, self.show_login_or_setup, self.show_device_setup)
        self.open_till_page = OpenTillPage(
            repo,
            self.session,
            self.show_customer_search,
            self.logout,
            self.show_sunbed_status,
            self.refresh_business_access,
        )
        self.customer_search_page = CustomerSearchPage(
            repo,
            self.session,
            self.show_customer_summary,
            self.show_totals,
            self.logout,
            self.show_sunbed_status,
        )
        self.customer_summary_page = CustomerSummaryPage(
            repo,
            self.session,
            self.show_customer_search,
            self.show_sell_tanning,
            self.show_sell_course,
            self.show_sell_products,
            self.show_history,
            self.show_sunbed_status,
            self.show_totals,
        )
        self.sell_tanning_page = SellTanningPage(
            repo,
            self.session,
            self.return_from_tanning,
            self.show_customer_search,
            self.show_sell_course,
            self.show_sunbed_status,
            self.show_totals,
            self.handle_sale_complete,
            self.get_sale_basket,
            self.clear_sale_basket,
        )
        self.sell_products_page = SellProductsPage(
            repo,
            self.session,
            self.return_from_products,
            self.show_customer_search,
            self.show_sunbed_status,
            self.add_product_to_current_sale,
            self.show_sell_tanning,
        )
        self.sell_course_page = SellCoursePage(
            repo,
            self.session,
            self.return_from_course,
            self.show_customer_search,
            self.show_sunbed_status,
        )
        self.totals_page = TotalsPage(
            repo,
            self.session,
            self.show_customer_search,
            self.show_sunbed_status,
            self.handle_shift_closed,
        )
        self.sunbed_board_page = SunbedBoardPage(
            repo,
            self.session,
            self.runtime_state,
            self.return_from_sunbed_status,
            self.show_customer_search,
            self.show_totals,
            self.show_gpio_test,
        )

        for page in [
            self.login_page,
            self.device_setup_page,
            self.staff_bootstrap_page,
            self.business_locked_page,
            self.open_till_page,
            self.customer_search_page,
            self.customer_summary_page,
            self.sell_tanning_page,
            self.sell_products_page,
            self.sell_course_page,
            self.totals_page,
            self.sunbed_board_page,
        ]:
            self.stack.addWidget(page)

        self.sync_stub_timer = QTimer(self)
        self.sync_stub_timer.timeout.connect(self.run_sync_cycle)
        poll_seconds = max(5, int(self.sync_settings.get("sync_poll_seconds") or 15))
        self.sync_stub_timer.start(poll_seconds * 1000)
        self.run_sync_cycle()

        self.show_login_or_setup()

    def closeEvent(self, event):
        relay_off_all()
        super().closeEvent(event)

    def sync_session_to_pages(self):
        for page_name in [
            "open_till_page",
            "customer_search_page",
            "customer_summary_page",
            "sell_tanning_page",
            "sell_products_page",
            "sell_course_page",
            "totals_page",
            "sunbed_board_page",
        ]:
            page = getattr(self, page_name, None)
            if page is not None:
                page.session = self.session
        self.refresh_gpio_test_access()

    def refresh_runtime_settings_from_repo(self):
        timing = self.repo.bed_timing_settings()
        self.runtime_state["prep_minutes"] = int(timing.get("prep_minutes", 3))
        self.runtime_state["cooldown_minutes"] = int(timing.get("cooldown_minutes", 3))

    def gpio_test_allowed(self):
        staff = self.session.staff_row
        if staff is None:
            return False
        current = self.repo.fetch_one(
            "select role,is_active from staff_users where id=?", (int(staff["id"]),)
        )
        return bool(current and current["is_active"]
                    and str(current["role"]).strip().lower() in {"owner", "manager"})

    def refresh_gpio_test_access(self):
        allowed = self.gpio_test_allowed()
        for button in self.sunbed_board_page.findChildren(QPushButton):
            if button.text() == "GPIO Test":
                button.setVisible(allowed)
                button.setEnabled(allowed)

    def show_gpio_test(self):
        if not self.gpio_test_allowed():
            QMessageBox.warning(self, "Access restricted", "GPIO Test requires an owner or manager sign-in.")
            return
        dialog = GpioTestDialog(self)
        dialog.exec()

    def store_cloud_lease(self, cloud_lease: dict):
        current_identity = self.repo.local_device_identity()
        terminal_public_id = (
            str(current_identity["terminal_device_public_id"] or "").strip()
            if current_identity is not None
            else ""
        )
        if not terminal_public_id:
            return
        self.repo.store_local_licence_lease(
            terminal_device_public_id=terminal_public_id,
            licence_status=str(cloud_lease.get("licence_status") or "active"),
            signed_token=str(cloud_lease.get("signed_token") or ""),
            issued_at=str(cloud_lease.get("issued_at") or utc_now_text()),
            expires_at=str(cloud_lease.get("expires_at") or utc_now_text()),
            last_verified_at=utc_now_text(),
        )

    def show_business_locked(self, message: str = ""):
        self.session = AppSession()
        self.current_customer_id = None
        self.runtime_state["sale_baskets"] = {}
        self.sync_session_to_pages()
        self.business_locked_page.refresh_state(message)
        self.stack.setCurrentWidget(self.business_locked_page)

    def refresh_business_access(self) -> bool:
        pairing_required = device_pairing_required(self.repo)
        if pairing_required:
            return False
        cloud_lease = fetch_cloud_licence_lease(self.repo)
        if cloud_lease:
            self.store_cloud_lease(cloud_lease)
        locked, message = business_access_locked(self.repo)
        if locked:
            self.show_business_locked(message)
            return False
        cloud_config = fetch_cloud_device_config(self.repo)
        if cloud_config:
            self.repo.apply_cloud_device_config(cloud_config)
        return True

    def show_login_or_setup(self):
        pairing_required = device_pairing_required(self.repo)
        self.device_setup_page.set_pairing_required_mode(pairing_required)
        if pairing_required:
            self.stack.setCurrentWidget(self.device_setup_page)
            return
        if not self.refresh_business_access():
            return
        if staff_bootstrap_required(self.repo):
            self.stack.setCurrentWidget(self.staff_bootstrap_page)
            return
        self.login_page.refresh_branding()
        self.open_till_page.refresh_till_allocation()
        self.stack.setCurrentWidget(self.login_page)

    def show_device_setup(self):
        if not device_pairing_required(self.repo) and not self.open_till_page.owner_can_pair():
            QMessageBox.warning(self, "Access restricted", "Device pairing requires an owner sign-in.")
            return
        self.device_setup_page.set_pairing_required_mode(device_pairing_required(self.repo))
        self.stack.setCurrentWidget(self.device_setup_page)

    def handle_device_paired(self):
        self.refresh_business_access()
        self.login_page.refresh_branding()
        self.open_till_page.refresh_till_allocation()
        self.show_login_or_setup()

    def handle_staff_bootstrap_complete(self):
        self.show_login_or_setup()

    def handle_login(self, staff_row):
        self.session.staff_row = staff_row
        self.sync_session_to_pages()
        self.open_till_page.refresh_staff_display()
        self.stack.setCurrentWidget(self.open_till_page)

    def logout(self):
        self.session = AppSession()
        self.current_customer_id = None
        self.runtime_state["sale_baskets"] = {}
        self.sync_session_to_pages()
        self.show_login_or_setup()

    def show_customer_search(self):
        if not self.refresh_business_access():
            return
        self.refresh_runtime_settings_from_repo()
        self.sunbed_return_widget = self.customer_search_page
        self.customer_search_page.refresh()
        self.stack.setCurrentWidget(self.customer_search_page)

    def show_customer_summary(self, customer_id: int):
        self.current_customer_id = customer_id
        self.customer_summary_page.load_customer(customer_id)
        self.stack.setCurrentWidget(self.customer_summary_page)

    def show_sell_tanning(self, customer_id: int):
        self.current_customer_id = customer_id
        self.sell_tanning_page.load_customer(customer_id)
        self.stack.setCurrentWidget(self.sell_tanning_page)

    def show_sell_products(self, customer_id: int):
        self.current_customer_id = customer_id
        self.sell_products_page.load_customer(customer_id)
        self.stack.setCurrentWidget(self.sell_products_page)

    def get_sale_basket(self, customer_id: int | None) -> list[dict]:
        if not customer_id:
            return []
        baskets = self.runtime_state.setdefault("sale_baskets", {})
        return list(baskets.get(int(customer_id), []))

    def clear_sale_basket(self, customer_id: int | None):
        if not customer_id:
            return
        baskets = self.runtime_state.setdefault("sale_baskets", {})
        baskets.pop(int(customer_id), None)

    def add_product_to_current_sale(self, customer_id: int | None, item: dict):
        if not customer_id:
            return
        baskets = self.runtime_state.setdefault("sale_baskets", {})
        baskets.setdefault(int(customer_id), []).append(item)

    def record_busyness_snapshot(self):
        path = Path.home() / "till-build/salon-wait.json"
        session = self.session.till_session
        board = self.sunbed_board_page
        waits = [
            board.queue_wait_seconds(board.active_cycle_remaining_seconds(int(row["bed_number"])),
                                     board.bed_queue(int(row["bed_number"])))
            for row in board.bed_rows()
            if session is not None and row["site_is_active"] and row["status_text"] != "Inactive"
        ]
        if not waits:
            path.unlink(missing_ok=True)
            return
        staged = path.with_suffix(".next")
        staged.write_text(json.dumps({"timestamp": time.time(), "wait_seconds": min(86400, min(waits))}))
        staged.replace(path)

    def refresh_sunbed_settings(self):
        rows = self.repo.sunbed_status_rows(self.session.till_session)
        signature = tuple((row["id"], row["display_name"], row["image_path"], row["bed_type"],
                           row["status_text"], row["site_is_active"]) for row in rows)
        if signature != getattr(self, "_sunbed_settings_signature", None):
            self._sunbed_settings_signature = signature
            self.sunbed_board_page.refresh_view()

    def run_sync_cycle(self):
        self.refresh_sunbed_settings()
        self.refresh_gpio_test_access()
        self.open_till_page.refresh_pair_device_access()
        try:
            self.record_busyness_snapshot()
        except (OSError, ValueError, KeyError, IndexError):
            pass
        try:
            settings = self.repo.sync_settings()
            if int(settings.get("sync_stub_enabled") or 0) == 1:
                process_local_sync_stub(self.repo)
            else:
                process_sync_push_http(self.repo)
            if not device_pairing_required(self.repo):
                self.refresh_business_access()
                correction_payload = fetch_cloud_customer_corrections(self.repo)
                if correction_payload:
                    applied = self.repo.apply_cloud_customer_corrections(correction_payload)
                    if int(applied.get("applied_count") or 0) > 0:
                        self.refresh_customer_views(applied.get("customer_ids") or [])
                customer_directory_payload = fetch_cloud_customer_directory(self.repo)
                if customer_directory_payload:
                    imported = self.repo.apply_cloud_customer_directory(customer_directory_payload)
                    if int(imported.get("imported_count") or 0) > 0:
                        self.refresh_customer_views(imported.get("customer_ids") or [])
        except Exception:
            pass

    def refresh_customer_views(self, customer_ids: list[int]):
        refreshed_ids = {int(value) for value in customer_ids if str(value).strip()}
        if not refreshed_ids:
            return
        current_customer_id = int(self.current_customer_id or 0) if self.current_customer_id else 0
        if current_customer_id and current_customer_id in refreshed_ids:
            current_widget = self.stack.currentWidget()
            if current_widget == self.customer_summary_page:
                self.customer_summary_page.load_customer(current_customer_id)
            elif current_widget == self.sell_tanning_page:
                self.sell_tanning_page.load_customer(current_customer_id)
            elif current_widget == self.sell_products_page:
                self.sell_products_page.load_customer(current_customer_id)
            elif current_widget == self.sell_course_page:
                self.sell_course_page.load_customer(current_customer_id)
        if self.stack.currentWidget() == self.customer_search_page:
            self.customer_search_page.refresh()

    def show_sell_course(self, customer_id: int):
        self.current_customer_id = customer_id
        self.sell_course_page.load_customer(customer_id)
        self.stack.setCurrentWidget(self.sell_course_page)

    def return_from_tanning(self):
        if self.current_customer_id:
            self.show_customer_summary(self.current_customer_id)
        else:
            self.show_customer_search()

    def return_from_products(self):
        if self.current_customer_id:
            self.show_customer_summary(self.current_customer_id)
        else:
            self.show_customer_search()

    def return_from_course(self):
        if self.current_customer_id:
            self.show_customer_summary(self.current_customer_id)
        else:
            self.show_customer_search()

    def show_history(self, customer_id: int):
        customer = self.repo.get_customer(customer_id)
        if customer is None:
            return
        dialog = TransactionHistoryDialog(
            self.repo,
            customer_id,
            f"{customer['first_name']} {customer['last_name']}",
            self,
        )
        dialog.exec()

    def show_totals(self):
        self.totals_page.refresh()
        self.stack.setCurrentWidget(self.totals_page)

    def handle_shift_closed(self):
        self.session.till_session = None
        self.current_customer_id = None
        self.runtime_state["sale_baskets"] = {}
        self.sync_session_to_pages()
        self.open_till_page.refresh_till_allocation()
        self.open_till_page.refresh_staff_display()
        self.stack.setCurrentWidget(self.open_till_page)

    def return_from_sunbed_status(self):
        if self.sunbed_return_widget is not None:
            self.stack.setCurrentWidget(self.sunbed_return_widget)
            return
        if self.current_customer_id:
            self.show_customer_summary(self.current_customer_id)
            return
        self.show_customer_search()

    def show_sunbed_status(self):
        current_widget = self.stack.currentWidget()
        if current_widget not in (self.sunbed_board_page, self.login_page):
            self.sunbed_return_widget = current_widget
        self.sunbed_board_page.refresh_view()
        self.stack.setCurrentWidget(self.sunbed_board_page)

    def handle_sale_complete(self, pending_sale: PendingSale):
        self.sunbed_board_page.bed_queue(int(pending_sale.target_bed_number or 1)).append(
            self.sunbed_board_page.queue_item_from_pending(pending_sale)
        )
        self.sunbed_return_widget = self.customer_search_page
        self.show_sunbed_status()


def run():
    import sys

    base_dir = Path(__file__).resolve().parent.parent
    repo = TillRepository(default_database_path(base_dir))
    relay_off_all()

    app = QApplication(sys.argv)
    app.aboutToQuit.connect(relay_off_all)
    app.setStyleSheet(APP_STYLESHEET)
    window = NativeTillWindow(repo)
    if os.environ.get("SALONMAX_UPDATE_STATE"):
        from .update_runtime import update_tick
        window.update_timer = QTimer(window)
        window.update_timer.timeout.connect(lambda: update_tick(window, app))
        window.update_timer.start(2000)

    customer_display = CustomerDisplayWindow(repo, window.session, window.runtime_state)
    window.customer_display_window = customer_display

    def pick_screen(preferred_tokens: list[str], fallback):
        for screen in screens:
            name = str(screen.name() or "").lower()
            if any(token in name for token in preferred_tokens):
                return screen
        return fallback

    screens = app.screens()

    def arrange_windows():
        current_screens = app.screens()
        if len(current_screens) > 1:
            main_screen = pick_screen(["hdmi-a-1", "hdmi-1", "hdmi1"], app.primaryScreen() or current_screens[0])
            target_screen = pick_screen(
                ["hdmi-a-2", "hdmi-2", "hdmi2"],
                next((screen for screen in current_screens if screen is not main_screen), current_screens[1]),
            )
            if target_screen is main_screen:
                target_screen = next((screen for screen in current_screens if screen is not main_screen), current_screens[1])

            if window.windowHandle() is not None:
                window.windowHandle().setScreen(main_screen)
            window.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
            window.showNormal()
            window.setGeometry(main_screen.geometry())
            window.raise_()
            window.activateWindow()

            customer_display.hide()
            if customer_display.windowHandle() is not None:
                customer_display.windowHandle().setScreen(target_screen)
            customer_display.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
            customer_display.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
            if hasattr(Qt.WindowType, "WindowTransparentForInput"):
                customer_display.setWindowFlag(Qt.WindowType.WindowTransparentForInput, True)
            customer_display.showNormal()
            customer_display.setGeometry(target_screen.geometry())
            customer_display.showMaximized()
            customer_display.lower()
            window.raise_()
            window.activateWindow()
        else:
            window.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
            window.showNormal()
            single_screen = app.primaryScreen() or current_screens[0]
            window.setGeometry(single_screen.geometry())
            window.raise_()
            window.activateWindow()
            customer_display.hide()

    window.show()
    QTimer.singleShot(400, arrange_windows)
    return app.exec()



