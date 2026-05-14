from __future__ import annotations

import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("SALONMAX_PLATFORM_DB_PATH") or BASE_DIR / "salonmax_platform.db")

app = Flask(__name__)
app.secret_key = os.environ.get("SALONMAX_SECRET_KEY", "change-this-before-live")


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    if request.path in {"/healthz", "/platform-login"} or request.path.startswith("/static/"):
        return None
    if not session.get("salonmax_platform_authed"):
        return redirect(url_for("platform_login", next=request.full_path.rstrip("?")))
    return None


@app.get("/healthz")
def healthz():
    return {"ok": True, "product": "salon-max-platform"}


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
    return render_template(
        "business.html",
        business=business,
        sites=sites,
        terminals=terminals,
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
