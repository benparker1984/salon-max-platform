"""Hosted salon and customer access."""
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.error import URLError
from urllib.request import Request, urlopen

from flask import Blueprint, abort, flash, g, jsonify, make_response, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import provider as p
from customer_profiles import save_profile, ProfileError

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
        create table if not exists web_customer_profiles (
            tenant text not null,
            account_id text not null,
            full_name text not null,
            address_line text not null,
            town text not null,
            postcode text not null,
            phone text not null,
            old_account_number text not null default '',
            requested_at text,
            linked_at text,
            primary key (tenant,account_id)
        );
        create table if not exists web_customer_pairings (
            tenant text not null,
            account_number text not null,
            account_id text not null,
            primary key (tenant,account_number)
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


def clear_customer_session():
    session.pop("salon_account", None)
    session.pop("salon_proof", None)


def clear_team_session(tenant):
    teams = dict(session.get("salon_team_sessions", {}))
    if teams.pop(tenant, None) is not None:
        session["salon_team_sessions"] = teams
    # Older sign-ins used a single slot shared with the customer portal.
    legacy = session.get("salon_account")
    if legacy and p.platform_query_one("""
        select 1 from web_accounts where tenant=? and account_id=? and role in ('owner','staff')
    """, (tenant, legacy)):
        clear_customer_session()


def member(tenant, roles):
    team = session.get("salon_team_sessions", {}).get(tenant) if roles & TEAM else None
    legacy = (session.get("salon_account"), session.get("salon_proof"))
    credentials = [legacy, team] if "customer" in roles else [team, legacy]
    for credential in credentials:
        if not credential or not credential[0]:
            continue
        identity, proof = credential
        account = p.platform_query_one(
            "select * from web_accounts where tenant=? and account_id=? and disabled=0",
            (tenant, identity),
        )
        if not account or account["role"] not in roles:
            continue
        if account["password_hash"] and proof != digest(account["password_hash"]):
            if credential is team:
                clear_team_session(tenant)
            else:
                clear_customer_session()
            abort(403)
        return account
    abort(403)


def page(**context):
    team_account = context.pop("team_account", None)
    if team_account:
        from backoffice import SECTIONS, office_url
        tenant = context["salon"]["business_account_public_id"]
        address = office_url(tenant)
        if address:
            context.update(slug=address.lstrip("/"), sections=SECTIONS, account=team_account,
                           section={"manage": "staff", "offers": "packages"}.get(context["mode"]))
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


@bp.route("/platform/business/<tenant>/customer-access", methods=["GET", "POST"])
def provision_customers(tenant):
    if not (session.get("platform_admin_authenticated") or session.get("salonmax_platform_authed")):
        abort(403)
    return manage(tenant_record(tenant), {"customer"}, "salon.provision_customers")


@bp.route("/salon/<tenant>/access", methods=["GET", "POST"])
def provision_team(tenant):
    salon = tenant_record(tenant)
    account = member(tenant, TEAM)
    roles = {"customer", "staff"} if account["role"] == "owner" else {"customer"}
    return manage(salon, roles, "salon.provision_team", team_account=account)


def manage(salon, roles, endpoint, team_account=None):
    from backoffice import valid_email

    tenant = salon["business_account_public_id"]
    invite = None
    if request.method == "POST":
        verify_csrf()
        action = request.form.get("action")
        if action == "link-customer":
            if "customer" not in roles:
                abort(403)
            link_customer_record(tenant)
            return redirect(url_for(endpoint, tenant=tenant), code=303)
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
            customer_id = customer["customer_public_id"]
            login = customer["email"].strip().lower() or login
        if not login or len(login) > 254 or any(char.isspace() for char in login):
            abort(400, "Enter a valid login.")
        if not valid_email(login):
            abort(400, "Enter a valid email address for this account.")
        token = secrets.token_urlsafe(32)
        db = p.get_platform_db()
        if role == "customer":
            db.execute("""
                insert into web_accounts (account_id,tenant,login,role,customer_id)
                values (?,?,?,'customer',?) on conflict do nothing
            """, (secrets.token_hex(20), tenant, login, customer_id))
            cursor = db.execute("""
                update web_accounts set invite_hash=?,invite_until=?
                where tenant=? and customer_id=? and login=? and role='customer'
                  and password_hash is null and disabled=0
            """, (digest(token), stamp(86400), tenant, customer_id, login))
        else:
            cursor = db.execute("""
                insert into web_accounts (account_id,tenant,login,role,customer_id,invite_hash,invite_until)
                values (?,?,?,?,?,?,?)
                on conflict(tenant,login,role) do update set invite_hash=excluded.invite_hash,
                    invite_until=excluded.invite_until
                where web_accounts.password_hash is null and web_accounts.disabled=0
                  and (web_accounts.customer_id is null or web_accounts.customer_id=excluded.customer_id)
            """, (secrets.token_hex(20), tenant, login, role, customer_id, digest(token), stamp(86400)))
        db.commit()
        if cursor.rowcount != 1:
            abort(409, "An account already exists.")
        invite = url_for("salon.activate", tenant=tenant, token=token)
    accounts = p.platform_query_all(
        """select account_id, login, role, disabled,
                  case when password_hash is null then 'Awaiting setup' else 'Active' end as setup_status
           from web_accounts where tenant=? order by login""", (tenant,)
    )
    customers = p.platform_query_all("""
        select customer_public_id,first_name,last_name,email,account_number,customer_number from cloud_customers
        where business_account_public_id=? and status='active' order by last_name, first_name
    """, (tenant,)) if "customer" in roles else []
    link_requests = p.platform_query_all("""
        select profile.*, account.login from web_customer_profiles profile
        join web_accounts account on account.tenant=profile.tenant and account.account_id=profile.account_id
        where profile.tenant=? and profile.requested_at is not null and profile.linked_at is null
          and account.role='customer' and account.disabled=0
        order by profile.requested_at
    """, (tenant,)) if "customer" in roles else []
    return page(salon=salon, mode="manage", link_requests=link_requests, roles=sorted(roles), customers=customers, invite=invite,
                team_account=team_account,
                accounts=[account for account in accounts if account["role"] in roles])


def link_customer_record(tenant):
    if request.form.get("verified") != "yes":
        abort(400, "Verify the customer's identity and email before linking.")
    account_id = request.form.get("account_id", "")
    customer_id = request.form.get("customer_id", "")
    db = p.get_platform_db()
    linked = db.execute("""
        update web_customer_profiles set linked_at=?
        where tenant=? and account_id=? and requested_at is not null and linked_at is null
    """, (stamp(), tenant, account_id))
    if linked.rowcount != 1:
        db.rollback()
        flash("This link request is no longer pending.", "customer-link")
        return
    profile = db.execute("""
        select old_account_number from web_customer_profiles where tenant=? and account_id=?
    """, (tenant, account_id)).fetchone()
    number = profile["old_account_number"].strip().lower()
    if number:
        matching = db.execute("""
            select customer_public_id from cloud_customers
            where business_account_public_id=? and status='active'
              and lower(coalesce(nullif(trim(account_number),''),trim(customer_number)))=?
            limit 2
        """, (tenant, number)).fetchall()
        if len(matching) != 1 or matching[0]["customer_public_id"] != customer_id:
            db.rollback()
            flash("Choose the unique salon record that matches the requested account number.", "customer-link")
            return
        pairing = db.execute("""
            insert into web_customer_pairings (tenant,account_number,account_id)
            values (?,?,?) on conflict do nothing
        """, (tenant, number, account_id))
        if pairing.rowcount != 1:
            db.rollback()
            flash("This account number has already been paired.", "customer-link")
            return
    updated = db.execute("""
        update web_accounts set customer_id=?, invite_hash=null, invite_until=null
        where tenant=? and account_id=? and role='customer' and disabled=0 and password_hash is not null
          and exists (
            select 1 from cloud_customers where business_account_public_id=?
            and customer_public_id=? and status='active'
          )
        and not exists (
            select 1 from web_accounts other where other.tenant=? and other.customer_id=?
        )
    """, (customer_id, tenant, account_id, tenant, customer_id, tenant, customer_id))
    if updated.rowcount != 1:
        db.rollback()
        flash("Choose an active salon account that is not already linked.", "customer-link")
        return
    registration = db.execute("""
        select p.address_line,p.town,p.postcode,p.phone,a.login
        from web_customer_profiles p join web_accounts a on a.tenant=p.tenant and a.account_id=p.account_id
        where p.tenant=? and p.account_id=?
    """, (tenant, account_id)).fetchone()
    current = db.execute("""
        select postal_address,phone,email from cloud_customers
        where business_account_public_id=? and customer_public_id=?
    """, (tenant, customer_id)).fetchone()
    candidates = {"postal_address": ", ".join(registration[k] for k in ("address_line", "town", "postcode")
                                               if registration[k]),
                  "phone": registration["phone"], "email": registration["login"]}
    changes = {key: value for key, value in candidates.items() if value and not current[key]}
    if changes:
        save_profile(db, tenant, customer_id, changes)
    db.commit()
    flash("Customer account linked. Their existing minutes are now visible online.", "customer-link")


@bp.route("/salon/<tenant>/customer/join", methods=["GET", "POST"])
def customer_join(tenant):
    from backoffice import valid_email

    salon = tenant_record(tenant)
    details = {}
    error = None
    if request.method == "POST":
        verify_csrf()
        details = {field: request.form.get(field, "").strip()
                   for field in ("full_name", "address_line", "town", "postcode", "phone", "email")}
        email = details["email"].lower()
        password = request.form.get("password", "")
        if (any(not value or len(value) > 150 for field, value in details.items() if field != "email")
                or not valid_email(email) or not 12 <= len(password) <= 128
                or not 7 <= len(re.sub(r"\D", "", details["phone"])) <= 15):
            error = "Check your contact details and choose a password of 12 to 128 characters."
        else:
            throttle(tenant, f"customer-join:{email}")
            account_id = secrets.token_hex(20)
            password_hash = generate_password_hash(password)
            db = p.get_platform_db()
            account = db.execute("""
                insert into web_accounts (account_id,tenant,login,role,customer_id,password_hash)
                values (?,?,?,'customer',?,?) on conflict do nothing
            """, (account_id, tenant, email, "pending:" + account_id, password_hash))
            if account.rowcount == 1:
                db.execute("""
                    insert into web_customer_profiles
                    (tenant,account_id,full_name,address_line,town,postcode,phone)
                    values (?,?,?,?,?,?,?)
                """, (tenant, account_id, details["full_name"], details["address_line"],
                      details["town"], details["postcode"], details["phone"]))
                db.commit()
                clear_customer_session()
                session["salon_account"] = account_id
                session["salon_proof"] = digest(password_hash)
                return redirect(url_for("salon.dashboard", tenant=tenant), code=303)
            db.rollback()
            error = "If you already have online access, sign in or reset your password."
    return page(salon=salon, mode="customer-join", details=details, error=error)


@bp.post("/salon/<tenant>/customer/link")
def request_customer_link(tenant):
    tenant_record(tenant)
    account = member(tenant, {"customer"})
    verify_csrf()
    old_number = request.form.get("account_number", "").strip()
    if not old_number or len(old_number) > 80:
        abort(400, "Enter the account number your salon gave you.")
    p.platform_execute("""
        update web_customer_profiles set old_account_number=?, requested_at=?
        where tenant=? and account_id=? and requested_at is null and linked_at is null
    """, (old_number, stamp(), tenant, account["account_id"]))
    return redirect(url_for("salon.dashboard", tenant=tenant), code=303)


@bp.route("/salon/<tenant>/activate/<token>", methods=["GET", "POST"])
def activate(tenant, token):
    salon = tenant_record(tenant)
    if len(token) != 43:
        abort(404)
    hashed = digest(token)
    row = p.platform_query_one("""
        select account_id,role,login,password_hash,customer_id from web_accounts
        where tenant=? and invite_hash=? and invite_until>? and disabled=0
    """, (tenant, hashed, stamp()))
    if not row:
        abort(404, "This setup link is invalid or expired. Ask for a new link.")
    if row["role"] == "customer" and not p.platform_query_one("""
        select 1 from cloud_customers where business_account_public_id=? and customer_public_id=?
        and status='active' and (trim(email)='' or lower(trim(email))=?)
        union all
        select 1 from web_customer_profiles where tenant=? and account_id=? and linked_at is null
    """, (tenant, row["customer_id"], row["login"], tenant, row["account_id"])):
        abort(404, "Ask your salon for a new setup link.")
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
            clear_customer_session()
        if code:
            return recovery_page(salon, code)
        return redirect(url_for("salon.customer_login" if row["role"] == "customer" else "salon.login", tenant=tenant), code=303)
    return page(salon=salon, mode="activate", customer_only=row["role"] == "customer")


@bp.get("/v1/customers/online-status")
def customer_online_status():
    tenant = g.device_terminal["business_account_public_id"]
    rows = p.platform_query_all("""
        select c.customer_number,c.email,
            case when c.status='archived' then 'Archived'
                 when a.disabled=1 then 'Disabled'
                 when a.password_hash is not null then 'Active'
                 when a.invite_until>? then 'Awaiting setup'
                 else 'No online account' end as status
        from cloud_customers c left join web_accounts a
          on a.tenant=c.business_account_public_id and a.customer_id=c.customer_public_id and a.role='customer'
        where c.business_account_public_id=? and c.status in ('active','archived')
    """, (stamp(), tenant))
    statuses = {}
    for row in rows:
        number = row["customer_number"]
        statuses[number] = ({"status": "Check customer number", "can_invite": False} if number in statuses else
                            {"status": row["status"], "can_invite": bool(row["email"] and row["status"] in
                                                                         {"No online account", "Awaiting setup"})})
    response = jsonify(tenant=tenant, statuses=statuses)
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/salon/<tenant>/customer-invite/<number>", methods=["GET", "POST"])
def invite_customer_email(tenant, number):
    from backoffice import ORIGIN, valid_email
    from urllib.parse import urlencode, quote

    salon = tenant_record(tenant)
    if not session.get("salon_team_sessions", {}).get(tenant):
        session["customer_invite_return"] = [tenant, number]
        return redirect(url_for("salon.login", tenant=tenant))
    member(tenant, TEAM)
    matches = p.platform_query_all("""
        select customer_public_id,first_name,last_name,email from cloud_customers
        where business_account_public_id=? and customer_number=? and status='active' limit 2
    """, (tenant, number))
    if len(matches) != 1:
        abort(404, "The customer number must match one synced salon record.")
    customer = matches[0]
    email = customer["email"].strip().lower()
    link = mailto = message = None
    if request.method == "POST":
        verify_csrf()
        if not valid_email(email):
            abort(400, "Save a valid customer email before inviting them.")
        throttle(tenant, f"staff-customer-invite:{number}")
        token = secrets.token_urlsafe(32)
        db = p.get_platform_db()
        db.execute("""
            insert into web_accounts (account_id,tenant,login,role,customer_id)
            values (?,?,?,'customer',?) on conflict do nothing
        """, (secrets.token_hex(20), tenant, email, customer["customer_public_id"]))
        changed = db.execute("""
            update web_accounts set invite_hash=?,invite_until=?
            where tenant=? and customer_id=? and login=? and role='customer'
              and password_hash is null and disabled=0
        """, (digest(token), stamp(86400), tenant, customer["customer_public_id"], email))
        if changed.rowcount != 1:
            db.rollback()
            abort(409, "This customer or email already has an online account. Check their web access before inviting again.")
        db.commit()
        link = ORIGIN + url_for("salon.activate", tenant=tenant, token=token)
        subject = f"{salon['business_name']} customer account"
        body = f"Set up access to your salon account: {link}\n\nThis link expires in 24 hours."
        if customer_email_enabled():
            try:
                send_customer_email(email, subject, body)
                message = "Invitation submitted for email delivery."
                link = None
            except (URLError, TimeoutError):
                message = "Automatic delivery failed. Send the invitation using your email app."
        if link:
            mailto = "mailto:" + quote(email, safe="@") + "?" + urlencode(
                {"subject": subject, "body": body}, quote_via=quote)
    return page(salon=salon, mode="invite-customer-email", customer=customer, invite_email=email,
                invite_link=link, mailto=mailto, invite_message=message, email_enabled=customer_email_enabled())


def send_customer_email(email, subject, body):
    message = {"from": os.environ["SALONMAX_TRANSACTIONAL_EMAIL_FROM"],
               "to": [email], "subject": subject, "text": body}
    email_request = Request(
        "https://api.resend.com/emails", json.dumps(message).encode(),
        {"Authorization": "Bearer " + os.environ["SALONMAX_TRANSACTIONAL_EMAIL_KEY"],
         "Content-Type": "application/json"},
    )
    with urlopen(email_request, timeout=8):
        pass


def customer_email_enabled():
    return bool(os.environ.get("SALONMAX_TRANSACTIONAL_EMAIL_KEY")
                and os.environ.get("SALONMAX_TRANSACTIONAL_EMAIL_FROM"))


@bp.route("/salon/<tenant>/customer/<any(register,reset):action>", methods=["GET", "POST"])
def customer_access(tenant, action):
    from backoffice import ORIGIN, valid_email

    salon = tenant_record(tenant)
    enabled = customer_email_enabled()
    submitted = False
    if request.method == "POST":
        verify_csrf()
        setup_code = request.form.get("setup_code", "").strip()
        if action == "register" and setup_code:
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", setup_code):
                abort(400, "Enter the setup code your salon gave you.")
            if not p.platform_query_one("""
                select 1 from web_accounts where tenant=? and role='customer' and disabled=0
                and password_hash is null and invite_hash=? and invite_until>?
            """, (tenant, digest(setup_code), stamp())):
                abort(404, "This setup code is invalid or expired. Ask your salon for a new code.")
            return redirect(url_for("salon.activate", tenant=tenant, token=setup_code), code=303)
        email = request.form.get("email", "").strip().lower()
        if not valid_email(email):
            abort(400)
        if not enabled:
            abort(503, "Ask your salon for a private setup or reset link.")
        throttle(tenant, f"customer-access:{email}")
        customers = p.platform_query_all("""
            select customer_public_id from cloud_customers
            where business_account_public_id=? and status='active' and lower(trim(email))=?
            limit 2
        """, (tenant, email))
        if len(customers) == 1:
            customer_id = customers[0]["customer_public_id"]
            db = p.get_platform_db()
            if action == "register":
                db.execute("""
                    insert into web_accounts (account_id,tenant,login,role,customer_id)
                    values (?,?,?,'customer',?) on conflict do nothing
                """, (secrets.token_hex(20), tenant, email, customer_id))
            token = secrets.token_urlsafe(32)
            password_state = "is null" if action == "register" else "is not null"
            changed = db.execute(f"""
                update web_accounts set invite_hash=?, invite_until=?
                where tenant=? and login=? and role='customer' and customer_id=?
                  and disabled=0 and password_hash {password_state}
            """, (digest(token), stamp(1800), tenant, email, customer_id)).rowcount
            db.commit()
            if changed:
                link = ORIGIN + url_for("salon.activate", tenant=tenant, token=token)
                try:
                    send_customer_email(email, f"{salon['business_name']} customer account",
                                        f"Choose your account password: {link}\n\nThis link expires in 30 minutes.")
                except (URLError, TimeoutError):
                    p.app.logger.warning("Customer account email delivery failed")
        submitted = True
    return page(salon=salon, mode="customer-access", action=action, email_enabled=enabled, submitted=submitted)


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
            if role == "owner" and session.pop("busyness_settings_return", None) == tenant:
                return redirect(url_for("salon.busyness_settings", tenant=tenant), code=303)
            pending = session.pop("customer_invite_return", None) if role in TEAM else None
            if pending and pending[0] == tenant:
                return redirect(url_for("salon.invite_customer_email", tenant=tenant, number=pending[1]), code=303)
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
    proof = digest(row["password_hash"])
    if role in TEAM:
        teams = dict(session.get("salon_team_sessions", {}))
        teams[tenant] = [row["account_id"], proof]
        session["salon_team_sessions"] = teams
    else:
        session["salon_account"] = row["account_id"]
        session["salon_proof"] = proof
    return True


@bp.post("/salon/<tenant>/customer/details")
def edit_customer_details(tenant):
    tenant_record(tenant)
    account = member(tenant, {"customer"})
    verify_csrf()
    if not account["customer_id"]:
        abort(403)
    db = p.get_platform_db()
    try:
        save_profile(db, tenant, account["customer_id"], request.form,
                     expected=request.form.get("profile_version", ""))
        db.commit()
        flash("Customer details saved.", "profile")
    except ProfileError as error:
        db.rollback()
        flash(str(error), "profile")
    return redirect(url_for("salon.dashboard", tenant=tenant), code=303)


@bp.route("/salon/<tenant>/busyness-settings", methods=["GET", "POST"])
def busyness_settings(tenant):
    salon = tenant_record(tenant)
    if not session.get("salon_team_sessions", {}).get(tenant) and request.method == "GET":
        session["busyness_settings_return"] = tenant
        return redirect(url_for("salon.login", tenant=tenant))
    account = member(tenant, {"owner"})
    error = None
    if request.method == "POST":
        verify_csrf()
        amber = request.form.get("amber", type=int)
        red = request.form.get("red", type=int)
        if amber is not None and red is not None and 1 <= amber < red <= 120:
            p.platform_execute("""
                insert into salon_busyness_settings values (?,?,?,?)
                on conflict (tenant) do update set enabled=excluded.enabled,amber=excluded.amber,red=excluded.red
            """, (tenant, int(request.form.get("enabled") == "yes"), amber, red))
            flash("Busyness settings saved.", "busyness")
            return redirect(url_for("salon.busyness_settings", tenant=tenant), code=303)
        error = "Enter two whole minutes values. The amber threshold must be lower than red."
    settings = p.platform_query_one("select * from salon_busyness_settings where tenant=?", (tenant,))
    return page(salon=salon, mode="busyness-settings", team_account=account, error=error,
                settings=settings or {"enabled": False, "amber": 5, "red": 15})


def customer_busyness(tenant):
    settings = p.platform_query_one("select * from salon_busyness_settings where tenant=? and enabled=1", (tenant,))
    if not settings:
        return []
    fresh = stamp(-90)
    rows = p.platform_query_all("""
        select s.site_name, min(r.wait_seconds) as wait_seconds
        from cloud_business_sites s
        left join salon_busyness_reports r on r.tenant=s.business_account_public_id and r.site=s.site_public_id
            and r.reported_at>=? and exists (
                select 1 from cloud_terminal_registry t where t.business_account_public_id=r.tenant
                and t.site_public_id=r.site and t.terminal_device_public_id=r.terminal
                and t.management_status='active')
        where s.business_account_public_id=? and s.status='active'
        group by s.site_public_id,s.site_name order by s.site_name
    """, (fresh, tenant))
    result = []
    for row in rows:
        seconds = row["wait_seconds"]
        if seconds is None:
            colour, label = "gray", "Live status unavailable"
        elif seconds < settings["amber"] * 60:
            colour, label = "green", "Quiet"
        elif seconds < settings["red"] * 60:
            colour, label = "orange", "Getting busy"
        else:
            colour, label = "red", "Busy"
        result.append((row["site_name"], colour, label))
    return result


@bp.get("/salon/<tenant>/", defaults={"section": None})
@bp.get('/salon/<tenant>/customer/<any("top-up",history):section>')
def dashboard(tenant, section):
    salon = tenant_record(tenant)
    if not (session.get("salon_account") or session.get("salon_team_sessions", {}).get(tenant)):
        return redirect(url_for("salon.customer_login", tenant=tenant))
    account = member(tenant, {"customer"} if section else ROLES)
    if account["role"] in TEAM:
        from backoffice import office_url
        address = office_url(tenant)
        if address:
            return redirect(address)
    minutes = None
    history = []
    sessions = []
    details = None
    if account["role"] == "customer":
        profile = p.platform_query_one("""
            select full_name,address_line,town,postcode,phone,requested_at,linked_at
            from web_customer_profiles where tenant=? and account_id=?
        """, (tenant, account["account_id"]))
        if profile and not profile["linked_at"]:
            return page(salon=salon, mode="customer-unlinked", requested=bool(profile["requested_at"]),
                        details={**dict(profile), "email": account["login"]})
        row = p.platform_query_one("""
            select c.first_name,c.last_name,c.phone,c.account_number,c.customer_number,
                   c.birthday,c.postal_address,c.email,c.profile_synced_at,
                   m.minutes_available
            from cloud_customers c left join cloud_customer_minute_summary m
            on m.business_account_public_id=c.business_account_public_id and m.customer_public_id=c.customer_public_id
            where c.business_account_public_id=? and c.customer_public_id=? and c.status='active'
        """, (tenant, account["customer_id"]))
        if not row:
            abort(403)
        minutes = row["minutes_available"]
        details = {
            **dict(row),
            "full_name": " ".join(name for name in (row["first_name"], row["last_name"]) if name),
            "login_email": account["login"],
            "account_number": row["account_number"] or row["customer_number"],
        }
        customer_id = account["customer_id"]
        if section == "history":
            history = p.platform_query_all("""
                select created_at, entry_type, delta_minutes, minutes_after
                from cloud_customer_minute_ledger where business_account_public_id=? and customer_public_id=?
                order by created_at desc,id desc limit 50
            """, (tenant, customer_id))
            sessions = tanning_history(tenant, customer_id)
    from checkout import configured, offers
    course_orders = p.platform_query_all("""
        select o.minutes,o.amount,o.paid_at,w.title from web_orders o
        join web_offers w on w.tenant=o.tenant and w.offer_id=o.offer_id
        where o.tenant=? and o.customer_id=? and (o.stripe_account='' or o.paid_at is not null)
        order by o.paid_at desc, o.order_id limit 50
    """, (tenant, account["customer_id"])) if section == "top-up" else []
    return page(salon=salon, mode="dashboard", account=account, minutes=minutes, history=history, sessions=sessions,
                customer_section=section, details=details, busyness=customer_busyness(tenant) if details and not section else [], course_orders=course_orders, offers=offers(tenant) if section == "top-up" else [],
                checkout_enabled=section == "top-up" and configured(tenant))


def tanning_history(tenant, customer_id):
    from storage import postgres

    if postgres():
        kind = "payload_json::jsonb ->> 'transaction_type'"
        local_id = "payload_json::jsonb ->> 'customer_id'"
        normalise = f"trim(both '-' from regexp_replace(lower(trim({local_id})), '[^a-z0-9]+', '-', 'g'))"
        prefix = p.make_cloud_customer_public_id(tenant, "x")[:-1]
        events = p.platform_query_all(f"""
            select occurred_at, payload_json from cloud_sync_events
            where business_account_public_id=? and event_type='transaction.completed'
              and {kind}='tanning_sale'
              and ? || coalesce(nullif({normalise}, ''), 'customer')=?
              and nullif(trim({local_id}), '') is not null
            order by occurred_at desc,id desc limit 50
        """, (tenant, prefix, customer_id))
    else:
        events = p.platform_query_all("""
            select occurred_at,payload_json from cloud_sync_events
            where business_account_public_id=? and event_type='transaction.completed'
              and json_extract(payload_json,'$.transaction_type')='tanning_sale'
            order by occurred_at desc,id desc
        """, (tenant,))
    sessions = []
    for event in events:
        payload = json.loads(event["payload_json"])
        local_customer_id = payload.get("customer_id")
        if local_customer_id and p.make_cloud_customer_public_id(tenant, local_customer_id) == customer_id:
            sessions.append({"date": event["occurred_at"], "minutes": payload.get("minutes")})
            if len(sessions) == 50:
                break
    return sessions


@bp.post("/salon/<tenant>/logout")
def logout(tenant):
    verify_csrf()
    clear_customer_session()
    return redirect(url_for("salon.customer_login", tenant=tenant), code=303)


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
