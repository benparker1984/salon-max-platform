"""Channel-specific marketing preferences and durable campaign outbox."""
import os
import re
import secrets
import smtplib
from email.message import EmailMessage
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import base64

import click
from flask import Blueprint, abort, redirect, request, url_for

import provider as p
import salon_web as web

bp = Blueprint("marketing", __name__)
CHANNELS = {"email": "email", "sms": "phone"}
ORIGIN = "https://www.salonmax.co.uk"


def init_schema():
    db = p.get_platform_db()
    db.executescript("""
        create table if not exists web_marketing_consent (
            tenant text not null, customer_id text not null, channel text not null,
            destination text not null, granted integer not null, updated_at text not null,
            primary key(tenant, customer_id, channel)
        );
        create table if not exists web_marketing_outbox (
            message_id text primary key, tenant text not null, customer_id text not null,
            channel text not null, destination text not null, subject text not null,
            body text not null, unsubscribe_hash text not null unique, status text not null,
            created_at text not null, attempted_at text
        );
    """)
    db.commit()


def customer_record(tenant, customer_id):
    return p.platform_query_one("""
        select email, phone from cloud_customers
        where business_account_public_id=? and customer_public_id=? and status='active'
    """, (tenant, customer_id))


def destination(customer, channel):
    value = (customer[CHANNELS[channel]] or "").strip()
    if channel == "email":
        return value.lower() if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value) else ""
    value = re.sub(r"[ ()-]", "", value)
    if value.startswith("07") and len(value) == 11:
        value = "+44" + value[1:]
    return value if re.fullmatch(r"\+[1-9][0-9]{7,14}", value) else ""


@bp.route("/salon/<tenant>/preferences", methods=["GET", "POST"])
def preferences(tenant):
    salon = web.tenant_record(tenant)
    account = web.member(tenant, {"customer"})
    customer_id = account["customer_id"]
    customer = customer_record(tenant, customer_id)
    if not customer:
        abort(403)
    addresses = {channel: destination(customer, channel) for channel in CHANNELS}
    if request.method == "POST":
        web.verify_csrf()
        db = p.get_platform_db()
        for channel, address in addresses.items():
            db.execute("""
                insert into web_marketing_consent values (?,?,?,?,?,?)
                on conflict(tenant,customer_id,channel) do update set destination=excluded.destination,
                    granted=excluded.granted, updated_at=excluded.updated_at
            """, (tenant, customer_id, channel, address, int(bool(address and request.form.get(channel))), p.now_utc_text()))
        db.commit()
        return redirect(url_for("marketing.preferences", tenant=tenant), code=303)
    saved = p.platform_query_all("""
        select channel,destination from web_marketing_consent where tenant=? and customer_id=? and granted=1
    """, (tenant, customer_id))
    enabled = {row["channel"] for row in saved if addresses.get(row["channel"]) == row["destination"]}
    return web.page(salon=salon, mode="preferences", addresses=addresses, enabled=enabled)


@bp.route("/salon/<tenant>/marketing", methods=["GET", "POST"])
def campaigns(tenant):
    salon = web.tenant_record(tenant)
    account = web.member(tenant, {"owner"})
    if request.method == "POST":
        web.verify_csrf()
        channel = request.form.get("channel")
        subject = request.form.get("subject", "").strip()
        body = request.form.get("body", "").strip()
        if channel not in CHANNELS or not body or len(body) > (300 if channel == "sms" else 5000):
            abort(400, "Check the channel and message length.")
        if not subject or len(subject) > 100 or "\n" in subject or "\r" in subject:
            abort(400, "Enter a subject of up to 100 characters.")
        db = p.get_platform_db()
        recipients = db.execute("""
            select customer_id,destination from web_marketing_consent
            where tenant=? and channel=? and granted=1
        """, (tenant, channel)).fetchall()
        for row in recipients:
            customer = customer_record(tenant, row["customer_id"])
            address = destination(customer, channel) if customer else ""
            if not address or address != row["destination"]:
                continue
            token = secrets.token_urlsafe(24)
            link = ORIGIN + url_for("marketing.unsubscribe", token=token)
            message = f"{salon['business_name']}\n\n{body}\n\nUnsubscribe: {link}"
            db.execute("insert into web_marketing_outbox values (?,?,?,?,?,?,?,?,?,?,null)",
                       (secrets.token_hex(20), tenant, row["customer_id"], channel, address, subject,
                        message, web.digest(token), "queued", p.now_utc_text()))
        db.commit()
        return redirect(url_for("marketing.campaigns", tenant=tenant), code=303)
    counts = p.platform_query_all("""
        select channel,status,count(*) as total from web_marketing_outbox
        where tenant=? group by channel,status order by channel,status
    """, (tenant,))
    return web.page(salon=salon, mode="marketing", counts=counts, team_account=account)


@bp.route("/salon/unsubscribe/<token>", methods=["GET", "POST"])
def unsubscribe(token):
    if len(token) != 32:
        abort(404)
    row = p.platform_query_one("select tenant,customer_id,channel,destination from web_marketing_outbox where unsubscribe_hash=?",
                               (web.digest(token),))
    if not row:
        abort(404)
    salon = p.platform_query_one("select business_name from cloud_business_accounts where business_account_public_id=?",
                                 (row["tenant"],))
    if not salon:
        abort(404)
    if request.method == "POST":
        web.verify_csrf()
        p.platform_execute("""
            update web_marketing_consent set granted=0,updated_at=?
            where tenant=? and customer_id=? and channel=? and destination=?
        """, (p.now_utc_text(), row["tenant"], row["customer_id"], row["channel"], row["destination"]))
    return web.page(salon=salon, mode="unsubscribe", completed=request.method == "POST", channel=row["channel"])


def deliver(row):
    if row["channel"] == "email":
        message = EmailMessage()
        message["From"] = os.environ["SALONMAX_MARKETING_EMAIL_FROM"]
        message["To"] = row["destination"]
        message["Subject"] = row["subject"]
        message.set_content(row["body"])
        with smtplib.SMTP_SSL(os.environ["SALONMAX_MARKETING_SMTP_HOST"], timeout=15) as server:
            server.login(os.environ["SALONMAX_MARKETING_SMTP_USER"], os.environ["SALONMAX_MARKETING_SMTP_PASSWORD"])
            server.send_message(message)
    else:
        sid = os.environ["SALONMAX_TWILIO_SID"]
        auth = base64.b64encode(f"{sid}:{os.environ['SALONMAX_TWILIO_TOKEN']}".encode()).decode()
        data = urlencode({"To": row["destination"], "From": os.environ["SALONMAX_TWILIO_FROM"], "Body": row["body"]})
        with urlopen(Request(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                             data.encode(), {"Authorization": f"Basic {auth}"}), timeout=15):
            pass


def dispatch(limit):
    for row in p.platform_query_all("select * from web_marketing_outbox where status='queued' order by created_at limit ?", (limit,)):
        db = p.get_platform_db()
        claimed = db.execute("update web_marketing_outbox set status='sending',attempted_at=? where message_id=? and status='queued'",
                             (p.now_utc_text(), row["message_id"]))
        db.commit()
        if claimed.rowcount != 1:
            continue
        consent = p.platform_query_one("""
            select granted,destination from web_marketing_consent where tenant=? and customer_id=? and channel=?
        """, (row["tenant"], row["customer_id"], row["channel"]))
        customer = customer_record(row["tenant"], row["customer_id"])
        salon = p.platform_query_one("select * from cloud_business_accounts where business_account_public_id=? and product_type='salon'",
                                     (row["tenant"],))
        status = "suppressed"
        if (consent and consent["granted"] and customer and p.business_access_state(salon)[0]
                and consent["destination"] == row["destination"] == destination(customer, row["channel"])):
            try:
                deliver(row)
                status = "sent"
            except Exception:
                status = "needs_review"
        p.platform_execute("update web_marketing_outbox set status=? where message_id=?", (status, row["message_id"]))


@bp.cli.command("dispatch")
@click.option("--limit", default=25, type=click.IntRange(1, 100))
def dispatch_command(limit):
    """Deliver queued campaigns using configured providers."""
    dispatch(limit)
