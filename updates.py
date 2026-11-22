"""Approved signed native-till releases and per-terminal deployment status."""
import json
import secrets
from pathlib import Path

from flask import Blueprint, abort, g, jsonify, redirect, request, send_file, session

import provider as p

bp = Blueprint("updates", __name__)
RELEASES = Path(__file__).parent / "pi" / "releases"


def init_schema():
    p.platform_execute("""create table if not exists managed_till_updates (
        terminal_id text primary key, business_id text not null, installed text not null default '',
        available text not null default '', state text not null default 'unknown',
        message text not null default '', checked_at text not null default '',
        installed_at text not null default '', request_id text not null default ''
    )""")


def manifest(version=None):
    if version is None:
        pointer = RELEASES / "stable.json"
        if not pointer.exists():
            return None
        version = json.loads(pointer.read_text())["version"]
    if not isinstance(version, str) or not p.re.fullmatch(r"[0-9][0-9A-Za-z._-]{0,63}", version):
        return None
    path = RELEASES / (version + ".json")
    return json.loads(path.read_text()) if path.exists() else None


@bp.get("/v1/updates/manifest")
def device_manifest():
    terminal = g.device_terminal
    release = manifest(terminal["desired_app_version"] or None)
    if not release:
        return jsonify(ok=False, error="No approved release matches this target."), 409
    channel = terminal["app_update_channel"] or "stable"
    if channel != release["release"]["channel"]:
        return jsonify(ok=False, error="No approved release matches this channel."), 409
    row = p.platform_query_one("select request_id from managed_till_updates where terminal_id=?",
                               (terminal["terminal_device_public_id"],))
    response = jsonify(ok=True, **release, request_id=row["request_id"] if row else "")
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/v1/updates/artifact/<version>")
def artifact(version):
    if not manifest(version):
        abort(404)
    return send_file(RELEASES / (version + ".zip"), mimetype="application/zip", max_age=0)


@bp.post("/v1/updates/report")
def report():
    terminal = g.device_terminal
    data = request.get_json(silent=True) or {}
    allowed = {"current", "downloading", "waiting_idle", "installing", "rolled_back", "error"}
    if data.get("state") not in allowed or any(not isinstance(data.get(key, ""), str)
            for key in ("installed", "available", "message", "installed_at")):
        abort(400)
    device = terminal["terminal_device_public_id"]
    p.platform_execute("""
        insert into managed_till_updates
            (terminal_id,business_id,installed,available,state,message,checked_at,installed_at)
        values (?,?,?,?,?,?,?,?)
        on conflict (terminal_id) do update set installed=excluded.installed,
            available=excluded.available,state=excluded.state,message=excluded.message,
            checked_at=excluded.checked_at,installed_at=excluded.installed_at
    """, (device, terminal["business_account_public_id"], data.get("installed", "")[:64],
          data.get("available", "")[:64], data["state"], data.get("message", "")[:500],
          p.now_utc_text(), data.get("installed_at", "")[:40]))
    if data["state"] == "current":
        p.platform_execute("update cloud_terminal_registry set app_version_reported=? where terminal_device_public_id=?",
                           (data.get("installed", "")[:64], device))
    return jsonify(ok=True)


@bp.post("/platform/updates/managed/<device>")
def request_update(device):
    if not (session.get("platform_admin_authenticated") or session.get("salonmax_platform_authed")):
        abort(403)
    token = session.get("managed_updates_csrf")
    if not token or not secrets.compare_digest(token, request.form.get("csrf", "")):
        abort(400)
    terminal = p.platform_query_one("select * from cloud_terminal_registry where terminal_device_public_id=?", (device,))
    release = manifest()
    if not terminal or not release:
        abort(404)
    p.platform_execute("""update cloud_terminal_registry set desired_app_version=?,app_update_channel='stable'
                          where terminal_device_public_id=?""", (release["release"]["version"], device))
    p.platform_execute("""insert into managed_till_updates (terminal_id,business_id,request_id)
        values (?,?,?) on conflict (terminal_id) do update set request_id=excluded.request_id""",
        (device, terminal["business_account_public_id"], secrets.token_hex(16)))
    return redirect("/platform/updates", code=303)


def template_context():
    if request.path != "/platform/updates":
        return {}
    if not (session.get("platform_admin_authenticated") or session.get("salonmax_platform_authed")):
        return {}
    csrf = session.setdefault("managed_updates_csrf", secrets.token_urlsafe(32))
    rows = p.platform_query_all("""
        select t.terminal_device_public_id,t.terminal_name,b.business_name,u.*
        from cloud_terminal_registry t
        join cloud_business_accounts b on b.business_account_public_id=t.business_account_public_id
        left join managed_till_updates u on u.terminal_id=t.terminal_device_public_id
        where t.management_status<>'retired' and b.status<>'deleted'
        order by b.business_name,t.terminal_name
    """)
    return {"managed_updates": rows, "managed_updates_csrf": csrf, "approved_release": manifest()}
