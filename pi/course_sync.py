"""Keep the paired till and hosted course catalogue in sync by package code."""
import argparse
import json
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.request import Request, urlopen

FIELDS = ("title", "minutes", "amount", "validity_days", "active")


def catalogue(db):
    rows = db.execute("select * from package_products").fetchall()
    return {
        row["code"]: dict(zip(FIELDS, (
            row["name"], row["minutes_included"],
            int((Decimal(str(row["price"])) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
            row["validity_days"], row["is_active"],
        ))) for row in rows
    }


def sync(db, send, initial=False):
    db.execute("create table if not exists local_course_sync (id integer primary key, catalogue text not null)")
    saved = db.execute("select catalogue from local_course_sync where id=1").fetchone()
    previous = json.loads(saved[0]) if saved else {}
    before = catalogue(db)
    changes = []
    if initial or saved:
        for code, course in before.items():
            if initial or previous.get(code) != course:
                changes.append({"code": code, **course})
        for code, course in previous.items():
            if code not in before and course["active"]:
                changes.append({"code": code, **course, "active": 0})
    remote = send({"initial": initial, "courses": changes} if changes or initial else None)
    if not remote and not saved and not initial:
        return
    with db:
        db.execute("begin immediate")
        current = catalogue(db)
        if current != before:
            return
        for course in remote:
            db.execute("""
                insert into package_products (code,name,minutes_included,price,validity_days,is_active)
                values (?,?,?,?,?,?)
                on conflict(code) do update set name=excluded.name,minutes_included=excluded.minutes_included,
                    price=excluded.price,validity_days=excluded.validity_days,is_active=excluded.is_active
            """, (course["code"], course["title"], course["minutes"], course["amount"] / 100,
                  course["validity_days"], course["active"]))
        db.execute("""
            insert into local_course_sync values (1,?)
            on conflict(id) do update set catalogue=excluded.catalogue
        """, (json.dumps(catalogue(db), sort_keys=True),))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--initial-from-till", action="store_true")
    args = parser.parse_args()
    with sqlite3.connect(Path.home() / "till-build/till_backoffice.db", timeout=10) as db:
        db.row_factory = sqlite3.Row
        settings = db.execute("select sync_api_base_url,sync_stub_enabled from business_settings where id=1").fetchone()
        identity = db.execute("select * from local_device_identity order by id desc limit 1").fetchone()
        lease = db.execute("select signed_token from local_licence_lease order by id desc limit 1").fetchone()
        if not settings or settings["sync_stub_enabled"] or not identity or not lease:
            raise RuntimeError("Course sync requires a paired till.")
        url = settings["sync_api_base_url"].rstrip("/") + "/courses"
        if not url.startswith("https://"):
            raise RuntimeError("Course sync requires HTTPS.")
        headers = {
            "X-SalonMax-Business-Id": identity["business_account_public_id"],
            "X-SalonMax-Device-Id": identity["terminal_device_public_id"],
            "Authorization": "Bearer " + lease["signed_token"],
            "Content-Type": "application/json",
        }

        def send(body):
            data = None if body is None else json.dumps(body).encode()
            with urlopen(Request(url, data=data, headers=headers), timeout=12) as response:
                result = json.load(response)
            if result.get("ok") is not True:
                raise RuntimeError("Course sync was not acknowledged.")
            return result["courses"]

        sync(db, send, initial=args.initial_from_till)


if __name__ == "__main__":
    main()
