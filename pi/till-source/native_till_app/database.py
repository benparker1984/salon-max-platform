from __future__ import annotations

import json
import os
import re
import smtplib
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable

@dataclass
class TillSessionContext:
    id: int
    site_id: int
    terminal_id: int
    opened_by_user_id: int
    opening_float: float
    expected_cash: float
    status: str


class TillRepository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.ensure_local_platform_tables()
        self.ensure_business_settings_platform_columns()

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def fetch_all(self, sql: str, params: Iterable = ()):
        with self.connection() as conn:
            return conn.execute(sql, tuple(params)).fetchall()

    def fetch_one(self, sql: str, params: Iterable = ()):
        with self.connection() as conn:
            return conn.execute(sql, tuple(params)).fetchone()

    def execute(self, sql: str, params: Iterable = ()) -> int:
        with self.connection() as conn:
            cursor = conn.execute(sql, tuple(params))
            return int(cursor.lastrowid)

    def ensure_local_platform_tables(self):
        with self.connection() as conn:
            conn.executescript(
                """
                create table if not exists local_sync_outbox (
                    id integer primary key autoincrement,
                    local_event_uuid text not null unique,
                    event_type text not null,
                    payload_json text not null,
                    status text not null default 'pending',
                    attempt_count integer not null default 0,
                    last_attempt_at text,
                    created_at text not null default current_timestamp,
                    acknowledged_at text
                );

                create table if not exists local_sync_checkpoint (
                    id integer primary key autoincrement,
                    last_cloud_event_id integer not null default 0,
                    last_synced_at text
                );

                create table if not exists local_cloud_correction_checkpoint (
                    id integer primary key autoincrement,
                    last_minute_ledger_id integer not null default 0,
                    last_balance_ledger_id integer not null default 0,
                    last_checked_at text
                );

                create table if not exists local_cloud_customer_directory_checkpoint (
                    id integer primary key autoincrement,
                    last_customer_updated_at text,
                    last_checked_at text
                );

                create table if not exists local_licence_lease (
                    id integer primary key autoincrement,
                    terminal_device_public_id text not null,
                    licence_status text not null,
                    signed_token text not null,
                    issued_at text not null,
                    expires_at text not null,
                    last_verified_at text not null
                );

                create table if not exists local_device_identity (
                    id integer primary key autoincrement,
                    business_account_public_id text,
                    site_public_id text,
                    terminal_device_public_id text,
                    device_serial text,
                    hardware_fingerprint text,
                    created_at text not null default current_timestamp
                );
                """
            )
            checkpoint = conn.execute("select id from local_sync_checkpoint order by id limit 1").fetchone()
            if checkpoint is None:
                conn.execute(
                    "insert into local_sync_checkpoint (last_cloud_event_id, last_synced_at) values (0, null)"
                )
            correction_checkpoint = conn.execute(
                "select id from local_cloud_correction_checkpoint order by id limit 1"
            ).fetchone()
            if correction_checkpoint is None:
                conn.execute(
                    """
                    insert into local_cloud_correction_checkpoint (
                        last_minute_ledger_id,
                        last_balance_ledger_id,
                        last_checked_at
                    ) values (0, 0, null)
                    """
                )
            customer_directory_checkpoint = conn.execute(
                "select id from local_cloud_customer_directory_checkpoint order by id limit 1"
            ).fetchone()
            if customer_directory_checkpoint is None:
                conn.execute(
                    """
                    insert into local_cloud_customer_directory_checkpoint (
                        last_customer_updated_at,
                        last_checked_at
                    ) values (null, null)
                    """
                )

    def ensure_business_settings_platform_columns(self):
        if not self.table_exists("business_settings"):
            return
        existing_columns = {
            row["name"]
            for row in self.fetch_all("select name from pragma_table_info('business_settings')")
        }
        columns_to_add = {
            "sync_api_base_url": "text not null default 'https://api.salonmax.co.uk/v1'",
            "sync_stub_enabled": "integer not null default 1",
            "sync_poll_seconds": "integer not null default 15",
            "salonmax_business_account_public_id": "text not null default ''",
            "salonmax_site_public_id": "text not null default ''",
            "salonmax_remote_setup_version_applied": "integer not null default 0",
            "salonmax_remote_setup_applied_at": "text not null default ''",
        }
        for column_name, column_sql in columns_to_add.items():
            if column_name not in existing_columns:
                with self.connection() as conn:
                    conn.execute(f"alter table business_settings add column {column_name} {column_sql}")

    def enqueue_sync_event(
        self,
        *,
        event_type: str,
        payload: dict,
        local_event_uuid: str | None = None,
    ) -> str:
        event_id = str(local_event_uuid or f"evt_{uuid.uuid4().hex}")
        payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        self.execute(
            """
            insert into local_sync_outbox (
                local_event_uuid,
                event_type,
                payload_json,
                status
            ) values (?, ?, ?, 'pending')
            """,
            (event_id, event_type, payload_json),
        )
        return event_id

    def pending_sync_events(self, limit: int = 100):
        return self.fetch_all(
            """
            select *
            from local_sync_outbox
            where status in ('pending', 'failed')
            order by id
            limit ?
            """,
            (int(limit),),
        )

    def mark_sync_event_acknowledged(self, local_event_uuid: str):
        self.execute(
            """
            update local_sync_outbox
            set status = 'acknowledged',
                acknowledged_at = current_timestamp
            where local_event_uuid = ?
            """,
            (str(local_event_uuid),),
        )

    def mark_sync_event_attempt(self, local_event_uuid: str, *, failed: bool = False):
        self.execute(
            """
            update local_sync_outbox
            set status = ?,
                attempt_count = attempt_count + 1,
                last_attempt_at = current_timestamp
            where local_event_uuid = ?
            """,
            ("failed" if failed else "pending", str(local_event_uuid)),
        )

    def sync_checkpoint(self):
        return self.fetch_one("select * from local_sync_checkpoint order by id limit 1")

    def update_sync_checkpoint(self, last_cloud_event_id: int):
        self.execute(
            """
            update local_sync_checkpoint
            set last_cloud_event_id = ?,
                last_synced_at = current_timestamp
            where id = (select id from local_sync_checkpoint order by id limit 1)
            """,
            (int(last_cloud_event_id),),
        )

    def sync_outbox_health(self) -> dict:
        summary_row = self.fetch_one(
            """
            select
                count(*) as outstanding_count,
                coalesce(sum(case when status = 'pending' then 1 else 0 end), 0) as pending_count,
                coalesce(sum(case when status = 'failed' then 1 else 0 end), 0) as failed_count,
                min(case when status in ('pending', 'failed') then created_at else null end) as oldest_outstanding_at,
                max(last_attempt_at) as last_attempt_at,
                max(acknowledged_at) as last_acknowledged_at
            from local_sync_outbox
            """
        )
        summary = dict(summary_row) if summary_row is not None else {}
        checkpoint_row = self.sync_checkpoint()
        checkpoint = dict(checkpoint_row) if checkpoint_row is not None else {}
        pending_count = int(summary.get("pending_count") or 0)
        failed_count = int(summary.get("failed_count") or 0)
        outstanding_count = int(summary.get("outstanding_count") or 0)
        recent_failed_rows = self.fetch_all(
            """
            select local_event_uuid, event_type, payload_json, attempt_count, created_at, last_attempt_at
            from local_sync_outbox
            where status = 'failed'
            order by id desc
            limit 5
            """
        )
        recent_pending_rows = self.fetch_all(
            """
            select local_event_uuid, event_type, payload_json, attempt_count, created_at, last_attempt_at
            from local_sync_outbox
            where status = 'pending'
            order by id desc
            limit 5
            """
        )
        sync_status = "healthy"
        if failed_count > 0:
            sync_status = "failing"
        elif pending_count > 0:
            sync_status = "pending"

        def summarise_outbox_rows(rows):
            items = []
            for row in rows:
                payload = {}
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                    if not isinstance(payload, dict):
                        payload = {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    payload = {}
                items.append(
                    {
                        "local_event_uuid": str(row["local_event_uuid"] or ""),
                        "event_type": str(row["event_type"] or ""),
                        "attempt_count": int(row["attempt_count"] or 0),
                        "created_at": str(row["created_at"] or ""),
                        "last_attempt_at": str(row["last_attempt_at"] or ""),
                        "transaction_number": str(payload.get("transaction_number") or ""),
                        "transaction_type": str(payload.get("transaction_type") or ""),
                        "customer_number": str(payload.get("customer_number") or ""),
                    }
                )
            return items

        return {
            "outstanding_count": outstanding_count,
            "pending_count": pending_count,
            "failed_count": failed_count,
            "oldest_outstanding_at": str(summary.get("oldest_outstanding_at") or ""),
            "last_attempt_at": str(summary.get("last_attempt_at") or ""),
            "last_acknowledged_at": str(summary.get("last_acknowledged_at") or ""),
            "last_checkpoint_at": str(checkpoint.get("last_synced_at") or ""),
            "sync_status": sync_status,
            "recent_failed_items": summarise_outbox_rows(recent_failed_rows),
            "recent_pending_items": summarise_outbox_rows(recent_pending_rows),
        }

    def cloud_correction_checkpoint(self):
        return self.fetch_one("select * from local_cloud_correction_checkpoint order by id limit 1")

    def update_cloud_correction_checkpoint(self, *, last_minute_ledger_id: int, last_balance_ledger_id: int):
        self.execute(
            """
            update local_cloud_correction_checkpoint
            set last_minute_ledger_id = ?,
                last_balance_ledger_id = ?,
                last_checked_at = current_timestamp
            where id = (select id from local_cloud_correction_checkpoint order by id limit 1)
            """,
            (int(last_minute_ledger_id), int(last_balance_ledger_id)),
        )

    def cloud_customer_directory_checkpoint(self):
        return self.fetch_one("select * from local_cloud_customer_directory_checkpoint order by id limit 1")

    def update_cloud_customer_directory_checkpoint(self, *, last_customer_updated_at: str):
        self.execute(
            """
            update local_cloud_customer_directory_checkpoint
            set last_customer_updated_at = ?,
                last_checked_at = current_timestamp
            where id = (select id from local_cloud_customer_directory_checkpoint order by id limit 1)
            """,
            (str(last_customer_updated_at or "").strip() or None,),
        )

    def find_customer_for_cloud_identity(
        self,
        *,
        customer_number: str = "",
        account_number: str = "",
        first_name: str = "",
        last_name: str = "",
    ):
        customer_number = str(customer_number or "").strip()
        account_number = str(account_number or "").strip()
        first_name = str(first_name or "").strip()
        last_name = str(last_name or "").strip()
        if customer_number:
            row = self.fetch_one("select * from customers where customer_number = ? order by id desc limit 1", (customer_number,))
            if row is not None:
                return row
            return None
        if account_number:
            row = self.fetch_one("select * from customers where account_number = ? order by id desc limit 1", (account_number,))
            if row is not None:
                return row
        # A supplied account identity must never fall through to a name match.
        # Different people can share a name, and stale aliases must not move credit.
        if customer_number or account_number:
            return None
        if first_name and last_name:
            return self.fetch_one(
                """
                select *
                from customers
                where first_name = ?
                  and last_name = ?
                order by id desc
                limit 1
                """,
                (first_name, last_name),
            )
        return None

    def apply_cloud_customer_corrections(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            return {"applied_count": 0, "customer_ids": []}
        minute_entries = payload.get("minute_entries")
        balance_entries = payload.get("balance_entries")
        applied_count = 0
        touched_customer_ids: set[int] = set()

        with self.connection() as conn:
            conn.execute("begin immediate")
            checkpoint_row = conn.execute(
                "select * from local_cloud_correction_checkpoint order by id limit 1"
            ).fetchone()
            last_minute_ledger_id = int((checkpoint_row["last_minute_ledger_id"] if checkpoint_row else 0) or 0)
            last_balance_ledger_id = int((checkpoint_row["last_balance_ledger_id"] if checkpoint_row else 0) or 0)
            if isinstance(minute_entries, list):
                for entry in minute_entries:
                    if not isinstance(entry, dict):
                        continue
                    entry_id = int(entry.get("id") or 0)
                    if entry_id <= last_minute_ledger_id:
                        continue
                    customer_row = self.find_customer_for_cloud_identity(
                        customer_number=str(entry.get("customer_number") or ""),
                        account_number=str(entry.get("account_number") or ""),
                        first_name=str(entry.get("customer_first_name") or ""),
                        last_name=str(entry.get("customer_last_name") or ""),
                    )
                    if customer_row is None:
                        # Keep the cursor before this entry so it retries after
                        # the customer directory arrives. Never skip their credit.
                        break
                    delta_minutes = int(entry.get("delta_minutes") or 0)
                    conn.execute(
                        "update customers set package_minutes = package_minutes + ? where id = ?",
                        (delta_minutes, int(customer_row["id"])),
                    )
                    applied_count += 1
                    touched_customer_ids.add(int(customer_row["id"]))
                    last_minute_ledger_id = max(last_minute_ledger_id, int(entry.get("id") or 0))
            if isinstance(balance_entries, list):
                for entry in balance_entries:
                    if not isinstance(entry, dict):
                        continue
                    entry_id = int(entry.get("id") or 0)
                    if entry_id <= last_balance_ledger_id:
                        continue
                    customer_row = self.find_customer_for_cloud_identity(
                        customer_number=str(entry.get("customer_number") or ""),
                        account_number=str(entry.get("account_number") or ""),
                        first_name=str(entry.get("customer_first_name") or ""),
                        last_name=str(entry.get("customer_last_name") or ""),
                    )
                    if customer_row is None:
                        # Keep the cursor before this entry so it retries after
                        # the customer directory arrives. Never skip their credit.
                        break
                    delta_amount = round(float(entry.get("delta_amount") or 0), 2)
                    conn.execute(
                        "update customers set account_balance = account_balance + ? where id = ?",
                        (delta_amount, int(customer_row["id"])),
                    )
                    applied_count += 1
                    touched_customer_ids.add(int(customer_row["id"]))
                    last_balance_ledger_id = max(last_balance_ledger_id, int(entry.get("id") or 0))
            conn.execute(
                """
                update local_cloud_correction_checkpoint
                set last_minute_ledger_id = ?,
                    last_balance_ledger_id = ?,
                    last_checked_at = current_timestamp
                where id = (select id from local_cloud_correction_checkpoint order by id limit 1)
                """,
                (last_minute_ledger_id, last_balance_ledger_id),
            )
        return {"applied_count": applied_count, "customer_ids": sorted(touched_customer_ids)}

    def apply_cloud_customer_directory(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            return {"imported_count": 0, "customer_ids": []}
        customer_entries = payload.get("customers")
        checkpoint_row = self.cloud_customer_directory_checkpoint()
        latest_updated_at = str((checkpoint_row["last_customer_updated_at"] if checkpoint_row else "") or "").strip()
        imported_count = 0
        touched_customer_ids: set[int] = set()

        with self.connection() as conn:
            if isinstance(customer_entries, list):
                for entry in customer_entries:
                    if not isinstance(entry, dict):
                        continue
                    customer_number = str(entry.get("customer_number") or "").strip()
                    account_number = str(entry.get("account_number") or "").strip()
                    first_name = str(entry.get("first_name") or "").strip()
                    last_name = str(entry.get("last_name") or "").strip()
                    phone = str(entry.get("phone") or "").strip()
                    email = str(entry.get("email") or "").strip()
                    status = str(entry.get("status") or "active").strip().lower()
                    minutes_available = int(entry.get("minutes_available") or 0)
                    current_balance = round(float(entry.get("current_balance") or 0), 2)
                    updated_at = str(entry.get("updated_at") or "").strip()

                    customer_row = self.find_customer_for_cloud_identity(
                        customer_number=customer_number,
                        account_number=account_number,
                        first_name=first_name,
                        last_name=last_name,
                    )
                    if customer_row is None:
                        customer_number_value = customer_number or self.next_customer_number()
                        customer_id = conn.execute(
                            """
                            insert into customers (
                                customer_number,
                                account_number,
                                first_name,
                                last_name,
                                phone,
                                email,
                                account_balance,
                                package_minutes,
                                is_active
                            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                customer_number_value,
                                account_number,
                                first_name or "Imported",
                                last_name or "Customer",
                                phone,
                                email,
                                current_balance,
                                minutes_available,
                                0 if status == "archived" else 1,
                            ),
                        ).lastrowid
                        if customer_id:
                            touched_customer_ids.add(int(customer_id))
                            imported_count += 1
                    else:
                        customer_id = int(customer_row["id"])
                        conn.execute(
                            """
                            update customers
                            set customer_number = ?,
                                account_number = ?,
                                first_name = ?,
                                last_name = ?,
                                phone = ?,
                                email = ?,
                                account_balance = ?,
                                package_minutes = ?,
                                is_active = ?
                            where id = ?
                            """,
                            (
                                customer_number or str(customer_row["customer_number"] or ""),
                                account_number or str(customer_row["account_number"] or ""),
                                first_name or str(customer_row["first_name"] or ""),
                                last_name or str(customer_row["last_name"] or ""),
                                phone or str(customer_row["phone"] or ""),
                                email or str(customer_row["email"] or ""),
                                current_balance,
                                minutes_available,
                                0 if status == "archived" else 1,
                                customer_id,
                            ),
                        )
                        touched_customer_ids.add(customer_id)
                        imported_count += 1

                    if updated_at and (not latest_updated_at or updated_at > latest_updated_at):
                        latest_updated_at = updated_at

            conn.execute(
                """
                update local_cloud_customer_directory_checkpoint
                set last_customer_updated_at = ?,
                    last_checked_at = current_timestamp
                where id = (select id from local_cloud_customer_directory_checkpoint order by id limit 1)
                """,
                (latest_updated_at or None,),
            )
        return {"imported_count": imported_count, "customer_ids": sorted(touched_customer_ids)}

    def local_device_identity(self):
        return self.fetch_one("select * from local_device_identity order by id desc limit 1")

    def save_local_device_identity(
        self,
        *,
        business_account_public_id: str | None,
        site_public_id: str | None,
        terminal_device_public_id: str | None,
        device_serial: str | None,
        hardware_fingerprint: str | None,
    ):
        existing = self.local_device_identity()
        params = (
            business_account_public_id,
            site_public_id,
            terminal_device_public_id,
            device_serial,
            hardware_fingerprint,
        )
        if existing is None:
            self.execute(
                """
                insert into local_device_identity (
                    business_account_public_id,
                    site_public_id,
                    terminal_device_public_id,
                    device_serial,
                    hardware_fingerprint
                ) values (?, ?, ?, ?, ?)
                """,
                params,
            )
            return
        self.execute(
            """
            update local_device_identity
            set business_account_public_id = ?,
                site_public_id = ?,
                terminal_device_public_id = ?,
                device_serial = ?,
                hardware_fingerprint = ?
            where id = ?
            """,
            params + (int(existing["id"]),),
        )

    def current_licence_lease(self):
        return self.fetch_one("select * from local_licence_lease order by id desc limit 1")

    def store_local_licence_lease(
        self,
        *,
        terminal_device_public_id: str,
        licence_status: str,
        signed_token: str,
        issued_at: str,
        expires_at: str,
        last_verified_at: str,
    ):
        existing = self.current_licence_lease()
        params = (
            terminal_device_public_id,
            licence_status,
            signed_token,
            issued_at,
            expires_at,
            last_verified_at,
        )
        if existing is None:
            self.execute(
                """
                insert into local_licence_lease (
                    terminal_device_public_id,
                    licence_status,
                    signed_token,
                    issued_at,
                    expires_at,
                    last_verified_at
                ) values (?, ?, ?, ?, ?, ?)
                """,
                params,
            )
            return
        self.execute(
            """
            update local_licence_lease
            set terminal_device_public_id = ?,
                licence_status = ?,
                signed_token = ?,
                issued_at = ?,
                expires_at = ?,
                last_verified_at = ?
            where id = ?
            """,
            params + (int(existing["id"]),),
        )

    def email_report_settings(self):
        defaults = {
            "management_report_emails": "",
            "report_from_email": "",
            "smtp_host": "",
            "smtp_port": 587,
            "smtp_username": "",
            "smtp_password": "",
            "smtp_use_tls": 1,
            "email_reports_enabled": 0,
            "auto_email_shift_reports": 1,
        }
        if not self.table_exists("business_settings"):
            return defaults
        row = self.fetch_one("select * from business_settings where id = 1")
        if row is None:
            return defaults
        settings = defaults.copy()
        for key in settings:
            if key in row.keys():
                settings[key] = row[key]
        return settings

    def sync_settings(self):
        defaults = {
            "sync_api_base_url": "https://api.salonmax.co.uk/v1",
            "sync_stub_enabled": 1,
            "sync_poll_seconds": 15,
            "salonmax_business_account_public_id": "",
            "salonmax_site_public_id": "",
        }
        if not self.table_exists("business_settings"):
            return defaults
        row = self.fetch_one("select * from business_settings where id = 1")
        if row is None:
            return defaults
        settings = defaults.copy()
        for key in settings:
            if key in row.keys():
                settings[key] = row[key]
        return settings

    def update_sync_settings(
        self,
        *,
        business_account_public_id: str | None = None,
        site_public_id: str | None = None,
        sync_api_base_url: str | None = None,
    ):
        if not self.table_exists("business_settings"):
            return
        updates: list[str] = []
        params: list[object] = []
        if business_account_public_id is not None:
            updates.append("salonmax_business_account_public_id = ?")
            params.append(str(business_account_public_id))
        if site_public_id is not None:
            updates.append("salonmax_site_public_id = ?")
            params.append(str(site_public_id))
        if sync_api_base_url is not None:
            updates.append("sync_api_base_url = ?")
            params.append(str(sync_api_base_url))
        if not updates:
            return
        with self.connection() as conn:
            conn.execute(f"update business_settings set {', '.join(updates)} where id = 1", tuple(params))

    def _remote_setup_bed_rows(self, value: str) -> list[tuple[int, str]]:
        rows: list[tuple[int, str]] = []
        seen: set[int] = set()
        for part in re.split(r"[\n;,]+", str(value or "")):
            text = part.strip()
            if not text:
                continue
            match = re.search(r"\bbed\s*(\d+)\b\s*[:=\-| ]*\s*(.+)", text, flags=re.IGNORECASE)
            if not match:
                continue
            bed_number = int(match.group(1))
            bed_name = re.sub(r"\bphotos?\s+required\b", "", match.group(2), flags=re.IGNORECASE).strip(" -:|")
            if bed_number <= 0 or not bed_name or bed_number in seen:
                continue
            rows.append((bed_number, bed_name))
            seen.add(bed_number)
        return rows

    def _remote_setup_price_per_minute(self, value: str) -> float | None:
        for part in re.split(r"[\n;,]+", str(value or "")):
            text = part.strip().lower()
            if not text or "happy" in text:
                continue
            pound_match = re.search(r"(?:gbp|\u00a3)\s*(\d+(?:\.\d+)?)\s*(?:per|/)?\s*min", text)
            if pound_match:
                return round(float(pound_match.group(1)), 2)
            pence_match = re.search(r"(\d+(?:\.\d+)?)\s*p\s*(?:per|/)?\s*min", text)
            if pence_match:
                return round(float(pence_match.group(1)) / 100, 2)
            decimal_match = re.search(r"(\d+\.\d+)\s*(?:per|/)?\s*min", text)
            if decimal_match:
                return round(float(decimal_match.group(1)), 2)
        return None

    def _remote_setup_staff_rows(self, value: str) -> list[tuple[str, str, str]]:
        rows: list[tuple[str, str, str]] = []
        seen_pins: set[str] = set()
        for part in re.split(r"[\n;,]+", str(value or "")):
            text = part.strip()
            if not text:
                continue
            pin_match = re.search(r"\b(\d{4,8})\b", text)
            if not pin_match:
                continue
            pin = pin_match.group(1)
            if pin in seen_pins:
                continue
            lower_text = text.lower()
            if "owner" in lower_text:
                role = "owner"
                fallback_name = "Owner"
            elif "manager" in lower_text:
                role = "manager"
                fallback_name = "Manager"
            else:
                role = "staff"
                fallback_name = "Staff"
            name_text = text[: pin_match.start()].strip() or text[pin_match.end() :].strip()
            name_text = re.sub(r"\b(pin|owner|manager|staff)\b", "", name_text, flags=re.IGNORECASE)
            name = re.sub(r"\s+", " ", name_text).strip(" -:|") or fallback_name
            rows.append((name, pin, role))
            seen_pins.add(pin)
        return rows

    def apply_cloud_device_config(self, config_payload: dict):
        if not isinstance(config_payload, dict):
            return

        business = config_payload.get("business") or {}
        site = config_payload.get("site") or {}
        terminal = config_payload.get("terminal") or {}
        beds = config_payload.get("beds") or []
        pricing = config_payload.get("pricing") or []
        setup_profile = config_payload.get("setup_profile") or {}
        if not isinstance(business, dict):
            business = {}
        if not isinstance(site, dict):
            site = {}
        if not isinstance(terminal, dict):
            terminal = {}
        if not isinstance(setup_profile, dict):
            setup_profile = {}

        business_name = str(
            business.get("business_name")
            or config_payload.get("business_name")
            or setup_profile.get("business_name")
            or ""
        ).strip()
        currency_code = str(business.get("currency_code") or "").strip()
        prep_minutes = business.get("prep_minutes")
        cooldown_minutes = business.get("cooldown_minutes")

        with self.connection() as conn:
            if self.table_exists("business_settings"):
                update_parts = []
                params: list[object] = []
                if business_name:
                    update_parts.append("business_name = ?")
                    params.append(business_name)
                if currency_code:
                    update_parts.append("currency_symbol = ?")
                    params.append(currency_code)
                if isinstance(business.get("receipt_footer_text"), str):
                    pass
                if update_parts:
                    conn.execute(f"update business_settings set {', '.join(update_parts)} where id = 1", tuple(params))

            site_name = str(site.get("site_name") or config_payload.get("site_name") or "").strip()
            terminal_name = str(terminal.get("terminal_name") or config_payload.get("terminal_name") or "").strip()
            default_terminal_row = conn.execute(
                """
                select id, site_id
                from terminals
                order by id
                limit 1
                """
            ).fetchone()
            if site_name:
                if default_terminal_row is not None:
                    conn.execute(
                        "update sites set name = ? where id = ?",
                        (site_name, int(default_terminal_row["site_id"])),
                    )
            if terminal_name:
                if default_terminal_row is not None:
                    conn.execute(
                        "update terminals set name = ? where id = ?",
                        (terminal_name, int(default_terminal_row["id"])),
                    )

            for bed in beds:
                device_public_id = str(bed.get("device_id") or "").strip()
                match = re.fullmatch(r"bed_(\d+)", device_public_id)
                if not match:
                    continue
                bed_number = int(match.group(1))
                device_name = str(bed.get("device_name") or "").strip()
                if device_name:
                    conn.execute(
                        "update devices set device_name = ? where device_number = ?",
                        (device_name, bed_number),
                    )
                if isinstance(bed.get("prep_minutes"), int):
                    prep_minutes = bed.get("prep_minutes")
                if isinstance(bed.get("cooldown_minutes"), int):
                    cooldown_minutes = bed.get("cooldown_minutes")

            if self.table_exists("business_settings"):
                update_parts = []
                params = []
                if isinstance(prep_minutes, int):
                    update_parts.append("default_prep_minutes = ?")
                    params.append(int(prep_minutes))
                if isinstance(cooldown_minutes, int):
                    update_parts.append("default_cooldown_minutes = ?")
                    params.append(int(cooldown_minutes))
                if update_parts:
                    conn.execute(f"update business_settings set {', '.join(update_parts)} where id = 1", tuple(params))

            for price in pricing:
                device_public_id = str(price.get("device_id") or "").strip()
                match = re.fullmatch(r"bed_(\d+)", device_public_id)
                if not match:
                    continue
                bed_number = int(match.group(1))
                try:
                    price_per_minute = float(price.get("price_per_minute"))
                except (TypeError, ValueError):
                    continue
                device_row = conn.execute(
                    "select id, site_id from devices where device_number = ? order by id limit 1",
                    (bed_number,),
                ).fetchone()
                if device_row is None:
                    continue
                existing_rule = conn.execute(
                    """
                    select id
                    from pricing_rules
                    where site_id = ?
                      and device_id = ?
                    order by id
                    limit 1
                    """,
                    (int(device_row["site_id"]), int(device_row["id"])),
                ).fetchone()
                if existing_rule is None:
                    conn.execute(
                        """
                        insert into pricing_rules (site_id, device_id, price_per_minute, is_active)
                        values (?, ?, ?, 1)
                        """,
                        (int(device_row["site_id"]), int(device_row["id"]), price_per_minute),
                    )
                else:
                    conn.execute(
                        "update pricing_rules set price_per_minute = ?, is_active = 1 where id = ?",
                        (price_per_minute, int(existing_rule["id"])),
                    )

            if not isinstance(setup_profile, dict):
                return
            setup_status = str(setup_profile.get("status") or "").strip().lower()
            if setup_status not in {"ready_to_apply", "ready"}:
                return
            try:
                setup_version = int(setup_profile.get("version") or 0)
            except (TypeError, ValueError):
                setup_version = 0
            if setup_version <= 0 or not self.table_exists("business_settings"):
                return
            settings_row = conn.execute(
                "select salonmax_remote_setup_version_applied from business_settings where id = 1"
            ).fetchone()
            applied_version = int(settings_row["salonmax_remote_setup_version_applied"] or 0) if settings_row else 0
            if setup_version <= applied_version:
                return

            profile_business_name = str(
                setup_profile.get("salon_display_name")
                or setup_profile.get("backoffice_display_name")
                or setup_profile.get("business_name")
                or ""
            ).strip()
            if profile_business_name:
                conn.execute("update business_settings set business_name = ? where id = 1", (profile_business_name,))

            profile_site_name = str(setup_profile.get("site_name") or "").strip()
            default_site_id: int | None = None
            if default_terminal_row is not None:
                default_site_id = int(default_terminal_row["site_id"])
            else:
                site_row = conn.execute("select id from sites where is_active = 1 order by id limit 1").fetchone()
                if site_row is not None:
                    default_site_id = int(site_row["id"])
            if default_site_id is None:
                conn.execute("insert into sites (name, code, is_active) values (?, ?, 1)", ("Main Site", "main-site"))
                default_site_id = int(conn.execute("select last_insert_rowid() as id").fetchone()["id"])
            if profile_site_name:
                conn.execute("update sites set name = ? where id = ?", (profile_site_name, default_site_id))

            for bed_number, bed_name in self._remote_setup_bed_rows(str(setup_profile.get("sunbed_setup") or "")):
                device_row = conn.execute(
                    "select id from devices where site_id = ? and device_number = ? order by id limit 1",
                    (default_site_id, bed_number),
                ).fetchone()
                if device_row is None:
                    conn.execute(
                        "insert into devices (site_id, device_number, device_name, is_active) values (?, ?, ?, 1)",
                        (default_site_id, bed_number, bed_name),
                    )
                else:
                    conn.execute(
                        "update devices set device_name = ?, is_active = 1 where id = ?",
                        (bed_name, int(device_row["id"])),
                    )

            setup_price = self._remote_setup_price_per_minute(str(setup_profile.get("pricing_setup") or ""))
            if setup_price is not None:
                default_rule = conn.execute(
                    """
                    select id
                    from pricing_rules
                    where site_id = ?
                      and device_id is null
                    order by id
                    limit 1
                    """,
                    (default_site_id,),
                ).fetchone()
                if default_rule is None:
                    conn.execute(
                        "insert into pricing_rules (site_id, device_id, price_per_minute, is_active) values (?, null, ?, 1)",
                        (default_site_id, setup_price),
                    )
                else:
                    conn.execute(
                        "update pricing_rules set price_per_minute = ?, is_active = 1 where id = ?",
                        (setup_price, int(default_rule["id"])),
                    )

            if self.table_exists("staff_users"):
                active_staff_row = conn.execute("select count(*) as value from staff_users where is_active = 1").fetchone()
                active_staff = int(active_staff_row["value"] or 0) if active_staff_row else 0
                if active_staff == 0:
                    for name, pin, role in self._remote_setup_staff_rows(str(setup_profile.get("staff_setup") or "")):
                        conn.execute(
                            "insert into staff_users (name, pin_code, role, is_active) values (?, ?, ?, 1)",
                            (name, pin, role),
                        )

            applied_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                """
                update business_settings
                set salonmax_remote_setup_version_applied = ?,
                    salonmax_remote_setup_applied_at = ?
                where id = 1
                """,
                (setup_version, applied_at),
            )

    def recipient_list(self, value: str) -> list[str]:
        return [part.strip() for part in str(value or "").replace(";", ",").split(",") if part.strip()]

    def send_management_email(self, subject: str, body: str):
        settings = self.email_report_settings()
        if int(settings.get("email_reports_enabled") or 0) != 1:
            return False, "Email reports are disabled."

        recipients = self.recipient_list(settings.get("management_report_emails", ""))
        smtp_host = str(settings.get("smtp_host") or "").strip()
        from_email = str(settings.get("report_from_email") or settings.get("smtp_username") or "").strip()

        if not recipients:
            return False, "No management email recipients are configured."
        if not smtp_host:
            return False, "SMTP host is not configured."
        if not from_email:
            return False, "From email is not configured."

        port = int(settings.get("smtp_port") or 587)
        username = str(settings.get("smtp_username") or "").strip()
        password = str(settings.get("smtp_password") or "")
        use_tls = int(settings.get("smtp_use_tls") or 0) == 1

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = from_email
        message["To"] = ", ".join(recipients)
        message.set_content(body)

        try:
            with smtplib.SMTP(smtp_host, port, timeout=20) as server:
                if use_tls:
                    server.starttls()
                if username:
                    server.login(username, password)
                server.send_message(message)
        except Exception as exc:
            return False, str(exc)
        return True, f"Email sent to {', '.join(recipients)}"

    def next_transaction_number(self) -> str:
        row = self.fetch_one("select transaction_number from transactions order by id desc limit 1")
        if row is None:
            return "TX-1001"
        current = row["transaction_number"].split("-")[-1]
        return f"TX-{int(current) + 1}"

    def active_staff(self):
        return self.fetch_all("select * from staff_users where is_active = 1 order by name")

    def active_staff_count(self) -> int:
        row = self.fetch_one("select count(*) as value from staff_users where is_active = 1")
        return int(row["value"] or 0) if row is not None else 0

    def deactivate_all_staff_users(self):
        self.execute("update staff_users set is_active = 0")

    def create_staff_user(self, *, name: str, pin_code: str, role: str = "owner"):
        self.execute(
            """
            insert into staff_users (name, pin_code, role, is_active)
            values (?, ?, ?, 1)
            """,
            (str(name).strip(), str(pin_code).strip(), str(role).strip() or "owner"),
        )

    def reset_for_fresh_salon_install(
        self,
        *,
        business_name: str,
        site_name: str,
        terminal_name: str,
    ):
        reset_tables = [
            "transaction_lines",
            "transactions",
            "till_sessions",
            "customers",
            "customer_packages",
            "staff_users",
            "staff_shifts",
            "shift_patterns",
            "stock_adjustments",
            "retail_products",
            "product_groups",
            "package_products",
            "sunbed_usage_log",
            "sunbed_retube_history",
            "sunbeds",
            "local_sync_outbox",
            "local_device_identity",
            "local_licence_lease",
        ]

        with self.connection() as conn:
            existing_tables = {
                row["name"]
                for row in conn.execute("select name from sqlite_master where type = 'table'").fetchall()
            }

            for table_name in reset_tables:
                if table_name in existing_tables:
                    conn.execute(f"delete from {table_name}")

            if "sqlite_sequence" in existing_tables:
                for table_name in reset_tables:
                    conn.execute("delete from sqlite_sequence where name = ?", (table_name,))

            if "local_sync_checkpoint" in existing_tables:
                conn.execute("update local_sync_checkpoint set last_cloud_event_id = 0, last_synced_at = null")

            if "business_settings" in existing_tables:
                conn.execute(
                    """
                    update business_settings
                    set business_name = ?,
                        currency_symbol = 'GBP',
                        default_package_validity_days = 365,
                        default_prep_minutes = 3,
                        default_cooldown_minutes = 3,
                        till_logo_path = '',
                        till_theme_preset = 'ultra_violet',
                        till_primary_color = '#155e75',
                        till_button_color = '#1976d2',
                        till_background_color = '#111111',
                        till_text_color = '#ffffff',
                        peak_price_per_minute = 0.65,
                        happy_hour_price_per_minute = 0.55,
                        happy_hour_1_start = '10:00',
                        happy_hour_1_end = '11:00',
                        happy_hour_2_start = '20:00',
                        happy_hour_2_end = '21:00',
                        management_report_emails = '',
                        report_from_email = '',
                        smtp_host = '',
                        smtp_port = 587,
                        smtp_username = '',
                        smtp_password = '',
                        smtp_use_tls = 1,
                        email_reports_enabled = 0,
                        auto_email_shift_reports = 1,
                        auto_email_daily_reports = 0
                    where id = 1
                    """,
                    (str(business_name).strip() or "Your Salon",),
                )

            active_site_id = None
            if "sites" in existing_tables:
                first_site = conn.execute("select id from sites order by id limit 1").fetchone()
                site_code = re.sub(r"[^A-Za-z0-9]+", "", str(site_name or "").upper())[:8] or "MAIN"
                if first_site is None:
                    cursor = conn.execute(
                        "insert into sites (name, code, is_active) values (?, ?, 1)",
                        (str(site_name).strip() or "Main Site", site_code),
                    )
                    active_site_id = int(cursor.lastrowid)
                else:
                    active_site_id = int(first_site["id"])
                    conn.execute("update sites set is_active = 0")
                    conn.execute(
                        "update sites set name = ?, code = ?, is_active = 1 where id = ?",
                        (str(site_name).strip() or "Main Site", site_code, active_site_id),
                    )

            active_terminal_id = None
            if "terminals" in existing_tables and active_site_id is not None:
                first_terminal = conn.execute("select id from terminals order by id limit 1").fetchone()
                if first_terminal is None:
                    cursor = conn.execute(
                        "insert into terminals (site_id, name, is_active) values (?, ?, 1)",
                        (active_site_id, str(terminal_name).strip() or "Till 1"),
                    )
                    active_terminal_id = int(cursor.lastrowid)
                else:
                    active_terminal_id = int(first_terminal["id"])
                    conn.execute("update terminals set is_active = 0")
                    conn.execute(
                        "update terminals set site_id = ?, name = ?, is_active = 1 where id = ?",
                        (active_site_id, str(terminal_name).strip() or "Till 1", active_terminal_id),
                    )

            if "devices" in existing_tables and active_site_id is not None:
                device_rows = conn.execute(
                    "select id, device_number from devices order by device_number, id"
                ).fetchall()
                seen_numbers: set[int] = set()
                for row in device_rows:
                    bed_number = int(row["device_number"] or 0)
                    if bed_number <= 0:
                        conn.execute("update devices set is_active = 0 where id = ?", (int(row["id"]),))
                        continue
                    if bed_number in seen_numbers:
                        conn.execute("update devices set is_active = 0 where id = ?", (int(row["id"]),))
                        continue
                    seen_numbers.add(bed_number)
                    conn.execute(
                        "update devices set site_id = ?, device_name = ?, is_active = 1 where id = ?",
                        (active_site_id, f"Bed {bed_number}", int(row["id"])),
                    )
                if not seen_numbers:
                    for bed_number in range(1, 9):
                        conn.execute(
                            "insert into devices (site_id, device_number, device_name, is_active) values (?, ?, ?, 1)",
                            (active_site_id, bed_number, f"Bed {bed_number}"),
                        )

            if "pricing_rules" in existing_tables and active_site_id is not None:
                conn.execute("delete from pricing_rules")
                if "sqlite_sequence" in existing_tables:
                    conn.execute("delete from sqlite_sequence where name = 'pricing_rules'")
                conn.execute(
                    "insert into pricing_rules (site_id, device_id, price_per_minute, is_active) values (?, null, 0.55, 1)",
                    (active_site_id,),
                )

    def authenticate_staff(self, pin_code: str):
        return self.fetch_one(
            "select * from staff_users where pin_code = ? and is_active = 1",
            (pin_code.strip(),),
        )

    def active_sites(self):
        return self.fetch_all("select * from sites where is_active = 1 order by name")

    def active_terminals(self, site_id: int | None = None):
        if site_id:
            return self.fetch_all(
                "select * from terminals where is_active = 1 and site_id = ? order by name",
                (site_id,),
            )
        return self.fetch_all("select * from terminals where is_active = 1 order by name")

    def active_devices(self, site_id: int | None = None):
        if site_id:
            return self.fetch_all(
                "select * from devices where is_active = 1 and site_id = ? order by device_name",
                (site_id,),
            )
        return self.fetch_all("select * from devices where is_active = 1 order by device_name")

    def default_site_and_terminal(self):
        site_row = self.fetch_one("select * from sites where is_active = 1 order by name limit 1")
        if site_row is None:
            return None, None
        terminal_row = self.fetch_one(
            "select * from terminals where is_active = 1 and site_id = ? order by name limit 1",
            (int(site_row["id"]),),
        )
        return site_row, terminal_row

    def table_exists(self, table_name: str) -> bool:
        row = self.fetch_one(
            "select name from sqlite_master where type = 'table' and name = ?",
            (table_name,),
        )
        return row is not None

    def normalise_birthday_input(self, value: str) -> str:
        text = value.strip()
        if not text:
            return ""
        for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
        return text

    def birthday_for_display(self, value: str | None) -> str:
        if not value:
            return ""
        try:
            return datetime.strptime(value, "%Y-%m-%d").strftime("%d/%m/%Y")
        except ValueError:
            return str(value)

    def timestamp_for_display(self, value: str | None, *, include_seconds: bool = False) -> str:
        if not value:
            return ""
        text = str(value).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                utc_dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
                local_dt = utc_dt.astimezone()
                output_fmt = "%d/%m/%Y %H:%M:%S" if include_seconds else "%d/%m/%Y %H:%M"
                return local_dt.strftime(output_fmt)
            except ValueError:
                continue
        return text

    def search_customers(self, query_text: str, birthday_text: str = ""):
        query_text = query_text.strip()
        birthday_text = self.normalise_birthday_input(birthday_text)
        if not query_text and not birthday_text:
            return self.fetch_all("select * from customers where is_active=1 order by last_name, first_name limit 30")

        like = f"%{query_text}%"
        clauses = []
        params = []

        if birthday_text:
            clauses.append("birthday = ?")
            params.append(birthday_text)

        if query_text:
            clauses.append(
                """
                (
                    customer_number like ?
                    or account_number like ?
                    or first_name like ?
                    or last_name like ?
                )
                """
            )
            params.extend((like, like, like, like))

        where_sql = " and ".join(clauses) if clauses else "1=1"
        return self.fetch_all(
            f"""
            select *
            from customers
            where is_active=1 and ({where_sql})
            order by
                case when birthday = ? then 0 else 1 end,
                last_name,
                first_name
            """,
            (*params, birthday_text),
        )

    def get_customer(self, customer_id: int):
        return self.fetch_one("select * from customers where id = ?", (customer_id,))

    def customer_sync_payload(self, customer_id: int) -> dict:
        customer = self.get_customer(customer_id)
        if customer is None:
            return {}
        return {
            "customer_number": str(customer["customer_number"] or ""),
            "account_number": str(customer["account_number"] or ""),
            "customer_first_name": str(customer["first_name"] or ""),
            "customer_last_name": str(customer["last_name"] or ""),
            "customer_phone": str(customer["phone"] or ""),
            "customer_email": str(customer["email"] or ""),
        }

    def branding_settings(self):
        project_root = Path(os.environ.get("SALONMAX_SHARED_ROOT") or Path(__file__).resolve().parent.parent)
        assets_dir = project_root / "native_till_app" / "assets"
        full_logo = assets_dir / "salon_max_logo.png"
        defaults = {
            "business_name": "Salon Max",
            "logo_path": str(full_logo) if full_logo.exists() else "",
        }
        if not self.table_exists("business_settings"):
            return defaults
        row = self.fetch_one("select * from business_settings where id = 1")
        if row is None:
            return defaults
        configured_logo = str(row["till_logo_path"] or "").strip()
        resolved_logo = ""
        if configured_logo:
            configured_path = Path(configured_logo.replace("\\", "/"))
            candidates = []
            if configured_path.is_absolute():
                candidates.append(configured_path)
            else:
                candidates.append(project_root / configured_path)
            if "/static/uploads/" in configured_logo:
                upload_name = configured_logo.split("/static/uploads/", 1)[1].lstrip("/")
                candidates.append(project_root / "static" / "uploads" / upload_name)
            else:
                candidates.append(project_root / "static" / "uploads" / configured_path.name)

            for candidate in candidates:
                if candidate.exists():
                    resolved_logo = str(candidate)
                    break

        logo_path = resolved_logo or defaults["logo_path"]
        return {
            "business_name": row["business_name"] or defaults["business_name"],
            "logo_path": logo_path,
        }

    def customer_display_settings(self):
        defaults = {
            "business_name": "Your Salon",
        }
        if not self.table_exists("business_settings"):
            return defaults
        row = self.fetch_one("select * from business_settings where id = 1")
        if row is None:
            return defaults
        return {
            "business_name": row["business_name"] or defaults["business_name"],
        }

    def receipt_settings(self):
        defaults = {
            "business_name": "Your Salon",
            "currency_symbol": "GBP",
        }
        if not self.table_exists("business_settings"):
            return defaults
        row = self.fetch_one("select * from business_settings where id = 1")
        if row is None:
            return defaults
        return {
            "business_name": row["business_name"] or defaults["business_name"],
            "currency_symbol": row["currency_symbol"] or defaults["currency_symbol"],
        }

    def bed_timing_settings(self):
        defaults = {
            "prep_minutes": 3,
            "cooldown_minutes": 3,
        }
        if not self.table_exists("business_settings"):
            return defaults

        row = self.fetch_one("select * from business_settings where id = 1")
        if row is None:
            return defaults

        settings = defaults.copy()
        try:
            settings["prep_minutes"] = int(row["default_prep_minutes"] or defaults["prep_minutes"])
        except (TypeError, ValueError, KeyError, IndexError):
            pass
        try:
            settings["cooldown_minutes"] = int(
                row["default_cooldown_minutes"] or defaults["cooldown_minutes"]
            )
        except (TypeError, ValueError, KeyError, IndexError):
            pass
        return settings

    def next_customer_number(self) -> str:
        row = self.fetch_one(
            "select customer_number from customers order by cast(customer_number as integer) desc limit 1"
        )
        if row is None:
            return "1001"
        try:
            return str(int(row["customer_number"]) + 1)
        except (TypeError, ValueError):
            return "1001"

    def create_customer(
        self,
        first_name: str,
        last_name: str,
        birthday: str = "",
        account_number: str = "",
        phone: str = "",
        email: str = "",
    ) -> int:
        with self.connection() as conn:
            conn.execute("begin immediate")
            number = conn.execute("""select cast(max(value) + 1 as text) from (
            select 1000 as value
            union all select cast(customer_number as integer) from customers
                where customer_number <> '' and customer_number not glob '*[^0-9]*'
            union all select cast(account_number as integer) from customers
                where account_number <> '' and account_number not glob '*[^0-9]*'
        )""").fetchone()[0]
            customer_id = conn.execute(
                """insert into customers (customer_number,account_number,first_name,last_name,birthday,phone,email)
                   values (?,?,?,?,?,?,?)""",
                (number, number, first_name.strip(), last_name.strip(),
                 self.normalise_birthday_input(birthday), phone.strip(), email.strip()),
            ).lastrowid
        return int(customer_id)

    def update_customer(
        self,
        customer_id: int,
        first_name: str,
        last_name: str,
        birthday: str = "",
        account_number: str = "",
        phone: str = "",
        email: str = "",
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                update customers
                set first_name = ?,
                    last_name = ?,
                    birthday = ?,
                    account_number = ?,
                    phone = ?,
                    email = ?
                where id = ?
                """,
                (
                    first_name.strip(),
                    last_name.strip(),
                    self.normalise_birthday_input(birthday),
                    account_number.strip(),
                    phone.strip(),
                    email.strip(),
                    int(customer_id),
                ),
            )

    def recent_transactions(self, customer_id: int, limit: int = 20):
        return self.fetch_all(
            """
            select transaction_number, transaction_type, total_amount, payment_method, status, notes, created_at
            from transactions
            where customer_id = ?
            order by id desc
            limit ?
            """,
            (customer_id, limit),
        )

    def transaction_receipt_data(self, transaction_id: int):
        transaction = self.fetch_one(
            """
            select
                transactions.*,
                customers.first_name,
                customers.last_name,
                sites.name as site_name,
                terminals.name as terminal_name
            from transactions
            left join customers on customers.id = transactions.customer_id
            left join sites on sites.id = transactions.site_id
            left join terminals on terminals.id = transactions.terminal_id
            where transactions.id = ?
            """,
            (transaction_id,),
        )
        if transaction is None:
            return None
        lines = self.fetch_all(
            """
            select *
            from transaction_lines
            where transaction_id = ?
            order by id
            """,
            (transaction_id,),
        )
        return {
            "transaction": transaction,
            "lines": lines,
            "settings": self.receipt_settings(),
        }

    def retail_product_count(self) -> int:
        row = self.fetch_one(
            "select count(*) as value from retail_products where is_active = 1 and stock_quantity > 0"
        )
        return int(row["value"] or 0) if row else 0

    def active_retail_products(self, search_text: str = ""):
        text = (search_text or "").strip()
        if not self.table_exists("retail_products"):
            return []

        if not text:
            return self.fetch_all(
                """
                select retail_products.*, coalesce(product_groups.name, '') as group_name
                from retail_products
                left join product_groups on product_groups.id = retail_products.group_id
                where retail_products.is_active = 1
                  and retail_products.stock_quantity > 0
                order by coalesce(product_groups.sort_order, 9999), group_name, retail_products.name
                """
            )

        like = f"%{text}%"
        return self.fetch_all(
            """
            select retail_products.*, coalesce(product_groups.name, '') as group_name
            from retail_products
            left join product_groups on product_groups.id = retail_products.group_id
            where retail_products.is_active = 1
              and retail_products.stock_quantity > 0
              and (
                retail_products.name like ?
                or retail_products.sku like ?
                or coalesce(product_groups.name, '') like ?
              )
            order by coalesce(product_groups.sort_order, 9999), group_name, retail_products.name
            """,
            (like, like, like),
        )

    def active_package_products(self):
        if not self.table_exists("package_products"):
            return []
        return self.fetch_all(
            """
            select *
            from package_products
            where is_active = 1
            order by minutes_included, name
            """
        )

    def current_site_devices(self, till_session: TillSessionContext | None):
        if till_session is None:
            return self.active_devices()
        return self.active_devices(till_session.site_id)

    def sunbed_status_rows(self, till_session: TillSessionContext | None):
        if self.table_exists("sunbeds"):
            rows = self.fetch_all(
                """
                select
                    sunbeds.id,
                    sunbeds.site_id,
                    room_number as bed_number,
                    case
                        when trim(coalesce(display_name, '')) = ''
                            or trim(display_name) = 'Bed ' || room_number
                            or trim(display_name) = 'Room ' || room_number || ' - Bed ' || room_number
                            or trim(display_name) = 'Room ' || room_number
                        then ''
                        else trim(display_name)
                    end as custom_name,
                    'Bed ' || room_number as base_label,
                    case
                        when trim(coalesce(display_name, '')) = ''
                            or trim(display_name) = 'Bed ' || room_number
                            or trim(display_name) = 'Room ' || room_number || ' - Bed ' || room_number
                            or trim(display_name) = 'Room ' || room_number
                        then 'Bed ' || room_number
                        else trim(display_name)
                    end as display_name,
                    coalesce(bed_type, 'lay_down') as bed_type,
                    coalesce(customer_display_image_path, '') as image_path,
                    case when sunbeds.is_active = 1 then 'Ready' else 'Inactive' end as status_text,
                    '00:00' as wait_text,
                    coalesce(sites.is_active, 0) as site_is_active,
                    sunbeds.updated_at
                from sunbeds
                left join sites on sites.id = sunbeds.site_id
                order by
                    room_number,
                    case when coalesce(sites.is_active, 0) = 1 then 0 else 1 end,
                    case when sunbeds.is_active = 1 then 0 else 1 end,
                    case when sunbeds.site_id = ? then 0 else 1 end,
                    datetime(coalesce(sunbeds.updated_at, '1970-01-01 00:00:00')) desc,
                    sunbeds.id desc
                """,
                (till_session.site_id if till_session else -1,),
            )
            if rows:
                prepared = []
                seen_beds = set()
                for row in rows:
                    row_dict = dict(row)
                    bed_number = int(row_dict.get("bed_number") or 1)
                    if bed_number in seen_beds:
                        continue
                    seen_beds.add(bed_number)
                    image_path = str(row_dict.get("image_path") or "").strip()
                    prepared.append(row_dict)
                return prepared

        device_rows = self.current_site_devices(till_session)
        return [
            {
                "id": row["id"],
                "bed_number": row["device_number"],
                "custom_name": row["device_name"],
                "base_label": f"Bed {row['device_number']}",
                "display_name": row["device_name"],
                "bed_type": "sunbed",
                "image_path": "",
                "status_text": "Ready",
                "wait_text": "00:00",
            }
            for row in device_rows
        ]

    def till_site_name(self, till_session: TillSessionContext | None) -> str:
        if till_session is None:
            return "Not set"
        row = self.fetch_one("select * from sites where id = ?", (till_session.site_id,))
        return row["name"] if row else f"Site {till_session.site_id}"

    def till_terminal_name(self, till_session: TillSessionContext | None) -> str:
        if till_session is None:
            return "Not set"
        row = self.fetch_one("select * from terminals where id = ?", (till_session.terminal_id,))
        return row["name"] if row else f"Terminal {till_session.terminal_id}"

    def opening_float_default(self, site_id: int | None, terminal_id: int | None) -> float:
        return 0.0

    def open_till(self, site_id: int, terminal_id: int, staff_user_id: int, opening_float: float):
        # Sign-out leaves the cash session open. Resume it rather than creating
        # another cash drawer each time a member of staff signs back in.
        with self.connection() as conn:
            conn.execute("begin immediate")
            row = conn.execute(
                "select * from till_sessions where terminal_id=? and status='open' order by id desc limit 1",
                (terminal_id,),
            ).fetchone()
            if row is not None and int(row["site_id"]) != int(site_id):
                raise ValueError("The terminal has an open session at another site; close it before moving the terminal.")
            if row is None:
                session_id = conn.execute(
                    """
                    insert into till_sessions (
                        site_id, terminal_id, opened_by_user_id, opening_float,
                        expected_cash, counted_cash, variance, status, closing_notes
                    ) values (?, ?, ?, ?, ?, 0, 0, 'open', '')
                    """,
                    (site_id, terminal_id, staff_user_id, opening_float, opening_float),
                ).lastrowid
                row = conn.execute("select * from till_sessions where id=?", (session_id,)).fetchone()
        return TillSessionContext(
            id=int(row["id"]),
            site_id=int(row["site_id"]),
            terminal_id=int(row["terminal_id"]),
            opened_by_user_id=int(row["opened_by_user_id"]),
            opening_float=float(row["opening_float"] or 0),
            expected_cash=float(row["expected_cash"] or 0),
            status=row["status"],
        )

    def price_per_minute_for(self, site_id: int, device_id: int):
        row = self.fetch_one(
            """
            select price_per_minute
            from pricing_rules
            where is_active = 1
              and site_id = ?
              and device_id = ?
            limit 1
            """,
            (site_id, device_id),
        )
        if row is not None:
            return float(row["price_per_minute"])

        row = self.fetch_one(
            """
            select price_per_minute
            from pricing_rules
            where is_active = 1
              and site_id = ?
              and device_id is null
            limit 1
            """,
            (site_id,),
        )
        if row is not None:
            return float(row["price_per_minute"])
        return 0.55

    def quote_tanning_sale(
        self,
        *,
        customer_id: int,
        site_id: int,
        device_id: int,
        minutes: int,
        payment_method: str,
    ):
        customer = self.get_customer(customer_id)
        if customer is None:
            raise ValueError("Customer not found.")

        if minutes <= 0:
            raise ValueError("Minutes must be greater than zero.")

        available_minutes = int(customer["package_minutes"] or 0)
        price_per_minute = self.price_per_minute_for(site_id, device_id)
        full_session_value = round(price_per_minute * minutes, 2)

        if payment_method == "package_minutes":
            if available_minutes < minutes:
                raise ValueError("Customer does not have enough account minutes.")
            return {
                "full_session_value": full_session_value,
                "account_minutes_used": minutes,
                "charged_minutes": 0,
                "cash_or_card_amount": 0.0,
                "effective_payment_method": "package_minutes",
            }

        account_minutes_used = min(available_minutes, minutes)
        charged_minutes = max(0, minutes - account_minutes_used)
        cash_or_card_amount = round(price_per_minute * charged_minutes, 2)

        return {
            "full_session_value": full_session_value,
            "account_minutes_used": account_minutes_used,
            "charged_minutes": charged_minutes,
            "cash_or_card_amount": cash_or_card_amount,
            "effective_payment_method": payment_method,
        }

    def record_tanning_sale(
        self,
        *,
        customer_id: int,
        site_id: int,
        terminal_id: int,
        device_id: int,
        minutes: int,
        payment_method: str,
        staff_user_id: int,
        till_session_id: int | None,
    ):
        customer = self.get_customer(customer_id)
        if customer is None:
            raise ValueError("Customer not found.")

        quote = self.quote_tanning_sale(
            customer_id=customer_id,
            site_id=site_id,
            device_id=device_id,
            minutes=minutes,
            payment_method=payment_method,
        )

        device = self.fetch_one("select * from devices where id = ?", (device_id,))
        device_name = device["device_name"] if device else "Unknown Device"
        account_minutes_used = int(quote["account_minutes_used"])
        cash_or_card_amount = float(quote["cash_or_card_amount"])
        effective_payment_method = quote["effective_payment_method"]

        note_parts = [f"{minutes} min tanning - {device_name}"]
        if account_minutes_used > 0:
            note_parts.append(f"Used {account_minutes_used} account mins")
        if cash_or_card_amount > 0:
            note_parts.append(f"Charged GBP {cash_or_card_amount:.2f}")
        notes = " | ".join(note_parts)

        transaction_number = self.next_transaction_number()
        transaction_id = self.execute(
            """
            insert into transactions (
                transaction_number,
                customer_id,
                site_id,
                terminal_id,
                staff_user_id,
                transaction_type,
                total_amount,
                payment_method,
                status,
                notes
            ) values (?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?)
            """,
            (
                transaction_number,
                customer_id,
                site_id,
                terminal_id,
                staff_user_id,
                "tanning_sale",
                cash_or_card_amount,
                effective_payment_method,
                notes,
            ),
        )

        self.execute(
            """
            insert into transaction_lines (
                transaction_id,
                line_type,
                description,
                quantity,
                unit_price,
                line_total,
                minutes
            ) values (?, ?, ?, 1, ?, ?, ?)
            """,
            (
                transaction_id,
                "tanning_sale",
                f"Tanning Session - {device_name}",
                cash_or_card_amount,
                cash_or_card_amount,
                minutes,
            ),
        )

        if account_minutes_used > 0:
            self.execute(
                "update customers set package_minutes = package_minutes - ? where id = ?",
                (account_minutes_used, customer_id),
            )

        if till_session_id and effective_payment_method == "cash" and cash_or_card_amount > 0:
            self.execute(
                "update till_sessions set expected_cash = expected_cash + ? where id = ?",
                (cash_or_card_amount, till_session_id),
            )

        self.enqueue_sync_event(
            event_type="transaction.completed",
            payload={
                "transaction_id": transaction_id,
                "transaction_number": transaction_number,
                "transaction_type": "tanning_sale",
                "customer_id": customer_id,
                "site_id": site_id,
                "terminal_id": terminal_id,
                "staff_user_id": staff_user_id,
                "device_id": device_id,
                "device_name": device_name,
                "minutes": minutes,
                "payment_method": effective_payment_method,
                "account_minutes_used": account_minutes_used,
                "total_amount": cash_or_card_amount,
                "notes": notes,
                "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                **self.customer_sync_payload(customer_id),
            },
        )

        return {
            "transaction_id": transaction_id,
            "transaction_number": transaction_number,
            "total_amount": cash_or_card_amount,
            "full_session_value": float(quote["full_session_value"]),
            "account_minutes_used": account_minutes_used,
            "minutes": minutes,
            "device_id": device_id,
            "device_name": device_name,
            "payment_method": effective_payment_method,
            "notes": notes,
        }

    def record_retail_sale(
        self,
        *,
        customer_id: int,
        site_id: int,
        terminal_id: int,
        staff_user_id: int,
        till_session_id: int | None,
        product_id: int,
        quantity: int,
        payment_method: str,
    ):
        if payment_method not in {"cash", "card"}:
            raise ValueError("Creams and products can only be sold as cash or card here.")
        if quantity <= 0:
            raise ValueError("Quantity must be at least 1.")

        product = self.fetch_one(
            "select * from retail_products where id = ? and is_active = 1",
            (product_id,),
        )
        if product is None:
            raise ValueError("Product not found.")
        if int(product["stock_quantity"] or 0) < quantity:
            raise ValueError("Not enough stock available.")

        unit_price = float(product["price"] or 0)
        total_amount = round(unit_price * quantity, 2)
        size_part = (
            f" {product['size_label']}{product['unit_label']}"
            if product["size_label"] or product["unit_label"]
            else ""
        )
        description = f"{product['name']}{size_part}".strip()

        transaction_number = self.next_transaction_number()
        transaction_id = self.execute(
            """
            insert into transactions (
                transaction_number,
                customer_id,
                site_id,
                terminal_id,
                staff_user_id,
                transaction_type,
                total_amount,
                payment_method,
                status,
                notes
            ) values (?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?)
            """,
            (
                transaction_number,
                customer_id,
                site_id,
                terminal_id,
                staff_user_id,
                "retail_sale",
                total_amount,
                payment_method,
                f"Retail sale - {description}",
            ),
        )

        self.execute(
            """
            insert into transaction_lines (
                transaction_id,
                line_type,
                description,
                quantity,
                unit_price,
                line_total,
                minutes
            ) values (?, ?, ?, ?, ?, ?, 0)
            """,
            (
                transaction_id,
                "retail_sale",
                description,
                quantity,
                unit_price,
                total_amount,
            ),
        )

        self.execute(
            "update retail_products set stock_quantity = stock_quantity - ?, updated_at = current_timestamp where id = ?",
            (quantity, product_id),
        )

        if till_session_id and payment_method == "cash":
            self.execute(
                "update till_sessions set expected_cash = expected_cash + ? where id = ?",
                (total_amount, till_session_id),
            )

        self.enqueue_sync_event(
            event_type="transaction.completed",
            payload={
                "transaction_id": transaction_id,
                "transaction_number": transaction_number,
                "transaction_type": "retail_sale",
                "customer_id": customer_id,
                "site_id": site_id,
                "terminal_id": terminal_id,
                "staff_user_id": staff_user_id,
                "payment_method": payment_method,
                "total_amount": total_amount,
                "product_id": product_id,
                "product_name": description,
                "quantity": quantity,
                "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                **self.customer_sync_payload(customer_id),
            },
        )

        return {
            "transaction_id": transaction_id,
            "transaction_number": transaction_number,
            "product_name": description,
            "quantity": quantity,
            "total_amount": total_amount,
            "payment_method": payment_method,
        }

    def record_package_sale(
        self,
        *,
        customer_id: int,
        site_id: int,
        terminal_id: int,
        staff_user_id: int,
        till_session_id: int | None,
        package_product_id: int,
        payment_method: str,
    ):
        if payment_method not in {"cash", "card"}:
            raise ValueError("Sunbed courses can only be sold as cash or card here.")

        customer = self.get_customer(customer_id)
        if customer is None:
            raise ValueError("Customer not found.")

        package_row = self.fetch_one(
            "select * from package_products where id = ? and is_active = 1",
            (package_product_id,),
        )
        if package_row is None:
            raise ValueError("Course not found.")

        minutes_included = int(package_row["minutes_included"] or 0)
        total_amount = float(package_row["price"] or 0)
        description = str(package_row["name"])

        transaction_number = self.next_transaction_number()
        transaction_id = self.execute(
            """
            insert into transactions (
                transaction_number,
                customer_id,
                site_id,
                terminal_id,
                staff_user_id,
                transaction_type,
                total_amount,
                payment_method,
                status,
                notes
            ) values (?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?)
            """,
            (
                transaction_number,
                customer_id,
                site_id,
                terminal_id,
                staff_user_id,
                "package_sale",
                total_amount,
                payment_method,
                f"Course sale - {description}",
            ),
        )

        self.execute(
            """
            insert into transaction_lines (
                transaction_id,
                line_type,
                description,
                quantity,
                unit_price,
                line_total,
                minutes
            ) values (?, ?, ?, 1, ?, ?, ?)
            """,
            (
                transaction_id,
                "package_sale",
                description,
                total_amount,
                total_amount,
                minutes_included,
            ),
        )

        self.execute(
            "update customers set package_minutes = package_minutes + ? where id = ?",
            (minutes_included, customer_id),
        )

        if till_session_id and payment_method == "cash":
            self.execute(
                "update till_sessions set expected_cash = expected_cash + ? where id = ?",
                (total_amount, till_session_id),
            )

        self.enqueue_sync_event(
            event_type="transaction.completed",
            payload={
                "transaction_id": transaction_id,
                "transaction_number": transaction_number,
                "transaction_type": "package_sale",
                "customer_id": customer_id,
                "site_id": site_id,
                "terminal_id": terminal_id,
                "staff_user_id": staff_user_id,
                "payment_method": payment_method,
                "total_amount": total_amount,
                "package_product_id": package_product_id,
                "package_name": description,
                "minutes_included": minutes_included,
                "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                **self.customer_sync_payload(customer_id),
            },
        )

        return {
            "transaction_id": transaction_id,
            "transaction_number": transaction_number,
            "package_name": description,
            "minutes_included": minutes_included,
            "total_amount": total_amount,
            "payment_method": payment_method,
        }

    def shift_summary(self, till_session_id: int | None):
        if not till_session_id:
            return {
                "sales_total": 0.0,
                "cash_total": 0.0,
                "card_total": 0.0,
                "transaction_count": 0,
            }

        till_session = self.fetch_one("select * from till_sessions where id = ?", (till_session_id,))
        if till_session is None:
            return {
                "sales_total": 0.0,
                "cash_total": 0.0,
                "card_total": 0.0,
                "transaction_count": 0,
            }

        start_text = till_session["opened_at"]
        row = self.fetch_one(
            """
            select
                coalesce(sum(total_amount), 0) as sales_total,
                coalesce(sum(case when payment_method = 'cash' then total_amount else 0 end), 0) as cash_total,
                coalesce(sum(case when payment_method = 'card' then total_amount else 0 end), 0) as card_total,
                count(*) as transaction_count
            from transactions
            where status = 'completed'
              and site_id = ?
              and terminal_id = ?
              and datetime(created_at) >= datetime(?)
            """,
            (till_session["site_id"], till_session["terminal_id"], start_text),
        )
        return {
            "sales_total": float(row["sales_total"] or 0),
            "cash_total": float(row["cash_total"] or 0),
            "card_total": float(row["card_total"] or 0),
            "transaction_count": int(row["transaction_count"] or 0),
        }

    def day_summary(self, till_session: TillSessionContext | None):
        if till_session is None:
            return {
                "sales_total": 0.0,
                "cash_total": 0.0,
                "card_total": 0.0,
                "transaction_count": 0,
            }

        row = self.fetch_one(
            """
            select
                coalesce(sum(total_amount), 0) as sales_total,
                coalesce(sum(case when payment_method = 'cash' then total_amount else 0 end), 0) as cash_total,
                coalesce(sum(case when payment_method = 'card' then total_amount else 0 end), 0) as card_total,
                count(*) as transaction_count
            from transactions
            where status = 'completed'
              and site_id = ?
              and date(datetime(created_at, 'localtime')) = date('now', 'localtime')
            """,
            (till_session.site_id,),
        )
        return {
            "sales_total": float(row["sales_total"] or 0),
            "cash_total": float(row["cash_total"] or 0),
            "card_total": float(row["card_total"] or 0),
            "transaction_count": int(row["transaction_count"] or 0),
        }

    def close_till(
        self,
        till_session_id: int | None,
        *,
        counted_cash: float,
        closed_by_user_id: int,
        closing_notes: str = "",
    ):
        if not till_session_id:
            raise ValueError("No till session is open.")

        till_session = self.fetch_one("select * from till_sessions where id = ?", (till_session_id,))
        if till_session is None:
            raise ValueError("Till session not found.")
        if till_session["status"] != "open":
            raise ValueError("Till session is already closed.")

        expected_cash = float(till_session["expected_cash"] or 0)
        variance = float(counted_cash) - expected_cash

        self.execute(
            """
            update till_sessions
            set counted_cash = ?,
                variance = ?,
                closed_by_user_id = ?,
                closed_at = current_timestamp,
                status = 'closed',
                closing_notes = ?
            where id = ?
            """,
            (float(counted_cash), variance, int(closed_by_user_id), closing_notes.strip(), int(till_session_id)),
        )

        row = self.fetch_one("select * from till_sessions where id = ?", (int(till_session_id),))
        return {
            "expected_cash": expected_cash,
            "counted_cash": float(row["counted_cash"] or 0),
            "variance": float(row["variance"] or 0),
            "closed_at": row["closed_at"] or "",
        }

    def build_shift_summary_text(self, till_session_id: int):
        session = self.fetch_one(
            """
            select
                till_sessions.*,
                sites.name as site_name,
                terminals.name as terminal_name,
                opener.name as opened_by_name,
                closer.name as closed_by_name
            from till_sessions
            left join sites on sites.id = till_sessions.site_id
            left join terminals on terminals.id = till_sessions.terminal_id
            left join staff_users opener on opener.id = till_sessions.opened_by_user_id
            left join staff_users closer on closer.id = till_sessions.closed_by_user_id
            where till_sessions.id = ?
            """,
            (int(till_session_id),),
        )
        if session is None:
            return None

        totals = self.shift_summary(int(till_session_id))
        return "\n".join(
            [
                "Salon Max shift summary",
                "",
                f"Site: {session['site_name'] or '-'}",
                f"Terminal: {session['terminal_name'] or '-'}",
                f"Opened by: {session['opened_by_name'] or '-'}",
                f"Closed by: {session['closed_by_name'] or '-'}",
                f"Opened at: {self.timestamp_for_display(session['opened_at']) or '-'}",
                f"Closed at: {self.timestamp_for_display(session['closed_at']) or '-'}",
                "",
                f"Opening float: GBP {float(session['opening_float'] or 0):.2f}",
                f"Expected cash: GBP {float(session['expected_cash'] or 0):.2f}",
                f"Counted cash: GBP {float(session['counted_cash'] or 0):.2f}",
                f"Variance: GBP {float(session['variance'] or 0):.2f}",
                "",
                f"Shift sales: GBP {totals['sales_total']:.2f}",
                f"Shift cash: GBP {totals['cash_total']:.2f}",
                f"Shift card: GBP {totals['card_total']:.2f}",
                f"Shift transactions: {totals['transaction_count']}",
                f"Closing notes: {session['closing_notes'] or '-'}",
            ]
        )

    def send_shift_summary_email_for_session(self, till_session_id: int):
        settings = self.email_report_settings()
        if int(settings.get("email_reports_enabled") or 0) != 1:
            return False, "Email reports are disabled."
        if int(settings.get("auto_email_shift_reports") or 0) != 1:
            return False, "Automatic shift emails are disabled."

        session = self.fetch_one(
            """
            select till_sessions.*, sites.name as site_name
            from till_sessions
            left join sites on sites.id = till_sessions.site_id
            where till_sessions.id = ?
            """,
            (int(till_session_id),),
        )
        if session is None:
            return False, "Till session not found."

        body = self.build_shift_summary_text(int(till_session_id))
        if not body:
            return False, "Could not build shift summary."

        subject = f"Salon Max Shift Summary - {session['site_name'] or 'Site'} - Session {session['id']}"
        return self.send_management_email(subject, body)


def default_database_path(base_dir: Path) -> Path:
    live_db = Path("/home/benparker1984/till-build/till_backoffice.db")
    if live_db.exists():
        return live_db
    return base_dir / "till_backoffice.db"
