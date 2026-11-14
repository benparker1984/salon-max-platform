"""Shared customer profile writes and authenticated till synchronization."""
import re
from datetime import date, datetime, timezone

from flask import Blueprint, g, jsonify, request

import provider as p
import storage

bp = Blueprint("customer_profiles", __name__)
LIMITS = {"first_name": 100, "last_name": 100, "birthday": 10,
          "email": 254, "phone": 50, "postal_address": 500}


class ProfileError(ValueError):
    pass


def save_profile(db, tenant, customer_id, changes, *, expected=None):
    values = {}
    for field, limit in LIMITS.items():
        if field not in changes:
            continue
        value = changes[field]
        if not isinstance(value, str) or len(value.strip()) > limit or "\x00" in value:
            raise ProfileError("Check the length and format of the customer details.")
        values[field] = value.strip()
    if not values or any(values.get(field) == "" for field in ("first_name", "last_name")):
        raise ProfileError("Enter the customer's first and last names.")
    if values.get("email") and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", values["email"]):
        raise ProfileError("Enter a valid contact email.")
    if values.get("birthday"):
        try:
            birthday = date.fromisoformat(values["birthday"])
            if birthday.isoformat() != values["birthday"] or birthday > date.today():
                raise ValueError
        except ValueError:
            raise ProfileError("Enter a valid date of birth.") from None
    version = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    assignments = ", ".join(f"{field}=?" for field in values)
    where = "business_account_public_id=? and customer_public_id=? and status='active'"
    params = [*values.values(), version, tenant, customer_id]
    if expected is not None:
        where += " and profile_synced_at=?"
        params.append(expected)
    result = db.execute(
        f"update cloud_customers set {assignments}, profile_synced_at=? where {where}",
        tuple(params),
    )
    if result.rowcount != 1:
        raise ProfileError("These details changed since you opened the page. Review the latest details and try again.")


@bp.route("/v1/customers/profiles", methods=["GET", "POST"])
def profiles():
    terminal = g.device_terminal
    tenant = terminal["business_account_public_id"]
    device = terminal["terminal_device_public_id"]
    db = p.get_platform_db()
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        number = str(body.get("customer_number") or "").strip()
        event_id = str(body.get("event_id") or "")
        changes = body.get("changes")
        if not number or not re.fullmatch(r"profile_[a-f0-9]{32}", event_id) or not isinstance(changes, dict):
            return jsonify(ok=False, error="Invalid profile update."), 400
        if storage.postgres():
            db.raw.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (tenant,))
        seen = db.execute("""
            select 1 from cloud_sync_events
            where business_account_public_id=? and terminal_device_public_id=? and local_event_id=?
        """, (tenant, device, event_id)).fetchone()
        if not seen:
            matches = db.execute("""
                select customer_public_id,birthday,postal_address,profile_synced_at,status from cloud_customers
                where business_account_public_id=? and customer_number=? and status in ('active','archived')
                limit 2
            """, (tenant, number)).fetchall()
            if len(matches) != 1:
                return jsonify(ok=False, error="The salon customer number must match one active record."), 409
            try:
                match = matches[0]
                if body.get("initial") is True:
                    changes = {field: value for field, value in changes.items()
                               if field in ("birthday", "postal_address") and not match[field]}
                    if match["profile_synced_at"]:
                        changes = {}
                if set(changes) == {"status"} and changes["status"] in ("active", "archived"):
                    db.execute("""
                        update cloud_customers set status=?,profile_synced_at=?,updated_at=current_timestamp
                        where business_account_public_id=? and customer_public_id=?
                    """, (changes["status"], datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                          tenant, match["customer_public_id"]))
                elif changes:
                    save_profile(db, tenant, match["customer_public_id"], changes)
            except ProfileError as error:
                db.rollback()
                return jsonify(ok=False, error=str(error)), 400
            db.execute("""
                insert into cloud_sync_events
                    (business_account_public_id,terminal_device_public_id,local_event_id,event_type,occurred_at,payload_json)
                values (?,?,?,'customer.profile.updated',?,'{}')
            """, (tenant, device, event_id, p.now_utc_text()))
            db.commit()
        return jsonify(ok=True)
    rows = db.execute("""
        select customer_number,first_name,last_name,birthday,email,phone,postal_address,profile_synced_at,status
        from cloud_customers
        where business_account_public_id=? and status in ('active','archived') and customer_number!=''
          and (select count(*) from cloud_customers other
               where other.business_account_public_id=cloud_customers.business_account_public_id
                 and other.customer_number=cloud_customers.customer_number and other.status in ('active','archived'))=1
        order by customer_number
    """, (tenant,)).fetchall()
    response = jsonify(ok=True, profiles=[dict(row) for row in rows])
    response.headers["Cache-Control"] = "no-store"
    return response
