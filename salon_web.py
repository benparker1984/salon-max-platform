"""Hosted salon and customer access."""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, jsonify, make_response, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import provider as p

bp = Blueprint("salon", __name__)
TEAM = {"owner", "staff"}
ROLES = TEAM | {"customer"}
DUMMY_HASH = generate_password_hash(secrets.token_hex(16))


def init_schema():
    db = p.get_platform_db()
    db.executescript("""
        create table if not exists web_accounts (
            account_id text primary key,
            tenant text not null,
            login text not null,
            role text not null check (role in ('owner','staff','customer')),
            customer_id text,
            password_hash text,
            invite_hash text,
            invite_until text,
            disabled integer not null default 0,
            unique(tenant, login, role),
            unique(tenant, customer_id),
            check ((role = 'customer' and customer_id is not null)
                or (role <> 'customer' and customer_id is null))
        );
        create table if not exists web_login_attempts (
            attempt_key text primary key,
            expires text not null,
            attempts integer not null
        );
    """)
    db.commit()


def tenant_record(tenant):
    row = p.platform_query_one(
        "select * from cloud_business_accounts where business_account_public_id=? and product_type='salon'", (tenant,)
    )
    if not row or not p.business_access_state(row)[0]:
        abort(404)
    return row


def member(tenant, roles):
    identity = session.get("salon_account")
    account = p.platform_query_one("select * from web_accounts where tenant=? and account_id=? and disabled=0",
                                   (tenant, identity)) if identity else None
    if not account or account["role"] not in roles:
        abort(403)
    if account["password_hash"] and session.get("salon_proof") != digest(account["password_hash"]):
        session.clear()
        abort(403)
    return account


def page(**context):
    token = session.setdefault("salon_csrf", secrets.token_urlsafe(32))
    response = make_response(render_template("salon_web.html", csrf=token, **context))
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def verify_csrf():
    token = session.get("salon_csrf")
    if not token or not hmac.compare_digest(token, request.form.get("csrf", "")):
        abort(400)


def stamp(offset=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat(timespec="seconds")


def throttle(tenant, login):
    key = hashlib.sha256(f"{tenant}:{login}".encode()).hexdigest()
    now = stamp()
    db = p.get_platform_db()
    db.execute("delete from web_login_attempts where expires < ?", (now,))
    row = db.execute("""
        insert into web_login_attempts values (?, ?, 1)
        on conflict(attempt_key) do update set attempts=web_login_attempts.attempts+1
        where web_login_attempts.attempts < 12 returning attempts
    """, (key, stamp(900))).fetchone()
    db.commit()
    if not row:
        abort(429)


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


@bp.route("/platform/business/<tenant>/web-access", methods=["GET", "POST"])
def provision_owner(tenant):
    if not (session.get("platform_admin_authenticated") or session.get("salonmax_platform_authed")):
        abort(403)
    salon = tenant_record(tenant)
    return manage(salon, {"owner"}, "salon.provision_owner")


@bp.route("/salon/<tenant>/access", methods=["GET", "POST"])
def provision_team(tenant):
    salon = tenant_record(tenant)
    account = member(tenant, TEAM)
    roles = {"customer", "staff"} if account["role"] == "owner" else {"customer"}
    return manage(salon, roles, "salon.provision_team")


def manage(salon, roles, endpoint):
    tenant = salon["business_account_public_id"]
    invite = None
    if request.method == "POST":
        verify_csrf()
        action = request.form.get("action")
        if action in {"disable", "reset"}:
            existing = p.platform_query_one("select role,disabled from web_accounts where tenant=? and account_id=?",
                                            (tenant, request.form.get("account_id")))
            if not existing or existing["role"] not in roles or existing["disabled"]:
                abort(404)
            if action == "disable":
                p.platform_execute("update web_accounts set disabled=1, invite_hash=null where tenant=? and account_id=?",
                                   (tenant, request.form.get("account_id")))
                return redirect(url_for(endpoint, tenant=tenant), code=303)
            token = secrets.token_urlsafe(32)
            p.platform_execute("""
                update web_accounts set invite_hash=?,invite_until=? where tenant=? and account_id=? and disabled=0
            """, (digest(token), stamp(1800), tenant, request.form.get("account_id")))
            invite = url_for("salon.activate", tenant=tenant, token=token)
    if request.method == "POST" and invite is None:
        role = request.form.get("role")
        login = request.form.get("login", "").strip().lower()
        customer_id = None
        if role not in roles:
            abort(403)
        if role == "customer":
            customer = p.platform_query_one(
                "select customer_public_id,email from cloud_customers where business_account_public_id=? and customer_public_id=? and status='active'",
                (tenant, request.form.get("customer_id", "").strip()),
            )
            if not customer:
                abort(404)
            customer_id, login = customer["customer_public_id"], customer["email"].strip().lower()
        if not login or len(login) > 254 or any(char.isspace() for char in login):
            abort(400, "Enter a valid login.")
        if role in TEAM:
            from backoffice import valid_email
            if not valid_email(login):
                abort(400, "Enter an email address.")
        token = secrets.token_urlsafe(32)
        cursor = p.get_platform_db().execute("""
            insert into web_accounts (account_id,tenant,login,role,customer_id,invite_hash,invite_until)
            values (?,?,?,?,?,?,?)
            on conflict(tenant,login,role) do update set invite_hash=excluded.invite_hash,
                invite_until=excluded.invite_until
            where web_accounts.password_hash is null and web_accounts.disabled=0
              and (web_accounts.customer_id is null or web_accounts.customer_id=excluded.customer_id)
        """, (secrets.token_hex(20), tenant, login, role, customer_id, digest(token), stamp(86400)))
        p.get_platform_db().commit()
        if cursor.rowcount != 1:
            abort(409, "An account already exists.")
        invite = url_for("salon.activate", tenant=tenant, token=token)
    accounts = p.platform_query_all(
        """select account_id, login, role, disabled,
                  case when password_hash is null then 'Awaiting setup' else 'Active' end as setup_status
           from web_accounts where tenant=? order by login""", (tenant,)
    )
    customers = p.platform_query_all("""
        select customer_public_id,first_name,last_name,email from cloud_customers
        where business_account_public_id=? and status='active' and email<>'' order by last_name, first_name
    """, (tenant,)) if "customer" in roles else []
    return page(salon=salon, mode="manage", roles=sorted(roles), customers=customers, invite=invite,
                accounts=[account for account in accounts if account["role"] in roles])


@bp.route("/salon/<tenant>/activate/<token>", methods=["GET", "POST"])
def activate(tenant, token):
    salon = tenant_record(tenant)
    if len(token) != 43:
        abort(404)
    hashed = digest(token)
    row = p.platform_query_one("""
        select account_id,role,login,password_hash from web_accounts
        where tenant=? and invite_hash=? and invite_until>? and disabled=0
    """, (tenant, hashed, stamp()))
    if not row:
        abort(404, "This setup link is invalid or expired. Ask for a new link.")
    team_office = None
    if row["role"] in TEAM:
        from backoffice import office_url, recovery_code, recovery_page, team_activation_page
        team_office = office_url(tenant)
        if team_office and request.method == "GET":
            return team_activation_page(salon, token, bool(row["password_hash"]))
    if request.method == "POST":
        verify_csrf()
        if team_office and request.form.get("email", "").strip().lower() != row["login"]:
            abort(400, "Enter the email address the salon owner used for this setup link.")
        password = request.form.get("password", "")
        if not 12 <= len(password) <= 128:
            abort(400, "Use 12 to 128 characters.")
        db = p.get_platform_db()
        cursor = db.execute("""
            update web_accounts set password_hash=?, invite_hash=null, invite_until=null
            where tenant=? and account_id=? and invite_hash=? and invite_until>? and disabled=0
        """, (generate_password_hash(password), tenant, row["account_id"], hashed, stamp()))
        if cursor.rowcount != 1:
            abort(404)
        code = recovery_code(db, row["account_id"]) if team_office else None
        db.commit()
        if session.get("salon_account") == row["account_id"]:
            session.clear()
        if code:
            return recovery_page(salon, code)
        return redirect(url_for("salon.customer_login" if row["role"] == "customer" else "salon.login", tenant=tenant), code=303)
    return page(salon=salon, mode="activate")


@bp.route("/salon/<tenant>/login", methods=["GET", "POST"])
def login(tenant):
    return sign_in(tenant)


@bp.route("/salon/<tenant>/customer", methods=["GET", "POST"])
def customer_login(tenant):
    return sign_in(tenant, customer_only=True)


def sign_in(tenant, customer_only=False):
    salon = tenant_record(tenant)
    error = None
    if request.method == "POST":
        verify_csrf()
        login_name = request.form.get("login", "").strip().lower()
        password = request.form.get("password", "")
        role = "customer" if customer_only else request.form.get("role")
        if role not in ROLES or len(login_name) > 254 or len(password) > 128:
            abort(400)
        if authenticate(tenant, login_name, role, password):
            return redirect(url_for("salon.dashboard", tenant=tenant), code=303)
        error = "The login details did not match."
    return page(salon=salon, mode="login", error=error, customer_only=customer_only)


def authenticate(tenant, login_name, role, password):
    throttle(tenant, f"{role}:{login_name}")
    row = p.platform_query_one("""
        select account_id,password_hash from web_accounts
        where tenant=? and login=? and role=? and disabled=0 and password_hash is not null
    """, (tenant, login_name, role))
    if not check_password_hash(row["password_hash"] if row else DUMMY_HASH, password) or not row:
        return False
    session.clear()
    session["salon_account"] = row["account_id"]
    session["salon_proof"] = digest(row["password_hash"])
    return True


@bp.get("/salon/<tenant>/")
def dashboard(tenant):
    salon = tenant_record(tenant)
    if not session.get("salon_account"):
        return redirect(url_for("salon.customer_login", tenant=tenant))
    account = member(tenant, ROLES)
    minutes = None
    if account["role"] == "customer":
        row = p.platform_query_one("""
            select m.minutes_available from cloud_customers c left join cloud_customer_minute_summary m
            on m.business_account_public_id=c.business_account_public_id and m.customer_public_id=c.customer_public_id
            where c.business_account_public_id=? and c.customer_public_id=? and c.status='active'
        """, (tenant, account["customer_id"]))
        if not row:
            abort(403)
        minutes = row["minutes_available"] or 0
    from checkout import configured, offers
    return page(salon=salon, mode="dashboard", account=account, minutes=minutes,
                offers=offers(tenant) if minutes is not None else [],
                checkout_enabled=minutes is not None and configured(tenant))


@bp.post("/salon/<tenant>/logout")
def logout(tenant):
    verify_csrf()
    session.clear()
    return redirect(url_for("salon.login", tenant=tenant), code=303)


@bp.get("/salon/")
def choose_salon():
    tenant = request.args.get("code", "").strip()
    if tenant:
        tenant_record(tenant)
        return redirect(url_for("salon.dashboard", tenant=tenant))
    return page(salon={"business_name": "Customer portal"}, mode="choose")


@bp.get("/salon/service-worker.js")
def service_worker():
    return send_from_directory(p.app.static_folder, "salon-worker.js", max_age=0)


@bp.get("/salon/<tenant>/manifest.webmanifest")
def manifest(tenant):
    salon = tenant_record(tenant)
    scope = url_for("salon.dashboard", tenant=tenant)
    response = jsonify({
        "id": scope,
        "name": salon["business_name"] + " Customer Portal",
        "short_name": salon["business_name"],
        "start_url": scope,
        "scope": scope,
        "display": "standalone",
        "background_color": "#0c1523",
        "theme_color": "#0c1523",
        "icons": [
            {"src": url_for("static", filename=f"salon-icon-{size}.png"),
             "sizes": f"{size}x{size}", "type": "image/png"}
            for size in (192, 512)
        ],
    })
    response.mimetype = "application/manifest+json"
    response.headers["Cache-Control"] = "no-cache"
    return response
