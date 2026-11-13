"""Stripe Checkout for in-salon services."""
import csv
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from decimal import Decimal
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from flask import Blueprint, abort, flash, g, jsonify, redirect, request, url_for

import provider as p
import salon_web as web
from storage import postgres

bp = Blueprint("checkout", __name__)


def init_schema():
    db = p.get_platform_db()
    db.executescript("""
        create table if not exists web_offers (
            tenant text not null,
            offer_id text not null,
            title text not null,
            minutes integer not null check(minutes > 0),
            amount integer not null check(amount > 0),
            active integer not null default 1,
            primary key(tenant, offer_id)
        );
        create table if not exists web_orders (
            order_id text primary key,
            tenant text not null,
            customer_id text not null,
            stripe_account text not null,
            offer_id text not null,
            minutes integer not null,
            amount integer not null,
            paid_at text
        );
    """)
    columns = {row["name"] for row in db.execute("select * from pragma_table_info('web_offers')")}
    if "validity_days" not in columns:
        db.execute("alter table web_offers add column validity_days integer not null default 365")
    db.execute("create table if not exists till_course_sync (tenant text primary key)")
    db.execute("""create unique index if not exists one_pending_salon_order
        on web_orders (tenant,customer_id,offer_id) where stripe_account='' and paid_at is null""")
    db.commit()


def stripe_account(tenant):
    return json.loads(os.environ.get("SALONMAX_STRIPE_ACCOUNTS", "{}")).get(tenant)


def configured(tenant):
    return bool(stripe_account(tenant) and os.environ.get("SALONMAX_STRIPE_SECRET_KEY")
                and os.environ.get("SALONMAX_STRIPE_WEBHOOK_SECRET"))


def offers(tenant):
    return p.platform_query_all("select * from web_offers where tenant=? and active=1 order by amount", (tenant,))


@bp.route("/v1/courses", methods=["GET", "POST"])
def sync_courses():
    tenant = g.device_terminal["business_account_public_id"]
    db = p.get_platform_db()
    if request.method == "POST":
        body = request.get_json()
        changes = body.get("courses")
        if not isinstance(changes, list) or len(changes) > 500:
            abort(400, "Invalid course updates.")
        if postgres():
            db.raw.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (tenant,))
        enrolled = db.execute("select 1 from till_course_sync where tenant=?", (tenant,)).fetchone()
        if body.get("initial") is True and not enrolled:
            db.execute("update web_offers set active=0 where tenant=?", (tenant,))
            db.execute("insert into till_course_sync (tenant) values (?)", (tenant,))
        for course in changes:
            if not isinstance(course, dict):
                abort(400, "Invalid course.")
            code = course.get("code")
            title = course.get("title")
            if (not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", code)
                    or not isinstance(title, str) or not title.strip() or len(title) > 80
                    or any(type(course.get(field)) is not int or not low <= course[field] <= high
                           for field, low, high in (("minutes", 1, 10000), ("amount", 1, 100000),
                                                    ("validity_days", 0, 36500), ("active", 0, 1)))):
                abort(400, "Check the course code, name, minutes, price and validity.")
            db.execute("""
                insert into web_offers (tenant,offer_id,title,minutes,amount,validity_days,active)
                values (?,?,?,?,?,?,?)
                on conflict (tenant,offer_id) do update set title=excluded.title,
                    minutes=excluded.minutes, amount=excluded.amount,
                    validity_days=excluded.validity_days, active=excluded.active
            """, (tenant, "till:" + code, title, course["minutes"], course["amount"],
                  course["validity_days"], course["active"]))
        db.commit()
    rows = db.execute("""
        select substr(offer_id,6) as code,title,minutes,amount,validity_days,active
        from web_offers where tenant=? and offer_id like 'till:%' order by offer_id
    """, (tenant,)).fetchall()
    return jsonify(ok=True, courses=[dict(row) for row in rows])


@bp.route("/salon/<tenant>/offers", methods=["GET", "POST"])
def manage(tenant):
    salon = web.tenant_record(tenant)
    account = web.member(tenant, {"owner"})
    if request.method == "POST":
        web.verify_csrf()
        if request.form.get("confirm_paid"):
            db = p.get_platform_db()
            order = db.execute("""
                select * from web_orders where tenant=? and order_id=? and stripe_account=''
            """, (tenant, request.form["confirm_paid"])).fetchone()
            if not order:
                abort(404)
            credit_order(db, order)
            flash("Payment recorded and minutes credited.", "courses")
            return redirect(url_for("checkout.manage", tenant=tenant), code=303)
        offer_id = request.form.get("retire")
        edit_id = request.form.get("edit")
        if request.form.get("action") == "import-till-courses":
            count = import_till_courses(tenant, request.form.get("courses", ""))
            flash(f"Imported {count} courses from the till list.", "courses")
        elif offer_id:
            p.platform_execute("update web_offers set active=0 where tenant=? and offer_id=?", (tenant, offer_id))
        else:
            title = request.form.get("title", "").strip()
            try:
                minutes = int(request.form.get("minutes", ""))
                amount = int(request.form.get("amount", ""))
                validity_days = int(request.form.get("validity_days", "365"))
            except ValueError:
                abort(400, "Enter whole minutes and a price in pence.")
            if not title or len(title) > 80 or not 1 <= minutes <= 10000 or not 30 <= amount <= 100000 or not 0 <= validity_days <= 36500:
                abort(400, "Check the package name, minutes and price.")
            db = p.get_platform_db()
            enrolled = db.execute("select 1 from till_course_sync where tenant=?", (tenant,)).fetchone()
            if edit_id:
                db.execute("""
                    update web_offers set title=?,minutes=?,amount=?,validity_days=?
                    where tenant=? and offer_id=? and active=1
                """, (title, minutes, amount, validity_days, tenant, edit_id))
            else:
                code = ("till:WEB-" if enrolled else "") + secrets.token_hex(8)
                db.execute("insert into web_offers (tenant,offer_id,title,minutes,amount,validity_days) values (?,?,?,?,?,?)",
                           (tenant, code, title, minutes, amount, validity_days))
            db.commit()
        return redirect(url_for("checkout.manage", tenant=tenant), code=303)
    sales = p.platform_query_all("""
        select orders.paid_at, orders.minutes, orders.amount, offers.title
        from web_orders orders left join web_offers offers
          on offers.tenant=orders.tenant and offers.offer_id=orders.offer_id
        where orders.tenant=? and orders.paid_at is not null
        order by orders.paid_at desc,orders.order_id desc limit 50
    """, (tenant,))
    pending = p.platform_query_all("""
        select o.order_id,o.minutes,o.amount,c.first_name,c.last_name,c.customer_number,w.title
        from web_orders o join cloud_customers c
          on c.business_account_public_id=o.tenant and c.customer_public_id=o.customer_id
        join web_offers w on w.tenant=o.tenant and w.offer_id=o.offer_id
        where o.tenant=? and o.stripe_account='' and o.paid_at is null
        order by o.order_id
    """, (tenant,))
    return web.page(salon=salon, mode="offers", pending=pending, offers=offers(tenant), sales=sales, team_account=account)


def import_till_courses(tenant, raw):
    if not raw.strip() or len(raw) > 24000:
        abort(400, "Paste up to 100 courses from your till.")
    courses = {}
    separator = "\t" if "\t" in raw.partition("\n")[0] else ","
    for row in csv.reader(raw.splitlines(), delimiter=separator):
        if not row or not any(cell.strip() for cell in row):
            continue
        if [cell.strip().lower() for cell in row[:2]] == ["name", "code"]:
            continue
        if len(row) not in (4, 5):
            abort(400, "Each course needs a name, code, minutes and GBP price.")
        title, code, minutes_text, price_text = (cell.strip() for cell in row[:4])
        code = code.upper()
        pounds = price_text.removeprefix("GBP ").removeprefix(chr(163)).strip()
        if not re.fullmatch(r"[0-9]{1,5}", minutes_text) or not re.fullmatch(r"[0-9]{1,5}(?:\.[0-9]{1,2})?", pounds):
            abort(400, "Check the course minutes and GBP price.")
        minutes, price = int(minutes_text), int(Decimal(pounds) * 100)
        if (not title or len(title) > 80 or not re.fullmatch(r"[A-Z0-9_-]{1,40}", code)
                or not 1 <= minutes <= 10000 or not 30 <= price <= 100000
                or code in courses or len(courses) >= 100):
            abort(400, "Check the course details and use each package code once.")
        validity = row[4].strip() if len(row) == 5 else "365"
        if not validity.isdecimal() or not 0 <= int(validity) <= 36500:
            abort(400, "Enter valid course days.")
        courses[code] = (title, minutes, price, int(validity))
    if not courses:
        abort(400, "Paste at least one course.")
    db = p.get_platform_db()
    for code, (title, minutes, price, validity) in courses.items():
        db.execute("""
            insert into web_offers (tenant,offer_id,title,minutes,amount,validity_days)
            values (?,?,?,?,?,?)
            on conflict (tenant,offer_id) do update set title=excluded.title,
                minutes=excluded.minutes, amount=excluded.amount, validity_days=excluded.validity_days
        """, (tenant, "till:" + code, title, minutes, price, validity))
    db.commit()
    return len(courses)


@bp.post("/salon/<tenant>/buy")
def buy(tenant):
    web.tenant_record(tenant)
    account = web.member(tenant, {"customer"})
    web.verify_csrf()
    customer = p.platform_query_one("""
        select customer_public_id from cloud_customers
        where business_account_public_id=? and customer_public_id=? and status='active'
    """, (tenant, account["customer_id"]))
    if not customer:
        abort(403)
    offer = p.platform_query_one("select * from web_offers where tenant=? and offer_id=? and active=1",
                                 (tenant, request.form.get("offer_id")))
    if not offer:
        abort(404)
    connected = stripe_account(tenant)
    secret = os.environ.get("SALONMAX_STRIPE_SECRET_KEY")
    order_id = secrets.token_hex(24)
    if not configured(tenant):
        p.platform_execute("""
            insert into web_orders (order_id,tenant,customer_id,stripe_account,offer_id,minutes,amount)
            values (?,?,?,'',?,?,?) on conflict do nothing
        """, (order_id, tenant, customer["customer_public_id"], offer["offer_id"], offer["minutes"], offer["amount"]))
        flash("Course requested. Pay at the salon. Your minutes will appear after the salon confirms payment.", "course-order")
        return redirect(url_for("salon.dashboard", tenant=tenant, section="top-up"), code=303)
    p.platform_execute("""
        insert into web_orders (order_id,tenant,customer_id,stripe_account,offer_id,minutes,amount)
        values (?,?,?,?,?,?,?)
    """, (order_id, tenant, customer["customer_public_id"], connected, offer["offer_id"], offer["minutes"], offer["amount"]))
    portal = "https://www.salonmax.co.uk" + url_for("salon.dashboard", tenant=tenant)
    params = {
        "mode": "payment",
        "payment_method_types[0]": "card",
        "success_url": portal + "?checkout=returned",
        "cancel_url": "https://www.salonmax.co.uk" + url_for("salon.dashboard", tenant=tenant, section="top-up"),
        "metadata[order_id]": order_id,
        "line_items[0][quantity]": 1,
        "line_items[0][price_data][currency]": "gbp",
        "line_items[0][price_data][unit_amount]": offer["amount"],
        "line_items[0][price_data][product_data][name]": offer["title"],
    }
    stripe_request = Request("https://api.stripe.com/v1/checkout/sessions", urlencode(params).encode(),
                             headers={"Authorization": f"Bearer {secret}", "Stripe-Account": connected,
                                      "Idempotency-Key": order_id})
    try:
        with urlopen(stripe_request, timeout=10) as response:
            destination = json.load(response)["url"]
    except (URLError, TimeoutError, KeyError, ValueError):
        abort(502, "Checkout is temporarily unavailable.")
    parsed = urlsplit(destination)
    if parsed.scheme != "https" or parsed.netloc != "checkout.stripe.com":
        abort(502)
    return redirect(destination, code=303)


def verified_event():
    secret = os.environ.get("SALONMAX_STRIPE_WEBHOOK_SECRET")
    if not secret:
        abort(503)
    body = request.get_data()
    pieces = [part.strip().split("=", 1) for part in request.headers.get("Stripe-Signature", "").split(",")]
    values = [(pair[0], pair[1]) for pair in pieces if len(pair) == 2]
    timestamp = next((value for key, value in values if key == "t"), "")
    if not timestamp.isascii() or not timestamp.isdigit() or len(timestamp) > 12 or abs(time.time() - int(timestamp)) > 300:
        abort(400)
    signature = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(value.encode(), signature.encode()) for key, value in values if key == "v1"):
        abort(400)
    return request.get_json()


@bp.post("/salon/payments/webhook")
def webhook():
    event = verified_event()
    if event["type"] not in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        return "", 204
    checkout = event["data"]["object"]
    order_id = (checkout.get("metadata") or {}).get("order_id")
    if not order_id or checkout.get("payment_status") != "paid":
        return "", 204
    db = p.get_platform_db()
    order = db.execute("select * from web_orders where order_id=?", (order_id,)).fetchone()
    if not order:
        return "", 204
    if not order["stripe_account"]:
        abort(400)
    if (checkout.get("currency") != "gbp" or checkout.get("amount_total") != order["amount"]
            or event.get("account") != order["stripe_account"]):
        abort(400)
    credit_order(db, order)
    return "", 204


def credit_order(db, order):
    tenant, customer, order_id = order["tenant"], order["customer_id"], order["order_id"]
    if postgres():
        db.raw.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (tenant,))
    if not db.execute("""
        select 1 from cloud_customers
        where business_account_public_id=? and customer_public_id=? and status='active'
    """, (tenant, customer)).fetchone():
        abort(409, "The linked salon account is inactive.")
    now = p.now_utc_text()
    claimed = db.execute("update web_orders set paid_at=? where order_id=? and paid_at is null", (now, order_id))
    if claimed.rowcount == 1:
        summary = p.ensure_cloud_customer_minute_summary(db, tenant, customer)
        if summary["business_account_public_id"] != tenant:
            abort(409)
        after = summary["minutes_available"] + order["minutes"]
        db.execute("""
            insert into cloud_customer_minute_ledger
            (business_account_public_id, customer_public_id, source_reference, entry_type,
             delta_minutes, minutes_after, notes, created_at)
            values (?, ?, ?, 'web_purchase', ?, ?, 'Online package purchase', ?)
        """, (tenant, customer, order_id, order["minutes"], after, now))
        db.execute("""
            update cloud_customer_minute_summary set minutes_available=?, updated_at=?
            where business_account_public_id=? and customer_public_id=?
        """, (after, now, tenant, customer))
    db.commit()
