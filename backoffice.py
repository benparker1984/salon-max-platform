"""Tenant-scoped hosted back office and password recovery."""
import json
import ipaddress
import os
import re
import secrets
from decimal import Decimal, InvalidOperation
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from flask import Blueprint, abort, flash, make_response, redirect, render_template, request, session, url_for

import provider as p
import salon_web as web
from werkzeug.security import generate_password_hash
from werkzeug.exceptions import BadRequest, Forbidden, NotFound

bp = Blueprint("backoffice", __name__)
ORIGIN = "https://www.salonmax.co.uk"
SECTIONS = {
    "dashboard": "Dashboard", "settings": "Business Settings", "sunbeds": "Sunbed Settings",
    "customers": "Customers", "staff": "Staff", "pricing": "Pricing", "packages": "Packages",
    "store": "Store", "transactions": "Transactions", "tills": "Till Activity", "reports": "Reports",
}
CATALOG = {
    "sunbeds": ("Bed name", "Maximum minutes"),
    "pricing": ("Price name", "Price in pence"),
    "store": ("Product name", "Stock units"),
}
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
))


def init_schema():
    db = p.get_platform_db()
    db.executescript("""
        create table if not exists web_backoffices (
            tenant text primary key,
            slug text not null unique,
            created_at text not null
        );
        create table if not exists web_backoffice_catalog (
            tenant text not null, section text not null, item_id text not null,
            name text not null, value integer not null,
            primary key(tenant,section,item_id)
        );
        create table if not exists web_account_recovery (
            account_id text primary key,
            code_hash text not null
        );
        create table if not exists web_pi_backoffices (
            terminal_id text primary key,
            tenant text not null,
            origin text not null unique
        );
    """)
    db.commit()


def setup(tenant, business_name, owner_email=None):
    db = p.get_platform_db()
    row = db.execute("select slug from web_backoffices where tenant=?", (tenant,)).fetchone()
    if not row:
        stem = re.sub(r"[^a-z0-9]", "", business_name.lower())[:42] or "salon"
        for suffix in range(1, 1001):
            name = stem if suffix == 1 else f"{stem}-{suffix}"
            slug = f"salonmax-{name}-backoffice"
            db.execute("insert into web_backoffices values (?,?,?) on conflict do nothing",
                       (tenant, slug, p.now_utc_text()))
            row = db.execute("select slug from web_backoffices where tenant=?", (tenant,)).fetchone()
            if row:
                break
        if not row:
            raise RuntimeError("Could not allocate a back office address")
    if owner_email and valid_email(owner_email):
        db.execute("""
            insert into web_accounts (account_id,tenant,login,role)
            values (?,?,?,'owner') on conflict do nothing
        """, (secrets.token_hex(20), tenant, owner_email.strip().lower()))
    db.commit()
    return row["slug"]


def valid_email(value):
    return len(value) <= 254 and bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value.strip()))


def backfill():
    for row in p.platform_query_all("""
        select b.business_account_public_id,b.business_name from cloud_business_accounts b
        left join web_backoffices w on w.tenant=b.business_account_public_id
        where b.product_type='salon' and w.tenant is null
    """):
        setup(row["business_account_public_id"], row["business_name"])


def office_url(tenant):
    row = p.platform_query_one("select slug from web_backoffices where tenant=?", (tenant,))
    return url_for("backoffice.workspace", slug=row["slug"]) if row else None


def paired_tills(tenant):
    return p.platform_query_all("""
        select t.terminal_device_public_id as terminal_id,t.terminal_name,s.site_name,
               t.last_seen_at,w.origin
        from cloud_terminal_registry t
        join cloud_business_sites s on s.site_public_id=t.site_public_id
            and s.business_account_public_id=t.business_account_public_id
        join cloud_device_licences l on l.terminal_device_public_id=t.terminal_device_public_id
            and l.business_account_public_id=t.business_account_public_id
        left join web_pi_backoffices w on w.terminal_id=t.terminal_device_public_id
            and w.tenant=t.business_account_public_id
        where t.business_account_public_id=? and l.licence_status='active'
          and t.status not in ('retired','suspended','deleted')
          and t.management_status not in ('retired','suspended')
          and s.status not in ('suspended','deleted')
        order by s.site_name,t.terminal_name
    """, (tenant,))


def private_pi_origin(value):
    try:
        url = urlsplit(value)
        address = ipaddress.IPv4Address(url.hostname or "")
        port = url.port
    except ValueError:
        return None
    if (url.scheme not in {"http", "https"} or url.username is not None or url.password is not None
            or url.path not in {"", "/"} or url.query or url.fragment or not port
            or not any(address in network for network in PRIVATE_NETWORKS)):
        return None
    return f"{url.scheme}://{address}:{port}"


@bp.route("/platform/business/<tenant>/pi-back-office", methods=["GET", "POST"])
def manage_pi_links(tenant):
    if not (session.get("platform_admin_authenticated") or session.get("salonmax_platform_authed")):
        abort(403)
    salon = web.tenant_record(tenant)
    tills = paired_tills(tenant)
    if request.method == "POST":
        web.verify_csrf()
        terminal = request.form.get("terminal_id", "")
        if terminal not in {row["terminal_id"] for row in tills}:
            abort(404)
        value = request.form.get("origin", "").strip()
        if not value:
            p.platform_execute("delete from web_pi_backoffices where tenant=? and terminal_id=?", (tenant, terminal))
            flash("Pi back office link removed.", "pi-link")
        else:
            origin = private_pi_origin(value)
            if not origin:
                abort(400, "Enter the Pi’s private IP address and port, such as http://100.64.0.10:5001.")
            db = p.get_platform_db()
            # Conflicting addresses and reassigned terminal IDs must never overwrite another salon's mapping.
            db.execute("""
                insert into web_pi_backoffices (terminal_id,tenant,origin) values (?,?,?)
                on conflict do nothing
            """, (terminal, tenant, origin))
            saved = db.execute("""
                select origin from web_pi_backoffices where tenant=? and terminal_id=?
            """, (tenant, terminal)).fetchone()
            if not saved or saved["origin"] != origin:
                db.rollback()
                abort(409, "This terminal or address is already linked. Remove the old link first.")
            db.commit()
            flash("Pi back office link saved. It is available on the salon owner’s dashboard.", "pi-link")
        return redirect(url_for("backoffice.manage_pi_links", tenant=tenant), code=303)
    return render(salon, "", "pi-links", tills=tills)


def recovery_code(db, account_id):
    code = secrets.token_urlsafe(24)
    db.execute("""
        insert into web_account_recovery (account_id,code_hash) values (?,?)
        on conflict(account_id) do update set code_hash=excluded.code_hash
    """, (account_id, web.digest(code)))
    return code


def team_activation_page(salon, token, active):
    row = p.platform_query_one("select slug from web_backoffices where tenant=?",
                               (salon["business_account_public_id"],))
    if row:
        return render(salon, row["slug"], "register", token=token, resetting=active)
    return None


def recovery_page(salon, code):
    row = p.platform_query_one("select slug from web_backoffices where tenant=?",
                               (salon["business_account_public_id"],))
    return render(salon, row["slug"], "recovery-code", recovery_code=code)


@bp.post("/<slug>/sign-out")
def sign_out(slug):
    tenant, _ = resolve(slug)
    web.verify_csrf()
    web.clear_team_session(tenant)
    return redirect(url_for("backoffice.workspace", slug=slug, section="sign-in"), code=303)


def render(salon, slug, mode, **context):
    csrf = session.setdefault("salon_csrf", secrets.token_urlsafe(32))
    response = make_response(render_template("backoffice.html", salon=salon, slug=slug, mode=mode,
                                             sections=SECTIONS, csrf=csrf, **context))
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def resolve(slug):
    row = p.platform_query_one("select tenant from web_backoffices where slug=?", (slug,))
    if not row:
        abort(404)
    return row["tenant"], web.tenant_record(row["tenant"])


@bp.route("/<slug>", defaults={"section": "dashboard"}, methods=["GET", "POST"])
@bp.route("/<slug>/<section>", methods=["GET", "POST"])
def workspace(slug, section):
    if not slug.startswith("salonmax-") or not slug.endswith("-backoffice"):
        abort(404)
    tenant, salon = resolve(slug)
    if section == "forgot-password":
        return forgot(tenant, salon, slug)
    if section == "sign-in":
        return sign_in(tenant, salon, slug)
    if section == "register":
        if request.method == "POST":
            web.verify_csrf()
            setup = request.form.get("setup_code", "").strip()
            token = setup
            if len(setup) <= 512 and "/" in setup:
                try:
                    link = urlsplit(setup)
                    candidate = link.path.rsplit("/", 1)[-1]
                    expected_path = url_for("salon.activate", tenant=tenant, token=candidate)
                    if (link.scheme, link.netloc) in {("", ""), ("https", "www.salonmax.co.uk")} and link.path == expected_path:
                        token = candidate
                except ValueError:
                    pass
            if re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
                try:
                    return web.activate(tenant, token)
                except BadRequest as exc:
                    return render(salon, slug, "register", error=exc.description)
                except NotFound:
                    pass
            return render(salon, slug, "register", error=(
                "This setup link or code is invalid, expired, or already used. "
                "If your password was saved, sign in below. Otherwise, ask for a new setup link."
            ))
        return render(salon, slug, "register")
    if section not in SECTIONS:
        abort(404)
    try:
        account = web.member(tenant, web.TEAM)
    except Forbidden:
        if request.method != "GET":
            raise
        owner = p.platform_query_one("""
            select 1 from web_accounts where tenant=? and role='owner'
              and password_hash is not null and disabled=0 limit 1
        """, (tenant,))
        destination = "sign-in" if owner or session.get("salon_account") else "register"
        return redirect(url_for("backoffice.workspace", slug=slug, section=destination))
    if account["role"] == "owner" and request.method == "GET":
        linked_tills = [row for row in paired_tills(tenant) if row["origin"]]
        if linked_tills:
            if section != "dashboard":
                return redirect(office_url(tenant))
            return render(salon, slug, "linked-office", tills=linked_tills)
    if section in {"staff", "packages"}:
        endpoint = "salon.provision_team" if section == "staff" else "checkout.manage"
        return redirect(url_for(endpoint, tenant=tenant))
    if request.method == "POST":
        web.verify_csrf()
        if account["role"] != "owner":
            abort(403)
        if section == "settings":
            phone = request.form.get("phone", "").strip()
            email = request.form.get("email", "").strip()
            if len(phone) > 40 or (email and not valid_email(email)):
                abort(400)
            p.platform_execute("""
                update cloud_business_accounts set contact_phone=?,contact_email=?,updated_at=?
                where business_account_public_id=?
            """, (phone, email, p.now_utc_text(), tenant))
        elif section in CATALOG:
            edit_catalog(tenant, section)
        else:
            abort(405)
        return redirect(url_for("backoffice.workspace", slug=slug, section=section), code=303)

    columns, records = [], []
    search = request.args.get("q", "").strip()[:100] if section == "customers" else ""
    if section == "customers":
        columns = ["Customer No", "Name", "Account No", "Balance", "Package Minutes"]
        pattern = "%" + search.lower() + "%"
        records = p.platform_query_all("""
            select c.customer_number, c.first_name || ' ' || c.last_name as name,
                   c.account_number, coalesce(b.current_balance,'0.00') as balance,
                   coalesce(m.minutes_available,0) as minutes
            from cloud_customers c left join cloud_customer_minute_summary m
            on m.business_account_public_id=c.business_account_public_id and m.customer_public_id=c.customer_public_id
            left join cloud_customer_balance_summary b
            on b.business_account_public_id=c.business_account_public_id and b.customer_public_id=c.customer_public_id
            where c.business_account_public_id=? and c.status='active'
              and (lower(c.first_name || ' ' || c.last_name) like ?
                   or lower(c.customer_number) like ? or lower(c.account_number) like ?)
            order by c.last_name,c.first_name limit 250
        """, (tenant, pattern, pattern, pattern))
    elif section in {"transactions", "reports"}:
        columns = ["Transaction No", "Customer", "Site", "Staff", "Type", "Total", "Payment", "Time (UTC)"]
        records = transactions(tenant)
    elif section == "tills":
        columns = ["Till", "Site", "Status", "Last check-in"]
        records = p.platform_query_all("""
            select t.terminal_name,s.site_name,t.status,t.last_seen_at
            from cloud_terminal_registry t left join cloud_business_sites s
            on t.business_account_public_id=s.business_account_public_id and t.site_public_id=s.site_public_id
            where t.business_account_public_id=? order by t.terminal_name
        """, (tenant,))
    elif section in CATALOG:
        records = p.platform_query_all("""
            select name,value,item_id from web_backoffice_catalog
            where tenant=? and section=? order by name
        """, (tenant, section))
    rows = [list(row.values()) if isinstance(row, dict) else list(row) for row in records]
    if section == "customers":
        for row in rows:
            try:
                balance = Decimal(str(row[3]))
                row[3] = f"GBP {balance:,.2f}" if balance.is_finite() else "—"
            except InvalidOperation:
                row[3] = "—"
    elif section in {"transactions", "reports"}:
        for row in rows:
            row[5] = f"GBP {row[5]:,.2f}"
    metrics = {}
    sites, staff = [], []
    if section == "dashboard":
        sites = p.platform_query_all("""
            select site_name,site_code,status from cloud_business_sites
            where business_account_public_id=? and status<>'deleted' order by site_name
        """, (tenant,))
        staff = p.platform_query_all("""
            select login,role from web_accounts where tenant=? and role in ('owner','staff')
              and disabled=0 and password_hash is not null order by role,login
        """, (tenant,)) if account["role"] == "owner" else []
    if section in {"dashboard", "reports"}:
        for label, table in [("Customers", "cloud_customers"), ("Sites", "cloud_business_sites"),
                             ("Devices", "cloud_terminal_registry"), ("Online packages", "web_offers")]:
            key = "tenant" if table == "web_offers" else "business_account_public_id"
            metrics[label] = p.platform_query_one(f"select count(*) as total from {table} where {key}=?", (tenant,))["total"]
        sales = records if section == "reports" else transactions(tenant)
        metrics["Recent till sales"] = "£" + format(sum((row[5] for row in sales), Decimal(0)), ",.2f")
    return render(salon, slug, "workspace", section=section, account=account, rows=rows, columns=columns,
                  metrics=metrics, catalog=CATALOG.get(section),
                  sites=sites, staff=staff, search=search)


def transactions(tenant):
    rows = p.platform_query_all("""
        select e.occurred_at,e.payload_json,s.site_name from cloud_sync_events e
        left join cloud_terminal_registry t on t.business_account_public_id=e.business_account_public_id
            and t.terminal_device_public_id=e.terminal_device_public_id
        left join cloud_business_sites s on s.business_account_public_id=t.business_account_public_id
            and s.site_public_id=t.site_public_id
        where e.business_account_public_id=? and e.event_type='transaction.completed'
        order by e.occurred_at desc,e.id desc limit 100
    """, (tenant,))
    customers = {}
    result = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
            amount = Decimal(str(payload.get("total_amount", 0)))
            if not amount.is_finite():
                continue
        except (ValueError, AttributeError, InvalidOperation):
            continue
        local_customer = payload.get("customer_id")
        customer_name = ""
        if isinstance(local_customer, (int, str)) and local_customer != "":
            public_id = p.make_cloud_customer_public_id(tenant, local_customer)
            if public_id not in customers:
                customer = p.platform_query_one("""
                    select first_name,last_name from cloud_customers
                    where business_account_public_id=? and customer_public_id=?
                """, (tenant, public_id))
                customers[public_id] = (customer["first_name"] + " " + customer["last_name"]).strip() if customer else ""
            customer_name = customers[public_id]
        result.append([
            payload.get("transaction_number") or "—", customer_name or "—", row["site_name"] or "—",
            payload.get("staff_name") or "—", payload.get("transaction_type") or "—", amount,
            payload.get("payment_method") or "—", row["occurred_at"],
        ])
    return result


def edit_catalog(tenant, section):
    item_id = request.form.get("item_id", "").strip()
    if request.form.get("action") == "delete":
        p.platform_execute("delete from web_backoffice_catalog where tenant=? and section=? and item_id=?",
                           (tenant, section, item_id))
        return
    name = request.form.get("name", "").strip()
    try:
        value = int(request.form.get("value", ""))
    except ValueError:
        abort(400)
    if not name or len(name) > 100 or not 0 <= value <= 1000000:
        abort(400)
    if item_id:
        p.platform_execute("update web_backoffice_catalog set name=?,value=? where tenant=? and section=? and item_id=?",
                           (name, value, tenant, section, item_id))
    else:
        p.platform_execute("insert into web_backoffice_catalog values (?,?,?,?,?)",
                           (tenant, section, secrets.token_hex(16), name, value))


def sign_in(tenant, salon, slug):
    error = None
    if request.method == "POST":
        web.verify_csrf()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role")
        if role not in web.TEAM or not valid_email(email) or len(password) > 128:
            abort(400)
        if web.authenticate(tenant, email, role, password):
            return redirect(url_for("backoffice.workspace", slug=slug), code=303)
        error = "The email or password did not match."
    return render(salon, slug, "login", error=error)


def forgot(tenant, salon, slug):
    if request.method == "POST":
        web.verify_csrf()
        email = request.form.get("email", "").strip().lower()
        if not valid_email(email):
            abort(400)
        web.throttle(tenant, f"reset:{email}")
        if request.form.get("method") == "recovery-code":
            return recover_with_code(tenant, salon, slug, email)
        key = os.environ.get("SALONMAX_TRANSACTIONAL_EMAIL_KEY")
        sender = os.environ.get("SALONMAX_TRANSACTIONAL_EMAIL_FROM")
        if key and sender:
            rows = p.platform_query_all("""
                select account_id,role from web_accounts
                where tenant=? and login=? and role in ('owner','staff') and disabled=0
            """, (tenant, email))
            links = []
            for row in rows:
                token = secrets.token_urlsafe(32)
                p.platform_execute("""
                    update web_accounts set invite_hash=?,invite_until=?
                    where tenant=? and account_id=? and disabled=0
                """, (web.digest(token), web.stamp(1800), tenant, row["account_id"]))
                links.append(f"{row['role'].title()}: " + ORIGIN + url_for("salon.activate", tenant=tenant, token=token))
            if links:
                message = {"from": sender, "to": [email], "subject": "Salon Max password reset",
                           "text": f"{salon['business_name']} back office\n\n" + "\n".join(links)
                                   + "\n\nThese links expire in 30 minutes."}
                api_request = Request("https://api.resend.com/emails", json.dumps(message).encode(),
                                      {"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
                try:
                    with urlopen(api_request, timeout=8):
                        pass
                except (URLError, TimeoutError):
                    p.app.logger.warning("Back office password reset email delivery failed")
        return render(salon, slug, "forgot", submitted=True, email_enabled=bool(key and sender))
    return render(salon, slug, "forgot", email_enabled=bool(os.environ.get("SALONMAX_TRANSACTIONAL_EMAIL_KEY")
                                                         and os.environ.get("SALONMAX_TRANSACTIONAL_EMAIL_FROM")))


def recover_with_code(tenant, salon, slug, email):
    code = request.form.get("code", "").strip()
    password = request.form.get("password", "")
    if len(code) != 32 or not 12 <= len(password) <= 128:
        abort(400, "Check your recovery code and use a password of 12 to 128 characters.")
    db = p.get_platform_db()
    row = db.execute("""
        select a.account_id,a.password_hash from web_accounts a
        join web_account_recovery r on r.account_id=a.account_id
        where a.tenant=? and a.login=? and a.role in ('owner','staff')
          and a.disabled=0 and r.code_hash=?
    """, (tenant, email, web.digest(code))).fetchone()
    if row:
        changed = db.execute("""
            update web_accounts set password_hash=?,invite_hash=null,invite_until=null
            where account_id=? and tenant=? and password_hash=? and disabled=0
        """, (generate_password_hash(password), row["account_id"], tenant, row["password_hash"]))
        if changed.rowcount == 1:
            replacement = recovery_code(db, row["account_id"])
            db.commit()
            if session.get("salon_account") == row["account_id"]:
                web.clear_customer_session()
            return recovery_page(salon, replacement)
    db.rollback()
    return render(salon, slug, "forgot", error="The email address or recovery code did not match.",
                  email_enabled=bool(os.environ.get("SALONMAX_TRANSACTIONAL_EMAIL_KEY")
                                     and os.environ.get("SALONMAX_TRANSACTIONAL_EMAIL_FROM")))
