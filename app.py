from __future__ import annotations

import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("SALONMAX_PLATFORM_DB_PATH") or BASE_DIR / "salonmax_platform.db")

app = Flask(__name__)
app.secret_key = os.environ.get("SALONMAX_SECRET_KEY", "change-this-before-live")


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str | None):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(value: str, fallback: str = "business") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or fallback


def make_business_id(name: str) -> str:
    base = f"biz_{slugify(name).replace('-', '_')}"
    candidate = base
    suffix = 2
    while query_one("select id from businesses where public_id = ?", (candidate,)):
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def make_site_code(name: str) -> str:
    return slugify(name, "site")[:24]


def terminal_health(row) -> dict:
    last_seen = parse_utc(row["last_seen_at"])
    if row["status"] == "paired" and last_seen:
        age = datetime.now(timezone.utc) - last_seen
        if age <= timedelta(minutes=2):
            return {"status": "active", "label": "Active"}
        return {"status": "stale", "label": "Check-In Stale"}
    if row["status"] == "paired":
        return {"status": "stale", "label": "Paired, No Check-In"}
    return {"status": "neutral", "label": row["status"].replace("_", " ").title()}


def db() -> sqlite3.Connection:
    if "db" not in g:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def execute(sql: str, params: tuple = ()) -> None:
    db().execute(sql, params)
    db().commit()


def query_one(sql: str, params: tuple = ()):
    return db().execute(sql, params).fetchone()


def query_all(sql: str, params: tuple = ()):
    return db().execute(sql, params).fetchall()


def init_db() -> None:
    execute(
        """
        create table if not exists businesses (
            id integer primary key autoincrement,
            public_id text not null unique,
            name text not null,
            status text not null default 'active',
            subscription_status text not null default 'active',
            plan text not null default 'standard',
            contact_name text not null default '',
            contact_email text not null default '',
            contact_phone text not null default '',
            address_line_1 text not null default '',
            address_line_2 text not null default '',
            town text not null default '',
            postcode text not null default '',
            notes text not null default '',
            pairing_code text not null default '',
            archived_at text,
            created_at text not null,
            updated_at text not null
        )
        """
    )
    ensure_column("terminals", "app_version", "text not null default ''")
    ensure_column("terminals", "last_lease_status", "text not null default ''")
    ensure_column("terminals", "last_access_message", "text not null default ''")


def ensure_column(table: str, column: str, ddl: str) -> None:
    existing = {row["name"] for row in query_all(f"pragma table_info({table})")}
    if column not in existing:
        execute(f"alter table {table} add column {column} {ddl}")
    execute(
        """
        create table if not exists sites (
            id integer primary key autoincrement,
            business_public_id text not null,
            name text not null,
            code text not null,
            status text not null default 'active',
            address_line_1 text not null default '',
            address_line_2 text not null default '',
            town text not null default '',
            postcode text not null default '',
            notes text not null default '',
            archived_at text,
            created_at text not null,
            updated_at text not null,
            foreign key (business_public_id) references businesses(public_id)
        )
        """
    )
    execute(
        """
        create table if not exists terminals (
            id integer primary key autoincrement,
            business_public_id text not null,
            site_id integer,
            terminal_name text not null,
            device_public_id text not null default '',
            status text not null default 'awaiting_pairing',
            last_seen_at text,
            created_at text not null,
            updated_at text not null,
            foreign key (business_public_id) references businesses(public_id),
            foreign key (site_id) references sites(id)
        )
        """
    )


@app.before_request
def before_request():
    init_db()
    if request.path in {"/healthz", "/platform-login"} or request.path.startswith("/static/") or request.path.startswith("/v1/"):
        return None
    if not session.get("salonmax_platform_authed"):
        return redirect(url_for("platform_login", next=request.full_path.rstrip("?")))
    return None


@app.get("/healthz")
def healthz():
    return {"ok": True, "product": "salon-max-platform"}


def json_error(code: str, message: str, status: int = 400):
    return jsonify({"ok": False, "error": {"code": code, "message": message}}), status


def request_json() -> dict:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def business_access_state(business) -> tuple[str, str]:
    if business["archived_at"]:
        return "archived", "This business is archived and trading is locked."
    if business["status"] == "suspended":
        return "suspended", "This business is suspended and trading is locked."
    if business["subscription_status"] in {"suspended", "cancelled"}:
        return business["subscription_status"], "This subscription is not active and trading is locked."
    return "active", "Trading enabled."


@app.post("/v1/pairing/claim")
def api_pairing_claim():
    payload = request_json()
    pairing_code = str(payload.get("pairing_code") or payload.get("code") or "").strip().upper()
    terminal_device_id = str(payload.get("terminal_device_id") or payload.get("device_id") or "").strip()
    terminal_name = str(payload.get("terminal_name") or terminal_device_id or "Salon Till").strip()
    site_code = str(payload.get("site_code") or "").strip()
    if not pairing_code:
        return json_error("PAIRING_CODE_REQUIRED", "Pairing code is required.")
    if not terminal_device_id:
        return json_error("TERMINAL_DEVICE_ID_REQUIRED", "Terminal device id is required.")

    business = query_one(
        "select * from businesses where pairing_code = ? and archived_at is null",
        (pairing_code,),
    )
    if business is None:
        return json_error("PAIRING_CODE_NOT_FOUND", "Pairing code was not found.", status=404)

    site = None
    if site_code:
        site = query_one(
            "select * from sites where business_public_id = ? and code = ? and archived_at is null",
            (business["public_id"], site_code),
        )
    if site is None:
        site = query_one(
            "select * from sites where business_public_id = ? and archived_at is null order by id limit 1",
            (business["public_id"],),
        )

    timestamp = now_utc()
    existing = query_one(
        "select * from terminals where device_public_id = ?",
        (terminal_device_id,),
    )
    if existing:
        execute(
            """
            update terminals
            set business_public_id = ?, site_id = ?, terminal_name = ?, status = 'paired',
                last_seen_at = ?, updated_at = ?
            where id = ?
            """,
            (business["public_id"], site["id"] if site else None, terminal_name, timestamp, timestamp, existing["id"]),
        )
    else:
        execute(
            """
            insert into terminals (
                business_public_id, site_id, terminal_name, device_public_id, status,
                last_seen_at, created_at, updated_at
            ) values (?, ?, ?, ?, 'paired', ?, ?, ?)
            """,
            (business["public_id"], site["id"] if site else None, terminal_name, terminal_device_id, timestamp, timestamp, timestamp),
        )

    licence_status, access_message = business_access_state(business)
    return jsonify(
        {
            "ok": True,
            "data": {
                "business_account_public_id": business["public_id"],
                "business_name": business["name"],
                "site_id": site["id"] if site else None,
                "site_code": site["code"] if site else "",
                "site_name": site["name"] if site else "",
                "terminal_device_id": terminal_device_id,
                "licence_status": licence_status,
                "access_message": access_message,
            },
        }
    )


@app.post("/v1/licence/check-in")
def api_licence_check_in():
    payload = request_json()
    business_public_id = (
        request.headers.get("X-SalonMax-Business-Id")
        or payload.get("business_account_public_id")
        or payload.get("business_id")
        or ""
    )
    terminal_device_id = (
        request.headers.get("X-SalonMax-Device-Id")
        or payload.get("terminal_device_id")
        or payload.get("device_id")
        or ""
    )
    business_public_id = str(business_public_id).strip()
    terminal_device_id = str(terminal_device_id).strip()
    app_version = str(payload.get("app_version") or "").strip()
    if not business_public_id:
        return json_error("BUSINESS_ID_REQUIRED", "Business id is required.")
    if not terminal_device_id:
        return json_error("TERMINAL_DEVICE_ID_REQUIRED", "Terminal device id is required.")

    business = query_one("select * from businesses where public_id = ?", (business_public_id,))
    if business is None:
        return json_error("BUSINESS_NOT_FOUND", "Business account was not found.", status=404)

    timestamp = now_utc()
    terminal = query_one("select * from terminals where device_public_id = ?", (terminal_device_id,))
    if terminal is None:
        execute(
            """
            insert into terminals (
                business_public_id, terminal_name, device_public_id, status,
                last_seen_at, app_version, created_at, updated_at
            ) values (?, ?, ?, 'paired', ?, ?, ?, ?)
            """,
            (business_public_id, terminal_device_id, terminal_device_id, timestamp, app_version, timestamp, timestamp),
        )
    else:
        execute(
            """
            update terminals
            set business_public_id = ?, status = 'paired', last_seen_at = ?,
                app_version = ?, updated_at = ?
            where id = ?
            """,
            (business_public_id, timestamp, app_version, timestamp, terminal["id"]),
        )

    licence_status, access_message = business_access_state(business)
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(days=30)
    grace_ends_at = issued_at + timedelta(days=37)
    execute(
        """
        update terminals
        set last_lease_status = ?, last_access_message = ?, updated_at = ?
        where device_public_id = ?
        """,
        (licence_status, access_message, timestamp, terminal_device_id),
    )
    return jsonify(
        {
            "ok": True,
            "data": {
                "licence_status": licence_status,
                "access_message": access_message,
                "issued_at": iso_utc(issued_at),
                "expires_at": iso_utc(expires_at),
                "grace_ends_at": iso_utc(grace_ends_at),
                "signed_token": f"sm-lease:{terminal_device_id}:{iso_utc(issued_at)}",
            },
        }
    )


@app.get("/v1/devices/<terminal_device_id>/config")
def api_device_config(terminal_device_id: str):
    terminal = query_one("select * from terminals where device_public_id = ?", (terminal_device_id,))
    if terminal is None:
        return json_error("TERMINAL_NOT_FOUND", "Terminal has not been paired.", status=404)
    business = query_one("select * from businesses where public_id = ?", (terminal["business_public_id"],))
    if business is None:
        return json_error("BUSINESS_NOT_FOUND", "Business account was not found.", status=404)
    site = query_one("select * from sites where id = ?", (terminal["site_id"],)) if terminal["site_id"] else None
    licence_status, access_message = business_access_state(business)
    return jsonify(
        {
            "ok": True,
            "data": {
                "business_account_public_id": business["public_id"],
                "business_name": business["name"],
                "site_id": site["id"] if site else None,
                "site_code": site["code"] if site else "",
                "site_name": site["name"] if site else "",
                "terminal_device_id": terminal_device_id,
                "licence_status": licence_status,
                "access_message": access_message,
            },
        }
    )


@app.route("/")
def home():
    return redirect(url_for("platform_owner"))


@app.route("/platform-login", methods=["GET", "POST"])
def platform_login():
    notice = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        expected_username = os.environ.get("SALONMAX_PLATFORM_ADMIN_USERNAME", "admin")
        expected_password = os.environ.get("SALONMAX_PLATFORM_ADMIN_PASSWORD", "admin")
        expected_hash = os.environ.get("SALONMAX_PLATFORM_ADMIN_PASSWORD_HASH", "")
        password_ok = check_password_hash(expected_hash, password) if expected_hash else password == expected_password
        if username == expected_username and password_ok:
            session["salonmax_platform_authed"] = True
            return redirect(request.args.get("next") or url_for("platform_owner"))
        notice = "Login failed. Check the Salon Max owner username and password."
    return render_template("login.html", notice=notice)


@app.route("/platform-logout")
def platform_logout():
    session.clear()
    return redirect(url_for("platform_login"))


@app.route("/platform")
@app.route("/platform/owner")
def platform_owner():
    search = request.args.get("search", "").strip()
    params: list[str] = []
    where = "where archived_at is null"
    if search:
        like = f"%{search}%"
        where += """
            and (
                name like ? or public_id like ? or contact_name like ? or
                contact_email like ? or contact_phone like ? or town like ? or postcode like ?
            )
        """
        params.extend([like] * 7)
    businesses = query_all(
        f"""
        select
            businesses.*,
            (select count(*) from sites where sites.business_public_id = businesses.public_id and sites.archived_at is null) as site_count,
            (select count(*) from terminals where terminals.business_public_id = businesses.public_id) as terminal_count
        from businesses
        {where}
        order by updated_at desc, id desc
        """,
        tuple(params),
    )
    archived = query_all(
        """
        select
            businesses.*,
            (select count(*) from sites where sites.business_public_id = businesses.public_id) as site_count,
            (select count(*) from terminals where terminals.business_public_id = businesses.public_id) as terminal_count
        from businesses
        where archived_at is not null
        order by archived_at desc
        """
    )
    return render_template(
        "owner.html",
        businesses=businesses,
        archived=archived,
        search=search,
        notice=request.args.get("notice", "").strip(),
    )


@app.post("/platform/businesses/create")
def create_business():
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(url_for("platform_owner", notice="Business name is required."))
    public_id = make_business_id(name)
    timestamp = now_utc()
    pairing_code = secrets.token_hex(5).upper()
    execute(
        """
        insert into businesses (
            public_id, name, status, subscription_status, plan,
            contact_name, contact_email, contact_phone, town, postcode,
            pairing_code, created_at, updated_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            public_id,
            name,
            request.form.get("status", "active").strip() or "active",
            request.form.get("subscription_status", "active").strip() or "active",
            request.form.get("plan", "standard").strip() or "standard",
            request.form.get("contact_name", "").strip(),
            request.form.get("contact_email", "").strip(),
            request.form.get("contact_phone", "").strip(),
            request.form.get("town", "").strip(),
            request.form.get("postcode", "").strip(),
            pairing_code,
            timestamp,
            timestamp,
        ),
    )
    return redirect(url_for("business_detail", public_id=public_id, notice="Business created."))


@app.route("/platform/business/<public_id>")
def business_detail(public_id: str):
    business = query_one("select * from businesses where public_id = ?", (public_id,))
    if business is None:
        return redirect(url_for("platform_owner", notice="Business not found."))
    sites = query_all(
        "select * from sites where business_public_id = ? order by archived_at is not null, id",
        (public_id,),
    )
    terminals = query_all(
        "select * from terminals where business_public_id = ? order by id",
        (public_id,),
    )
    terminal_rows = []
    for terminal in terminals:
        item = dict(terminal)
        item["health"] = terminal_health(terminal)
        terminal_rows.append(item)
    return render_template(
        "business.html",
        business=business,
        sites=sites,
        terminals=terminal_rows,
        notice=request.args.get("notice", "").strip(),
    )


@app.post("/platform/business/<public_id>/update")
def update_business(public_id: str):
    business = query_one("select * from businesses where public_id = ?", (public_id,))
    if business is None:
        return redirect(url_for("platform_owner", notice="Business not found."))
    execute(
        """
        update businesses
        set name = ?, status = ?, subscription_status = ?, plan = ?,
            contact_name = ?, contact_email = ?, contact_phone = ?,
            address_line_1 = ?, address_line_2 = ?, town = ?, postcode = ?,
            notes = ?, updated_at = ?
        where public_id = ?
        """,
        (
            request.form.get("name", "").strip() or business["name"],
            request.form.get("status", "active").strip() or "active",
            request.form.get("subscription_status", "active").strip() or "active",
            request.form.get("plan", "standard").strip() or "standard",
            request.form.get("contact_name", "").strip(),
            request.form.get("contact_email", "").strip(),
            request.form.get("contact_phone", "").strip(),
            request.form.get("address_line_1", "").strip(),
            request.form.get("address_line_2", "").strip(),
            request.form.get("town", "").strip(),
            request.form.get("postcode", "").strip(),
            request.form.get("notes", "").strip(),
            now_utc(),
            public_id,
        ),
    )
    return redirect(url_for("business_detail", public_id=public_id, notice="Business details saved."))


@app.post("/platform/business/<public_id>/archive")
def archive_business(public_id: str):
    execute("update businesses set archived_at = ?, updated_at = ? where public_id = ?", (now_utc(), now_utc(), public_id))
    return redirect(url_for("platform_owner", notice="Business archived. Data has been kept."))


@app.post("/platform/business/<public_id>/restore")
def restore_business(public_id: str):
    execute("update businesses set archived_at = null, updated_at = ? where public_id = ?", (now_utc(), public_id))
    return redirect(url_for("platform_owner", notice="Business restored."))


@app.post("/platform/business/<public_id>/new-pairing-code")
def regenerate_pairing_code(public_id: str):
    pairing_code = secrets.token_hex(5).upper()
    execute("update businesses set pairing_code = ?, updated_at = ? where public_id = ?", (pairing_code, now_utc(), public_id))
    return redirect(url_for("business_detail", public_id=public_id, notice="New pairing code generated."))


@app.post("/platform/business/<public_id>/sites/create")
def create_site(public_id: str):
    business = query_one("select * from businesses where public_id = ?", (public_id,))
    if business is None:
        return redirect(url_for("platform_owner", notice="Business not found."))
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(url_for("business_detail", public_id=public_id, notice="Site name is required."))
    timestamp = now_utc()
    execute(
        """
        insert into sites (
            business_public_id, name, code, status, address_line_1, address_line_2,
            town, postcode, notes, created_at, updated_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            public_id,
            name,
            request.form.get("code", "").strip() or make_site_code(name),
            request.form.get("status", "active").strip() or "active",
            request.form.get("address_line_1", "").strip(),
            request.form.get("address_line_2", "").strip(),
            request.form.get("town", "").strip(),
            request.form.get("postcode", "").strip(),
            request.form.get("notes", "").strip(),
            timestamp,
            timestamp,
        ),
    )
    return redirect(url_for("business_detail", public_id=public_id, notice="Site added."))


@app.post("/platform/business/<public_id>/sites/<int:site_id>/update")
def update_site(public_id: str, site_id: int):
    site = query_one("select * from sites where id = ? and business_public_id = ?", (site_id, public_id))
    if site is None:
        return redirect(url_for("business_detail", public_id=public_id, notice="Site not found."))
    execute(
        """
        update sites
        set name = ?, code = ?, status = ?, address_line_1 = ?, address_line_2 = ?,
            town = ?, postcode = ?, notes = ?, updated_at = ?
        where id = ? and business_public_id = ?
        """,
        (
            request.form.get("name", "").strip() or site["name"],
            request.form.get("code", "").strip() or site["code"],
            request.form.get("status", "active").strip() or "active",
            request.form.get("address_line_1", "").strip(),
            request.form.get("address_line_2", "").strip(),
            request.form.get("town", "").strip(),
            request.form.get("postcode", "").strip(),
            request.form.get("notes", "").strip(),
            now_utc(),
            site_id,
            public_id,
        ),
    )
    return redirect(url_for("business_detail", public_id=public_id, notice="Site saved."))


@app.post("/platform/business/<public_id>/sites/<int:site_id>/archive")
def archive_site(public_id: str, site_id: int):
    execute(
        "update sites set archived_at = ?, updated_at = ? where id = ? and business_public_id = ?",
        (now_utc(), now_utc(), site_id, public_id),
    )
    return redirect(url_for("business_detail", public_id=public_id, notice="Site archived. Data has been kept."))


@app.post("/platform/business/<public_id>/sites/<int:site_id>/restore")
def restore_site(public_id: str, site_id: int):
    execute(
        "update sites set archived_at = null, updated_at = ? where id = ? and business_public_id = ?",
        (now_utc(), site_id, public_id),
    )
    return redirect(url_for("business_detail", public_id=public_id, notice="Site restored."))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5001")))
