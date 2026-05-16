from __future__ import annotations

import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # SQLite remains the local fallback.
    psycopg = None
    dict_row = None


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("SALONMAX_PLATFORM_DB_PATH") or BASE_DIR / "salonmax_platform.db")
DATABASE_URL = os.environ.get("SALONMAX_DATABASE_URL") or os.environ.get("DATABASE_URL")

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


def using_postgres() -> bool:
    return bool(DATABASE_URL and DATABASE_URL.startswith(("postgres://", "postgresql://")))


def db():
    if "db" not in g:
        if using_postgres():
            if psycopg is None:
                raise RuntimeError("PostgreSQL is configured but psycopg is not installed.")
            conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        else:
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


def adapt_sql(sql: str) -> str:
    if not using_postgres():
        return sql
    return (
        sql.replace("integer primary key autoincrement", "bigserial primary key")
        .replace("?", "%s")
    )


def execute(sql: str, params: tuple = ()) -> None:
    db().execute(adapt_sql(sql), params)
    db().commit()


def query_one(sql: str, params: tuple = ()):
    return db().execute(adapt_sql(sql), params).fetchone()


def query_all(sql: str, params: tuple = ()):
    return db().execute(adapt_sql(sql), params).fetchall()


def ensure_column(table: str, column: str, ddl: str) -> None:
    if using_postgres():
        existing = {
            row["column_name"]
            for row in query_all(
                """
                select column_name
                from information_schema.columns
                where table_schema = 'public' and table_name = ?
                """,
                (table,),
            )
        }
    else:
        existing = {row["name"] for row in query_all(f"pragma table_info({table})")}
    if column not in existing:
        execute(f"alter table {table} add column {column} {ddl}")


def last_insert_id() -> int:
    if using_postgres():
        return int(query_one("select lastval() as value")["value"])
    return int(query_one("select last_insert_rowid() as value")["value"])


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
    ensure_column("terminals", "app_version", "text not null default ''")
    ensure_column("terminals", "target_app_version", "text not null default ''")
    ensure_column("terminals", "last_lease_status", "text not null default ''")
    ensure_column("terminals", "last_access_message", "text not null default ''")
    ensure_column("terminals", "support_notes", "text not null default ''")
    ensure_column("terminals", "retired_at", "text")


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
    return {"ok": True, "product": "salon-max-platform", "database": "postgres" if using_postgres() else "sqlite"}


@app.get("/platform/export.json")
def platform_export_json():
    return jsonify(
        {
            "ok": True,
            "product": "salon-max-platform",
            "database": "postgres" if using_postgres() else "sqlite",
            "exported_at": now_utc(),
            "businesses": [dict(row) for row in query_all("select * from businesses order by id")],
            "sites": [dict(row) for row in query_all("select * from sites order by id")],
            "terminals": [dict(row) for row in query_all("select * from terminals order by id")],
        }
    )


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


def terminal_access_state(terminal, business) -> tuple[str, str]:
    business_status, business_message = business_access_state(business)
    if business_status != "active":
        return business_status, business_message
    if terminal["status"] in {"suspended", "retired"}:
        return terminal["status"], f"This terminal is {terminal['status']} and trading is locked."
    return "active", "Trading enabled."


@app.post("/v1/devices/pair")
@app.post("/v1/pairing/claim")
def api_pairing_claim():
    payload = request_json()
    pairing_code = str(payload.get("pairing_code") or payload.get("code") or "").strip().upper()
    device_serial = str(payload.get("device_serial") or "").strip()
    terminal_device_id = str(
        payload.get("terminal_device_public_id")
        or payload.get("terminal_device_id")
        or payload.get("device_id")
        or ""
    ).strip()
    terminal_name = str(payload.get("terminal_name") or terminal_device_id or "Salon Till").strip()
    site_code = str(payload.get("site_code") or "").strip()
    if not pairing_code:
        return json_error("PAIRING_CODE_REQUIRED", "Pairing code is required.")

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
    if site is None:
        timestamp = now_utc()
        execute(
            """
            insert into sites (business_public_id, name, code, status, created_at, updated_at)
            values (?, 'Main Site', 'main-site', 'active', ?, ?)
            """,
            (business["public_id"], timestamp, timestamp),
        )
        site = query_one(
            "select * from sites where business_public_id = ? and archived_at is null order by id limit 1",
            (business["public_id"],),
        )

    timestamp = now_utc()
    if not terminal_device_id:
        waiting_terminal = query_one(
            """
            select * from terminals
            where business_public_id = ? and (device_public_id = '' or device_public_id is null)
            order by id limit 1
            """,
            (business["public_id"],),
        )
        if waiting_terminal:
            terminal_device_id = f"term_{waiting_terminal['id']}"
        else:
            serial_suffix = re.sub(r"[^a-zA-Z0-9]+", "", device_serial)[-8:].lower() or secrets.token_hex(4)
            terminal_device_id = f"term_{slugify(terminal_name, 'till')}_{serial_suffix}"

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

    terminal = query_one("select * from terminals where device_public_id = ?", (terminal_device_id,))
    licence_status, access_message = terminal_access_state(terminal, business)
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(days=30)
    grace_ends_at = issued_at + timedelta(days=37)
    signed_token = f"sm-lease:{terminal_device_id}:{iso_utc(issued_at)}"
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
                "business_account_public_id": business["public_id"],
                "business_name": business["name"],
                "site_id": site["id"] if site else None,
                "site_code": site["code"] if site else "",
                "site_public_id": site["code"] if site else "",
                "site_name": site["name"] if site else "",
                "terminal_device_id": terminal_device_id,
                "terminal_device_public_id": terminal_device_id,
                "terminal_name": terminal_name,
                "install_mode": "fresh_install",
                "licence_status": licence_status,
                "access_message": access_message,
                "issued_at": iso_utc(issued_at),
                "expires_at": iso_utc(expires_at),
                "grace_ends_at": iso_utc(grace_ends_at),
                "signed_token": signed_token,
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
            set business_public_id = ?,
                status = case when status in ('suspended', 'retired') then status else 'paired' end,
                last_seen_at = ?,
                app_version = ?, updated_at = ?
            where id = ?
            """,
            (business_public_id, timestamp, app_version, timestamp, terminal["id"]),
        )

    terminal = query_one("select * from terminals where device_public_id = ?", (terminal_device_id,))
    licence_status, access_message = terminal_access_state(terminal, business)
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
    licence_status, access_message = terminal_access_state(terminal, business)
    execute(
        "update terminals set last_seen_at = ?, updated_at = ? where device_public_id = ?",
        (now_utc(), now_utc(), terminal_device_id),
    )
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
                "target_app_version": terminal["target_app_version"],
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
            (select count(*) from terminals where terminals.business_public_id = businesses.public_id and terminals.retired_at is null) as terminal_count,
            (select count(*) from terminals where terminals.business_public_id = businesses.public_id and terminals.status = 'paired' and terminals.retired_at is null) as paired_terminal_count,
            (select max(last_seen_at) from terminals where terminals.business_public_id = businesses.public_id and terminals.retired_at is null) as last_check_in
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
            (select count(*) from terminals where terminals.business_public_id = businesses.public_id and terminals.retired_at is null) as terminal_count,
            (select count(*) from terminals where terminals.business_public_id = businesses.public_id and terminals.status = 'paired' and terminals.retired_at is null) as paired_terminal_count,
            (select max(last_seen_at) from terminals where terminals.business_public_id = businesses.public_id and terminals.retired_at is null) as last_check_in
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
        summary={
            "site_count": sum(int(row["site_count"] or 0) for row in businesses),
            "paired_terminal_count": sum(int(row["paired_terminal_count"] or 0) for row in businesses),
        },
        notice=request.args.get("notice", "").strip(),
    )


@app.route("/platform/onboard")
def platform_onboard():
    return render_template("onboard.html", title="Onboard New Salon", notice=request.args.get("notice", "").strip())


@app.post("/platform/onboard/create")
def create_onboarded_business():
    name = request.form.get("name", "").strip()
    site_name = request.form.get("site_name", "").strip()
    terminal_name = request.form.get("terminal_name", "").strip()
    if not name:
        return redirect(url_for("platform_onboard", notice="Business name is required."))
    public_id = make_business_id(name)
    timestamp = now_utc()
    pairing_code = secrets.token_hex(5).upper()
    execute(
        """
        insert into businesses (
            public_id, name, status, subscription_status, plan,
            contact_name, contact_email, contact_phone,
            address_line_1, address_line_2, town, postcode, notes,
            pairing_code, created_at, updated_at
        ) values (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            public_id,
            name,
            request.form.get("subscription_status", "trial").strip() or "trial",
            request.form.get("plan", "standard").strip() or "standard",
            request.form.get("contact_name", "").strip(),
            request.form.get("contact_email", "").strip(),
            request.form.get("contact_phone", "").strip(),
            request.form.get("address_line_1", "").strip(),
            request.form.get("address_line_2", "").strip(),
            request.form.get("town", "").strip(),
            request.form.get("postcode", "").strip(),
            request.form.get("notes", "").strip(),
            pairing_code,
            timestamp,
            timestamp,
        ),
    )
    site_id = None
    if site_name:
        execute(
            """
            insert into sites (
                business_public_id, name, code, status, address_line_1, address_line_2,
                town, postcode, notes, created_at, updated_at
            ) values (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                public_id,
                site_name,
                request.form.get("site_code", "").strip() or make_site_code(site_name),
                request.form.get("site_address_line_1", "").strip() or request.form.get("address_line_1", "").strip(),
                request.form.get("site_address_line_2", "").strip() or request.form.get("address_line_2", "").strip(),
                request.form.get("site_town", "").strip() or request.form.get("town", "").strip(),
                request.form.get("site_postcode", "").strip() or request.form.get("postcode", "").strip(),
                request.form.get("site_notes", "").strip(),
                timestamp,
                timestamp,
            ),
        )
        site_id = last_insert_id()
    if terminal_name:
        execute(
            """
            insert into terminals (
                business_public_id, site_id, terminal_name, status, created_at, updated_at
            ) values (?, ?, ?, 'awaiting_pairing', ?, ?)
            """,
            (public_id, site_id, terminal_name, timestamp, timestamp),
        )
    return redirect(url_for("business_detail", public_id=public_id, notice=f"Salon onboarded. Pairing code: {pairing_code}"))


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


def terminal_rows_with_business(where: str = "", params: tuple = ()):
    sql = f"""
        select
            terminals.*,
            businesses.name as business_name,
            businesses.status as business_status,
            businesses.subscription_status as subscription_status,
            sites.name as site_name,
            sites.status as site_status
        from terminals
        join businesses on businesses.public_id = terminals.business_public_id
        left join sites on sites.id = terminals.site_id
        {where}
        order by terminals.retired_at is not null, businesses.name, terminals.terminal_name
    """
    rows = []
    for row in query_all(sql, params):
        item = dict(row)
        item["health"] = terminal_health(row)
        rows.append(item)
    return rows


def platform_counts():
    return {
        "businesses": query_one("select count(*) as value from businesses where archived_at is null")["value"],
        "archived_businesses": query_one("select count(*) as value from businesses where archived_at is not null")["value"],
        "terminals": query_one("select count(*) as value from terminals")["value"],
        "active_terminals": query_one("select count(*) as value from terminals where last_seen_at is not null")["value"],
        "suspended_businesses": query_one(
            "select count(*) as value from businesses where status = 'suspended' or subscription_status in ('suspended', 'cancelled')"
        )["value"],
    }


def active_business_rows():
    return query_all(
        """
        select
            businesses.*,
            (select count(*) from sites where sites.business_public_id = businesses.public_id and sites.archived_at is null) as site_count,
            (select count(*) from terminals where terminals.business_public_id = businesses.public_id and terminals.retired_at is null) as terminal_count,
            (select count(*) from terminals where terminals.business_public_id = businesses.public_id and terminals.status = 'paired' and terminals.retired_at is null) as paired_terminal_count,
            (select max(last_seen_at) from terminals where terminals.business_public_id = businesses.public_id and terminals.retired_at is null) as last_check_in
        from businesses
        where archived_at is null
        order by name
        """
    )


@app.route("/platform/stats")
def platform_stats():
    terminals = terminal_rows_with_business()
    health_counts = {"active": 0, "stale": 0, "neutral": 0}
    locked_count = 0
    for terminal in terminals:
        health_counts[terminal["health"]["status"]] = health_counts.get(terminal["health"]["status"], 0) + 1
        if (
            terminal["business_status"] == "suspended"
            or terminal["subscription_status"] in {"suspended", "cancelled"}
            or terminal["last_lease_status"] in {"suspended", "cancelled", "archived"}
        ):
            locked_count += 1
    recent_checkins = [
        terminal for terminal in sorted(
            terminals,
            key=lambda item: item["last_seen_at"] or "",
            reverse=True,
        )
        if terminal["last_seen_at"]
    ][:20]
    return render_template(
        "stats.html",
        title="Salon Max Stats",
        counts=platform_counts(),
        businesses=active_business_rows(),
        terminals=terminals,
        health_counts=health_counts,
        locked_count=locked_count,
        recent_checkins=recent_checkins,
        notice=request.args.get("notice", "").strip(),
    )


@app.route("/platform/analytics")
def platform_analytics():
    terminals = terminal_rows_with_business()
    version_counts: dict[str, int] = {}
    health_counts = {"active": 0, "stale": 0, "neutral": 0}
    business_terminal_counts = []
    for terminal in terminals:
        version = terminal["app_version"] or "Unknown"
        version_counts[version] = version_counts.get(version, 0) + 1
        health_counts[terminal["health"]["status"]] = health_counts.get(terminal["health"]["status"], 0) + 1
    for business in active_business_rows():
        business_terminal_counts.append(
            {
                "name": business["name"],
                "public_id": business["public_id"],
                "site_count": business["site_count"],
                "terminal_count": business["terminal_count"],
                "last_check_in": business["last_check_in"],
            }
        )
    return render_template(
        "analytics.html",
        title="Salon Max Analytics",
        counts=platform_counts(),
        version_counts=sorted(version_counts.items(), key=lambda item: item[0]),
        health_counts=health_counts,
        business_terminal_counts=business_terminal_counts,
        notice=request.args.get("notice", "").strip(),
    )


@app.route("/platform/licences")
def platform_licences():
    business_filter = request.args.get("business", "").strip()
    where = ""
    params: tuple = ()
    if business_filter:
        where = "where businesses.public_id = ?"
        params = (business_filter,)
    return render_template(
        "licences.html",
        title="Salon Max Licences",
        counts=platform_counts(),
        businesses=query_all("select public_id, name from businesses where archived_at is null order by name"),
        terminals=terminal_rows_with_business(where, params),
        selected_business=business_filter,
        notice=request.args.get("notice", "").strip(),
    )


@app.route("/platform/diagnostics")
def platform_diagnostics():
    terminals = terminal_rows_with_business()
    stale = [terminal for terminal in terminals if terminal["health"]["status"] == "stale"]
    locked = [
        terminal for terminal in terminals
        if terminal["business_status"] == "suspended"
        or terminal["subscription_status"] in {"suspended", "cancelled"}
        or terminal["last_lease_status"] in {"suspended", "cancelled", "archived"}
    ]
    return render_template(
        "diagnostics.html",
        title="Salon Max Diagnostics",
        counts=platform_counts(),
        terminals=terminals,
        stale=stale,
        locked=locked,
        notice=request.args.get("notice", "").strip(),
    )


@app.route("/platform/diagnostics/terminal/<int:terminal_id>")
def platform_terminal_diagnostics(terminal_id: int):
    rows = terminal_rows_with_business("where terminals.id = ?", (terminal_id,))
    if not rows:
        return redirect(url_for("platform_diagnostics", notice="Terminal not found."))
    terminal = rows[0]
    return render_template(
        "terminal_diagnostics.html",
        title="Terminal Diagnostics",
        terminal=terminal,
        notice=request.args.get("notice", "").strip(),
    )


@app.post("/platform/terminal/<int:terminal_id>/update")
def update_terminal(terminal_id: int):
    terminal = query_one("select * from terminals where id = ?", (terminal_id,))
    if terminal is None:
        return redirect(url_for("platform_diagnostics", notice="Terminal not found."))
    execute(
        """
        update terminals
        set terminal_name = ?, status = ?, target_app_version = ?, support_notes = ?, updated_at = ?
        where id = ?
        """,
        (
            request.form.get("terminal_name", "").strip() or terminal["terminal_name"],
            request.form.get("status", terminal["status"]).strip() or terminal["status"],
            request.form.get("target_app_version", "").strip(),
            request.form.get("support_notes", "").strip(),
            now_utc(),
            terminal_id,
        ),
    )
    return redirect(url_for("platform_terminal_diagnostics", terminal_id=terminal_id, notice="Terminal saved."))


@app.post("/platform/terminal/<int:terminal_id>/suspend")
def suspend_terminal(terminal_id: int):
    execute("update terminals set status = 'suspended', updated_at = ? where id = ?", (now_utc(), terminal_id))
    return redirect(url_for("platform_terminal_diagnostics", terminal_id=terminal_id, notice="Terminal suspended."))


@app.post("/platform/terminal/<int:terminal_id>/reactivate")
def reactivate_terminal(terminal_id: int):
    execute("update terminals set status = 'paired', retired_at = null, updated_at = ? where id = ?", (now_utc(), terminal_id))
    return redirect(url_for("platform_terminal_diagnostics", terminal_id=terminal_id, notice="Terminal reactivated."))


@app.post("/platform/terminal/<int:terminal_id>/retire")
def retire_terminal(terminal_id: int):
    execute("update terminals set status = 'retired', retired_at = ?, updated_at = ? where id = ?", (now_utc(), now_utc(), terminal_id))
    return redirect(url_for("platform_diagnostics", notice="Terminal retired. History has been kept."))


@app.route("/platform/updates")
def platform_updates():
    return render_template(
        "updates.html",
        title="Salon Max Updates",
        counts=platform_counts(),
        terminals=terminal_rows_with_business(),
        notice=request.args.get("notice", "").strip(),
    )


@app.post("/platform/updates/terminal/<int:terminal_id>/target")
def set_terminal_target_version(terminal_id: int):
    target = request.form.get("target_app_version", "").strip()
    execute("update terminals set target_app_version = ?, updated_at = ? where id = ?", (target, now_utc(), terminal_id))
    return redirect(url_for("platform_updates", notice="Target app version saved."))


@app.route("/platform/queries")
def platform_queries():
    search = request.args.get("search", "").strip()
    business_rows = []
    terminal_rows = []
    if search:
        like = f"%{search}%"
        business_rows = query_all(
            """
            select * from businesses
            where name like ? or public_id like ? or contact_name like ? or contact_email like ? or contact_phone like ?
            order by updated_at desc
            """,
            (like, like, like, like, like),
        )
        terminal_rows = terminal_rows_with_business(
            """
            where terminals.terminal_name like ?
               or terminals.device_public_id like ?
               or businesses.name like ?
               or businesses.public_id like ?
            """,
            (like, like, like, like),
        )
    return render_template(
        "queries.html",
        title="Salon Max Queries",
        search=search,
        businesses=business_rows,
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
