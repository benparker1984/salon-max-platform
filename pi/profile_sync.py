"""Synchronize this till's customer profiles with its paired salon."""
import json
import os
import sqlite3
from datetime import date
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

FIELDS = ("first_name", "last_name", "birthday", "email", "phone", "postal_address")


def install(db):
    columns = {row["name"] for row in db.execute("pragma table_info(customers)")}
    for column in ("birthday", "postal_address", "profile_synced_at"):
        if column not in columns:
            db.execute(f"alter table customers add column {column} text")
    db.execute("""create table if not exists local_profile_outbox (
        id integer primary key, event_id text not null, customer_number text not null, changes text not null
    )""")
    pairs = ", ".join(
        f"'{field}', case when new.{field} is not old.{field} then coalesce(new.{field}, '') end"
        for field in FIELDS
    )
    changed = " or ".join(f"new.{field} is not old.{field}" for field in FIELDS)
    db.execute(f"""create trigger if not exists queue_customer_profile_edit
        after update of {", ".join(FIELDS)} on customers
        when new.profile_synced_at is old.profile_synced_at and ({changed})
        begin
            insert into local_profile_outbox(event_id, customer_number, changes)
            values ('profile_' || lower(hex(randomblob(16))), new.customer_number, json_object({pairs}));
        end
    """)


def sync(db, send):
    blocked = set()
    for row in db.execute("select * from local_profile_outbox order by id").fetchall():
        number = row["customer_number"]
        if number in blocked:
            continue
        changes = {key: value for key, value in json.loads(row["changes"]).items() if value is not None}
        try:
            send({"event_id": row["event_id"], "customer_number": number, "changes": changes})
        except HTTPError as error:
            if error.code not in (400, 409):
                raise
            blocked.add(number)
            print(f"Customer profile sync needs attention (HTTP {error.code}).", flush=True)
            continue
        with db:
            db.execute("delete from local_profile_outbox where id=?", (row["id"],))
    for profile in send()["profiles"]:
        version = profile["profile_synced_at"]
        if not version:
            local = db.execute(
                "select birthday,postal_address from customers where customer_number=?",
                (profile["customer_number"],),
            ).fetchone()
            if local and profile["customer_number"] not in blocked:
                initial = {"postal_address": local["postal_address"]} if local["postal_address"] else {}
                birthday = local["birthday"] or ""
                try:
                    if date.fromisoformat(birthday).isoformat() == birthday and date.fromisoformat(birthday) <= date.today():
                        initial["birthday"] = birthday
                except ValueError:
                    pass
                if initial:
                    send({"event_id": "profile_" + uuid.uuid4().hex, "initial": True,
                          "customer_number": profile["customer_number"], "changes": initial})
            continue
        with db:
            db.execute(f"""
                update customers set {", ".join(field + "=?" for field in FIELDS)}, profile_synced_at=?, is_active=?
                where customer_number=? and coalesce(profile_synced_at,'') < ?
                  and not exists (
                    select 1 from local_profile_outbox where customer_number=customers.customer_number
                  )
            """, (*[profile[field] for field in FIELDS], version, int(profile.get("status", "active") == "active"), profile["customer_number"], version))


def main():
    path = Path(os.environ.get("SALONMAX_DB_PATH") or Path.home() / "till-build/till_backoffice.db")
    with sqlite3.connect(path, timeout=10) as db:
        db.row_factory = sqlite3.Row
        install(db)
        db.commit()
        settings = db.execute("select sync_api_base_url,sync_stub_enabled from business_settings where id=1").fetchone()
        identity = db.execute("select * from local_device_identity order by id desc limit 1").fetchone()
        licence = db.execute("select signed_token from local_licence_lease order by id desc limit 1").fetchone()
        if not settings or settings["sync_stub_enabled"] or not identity or not licence:
            return
        url = settings["sync_api_base_url"].rstrip("/") + "/customers/profiles"
        if not url.startswith("https://"):
            raise RuntimeError("Profile sync requires an HTTPS API.")
        headers = {
            "X-SalonMax-Business-Id": identity["business_account_public_id"],
            "X-SalonMax-Device-Id": identity["terminal_device_public_id"],
            "Authorization": "Bearer " + licence["signed_token"],
            "Content-Type": "application/json",
        }

        def send(body=None):
            data = None if body is None else json.dumps(body).encode()
            with urlopen(Request(url, data=data, headers=headers), timeout=12) as response:
                result = json.load(response)
            if result.get("ok") is not True:
                raise RuntimeError("Profile sync was not acknowledged.")
            return result

        sync(db, send)

        site = db.execute("select id from sites where is_active=1 order by id limit 1").fetchone()
        if site:
            beds = [row[0] for row in db.execute(
                "select device_number from devices where site_id=? and is_active=1", (site[0],)
            )]
            inventory_url = settings["sync_api_base_url"].rstrip("/") + "/estate-inventory"
            body = json.dumps({"beds": beds}).encode()
            with urlopen(Request(inventory_url, data=body, headers=headers), timeout=12) as response:
                inventory = json.load(response)
            if inventory.get("ok") is True:
                with db:
                    db.execute("create table if not exists local_estate_inventory (id integer primary key, data text)")
                    db.execute("""insert into local_estate_inventory values (1,?)
                                  on conflict(id) do update set data=excluded.data""", (json.dumps(inventory),))


if __name__ == "__main__":
    main()
