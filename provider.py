from flask import Flask, Response, g, jsonify, redirect, render_template, request, session, url_for
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from email.message import EmailMessage
import smtplib
from zoneinfo import ZoneInfo
from werkzeug.utils import secure_filename
from storage import connect, postgres


BASE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = BASE_DIR / "schema.sql"
UPLOADS_DIR = BASE_DIR / "static" / "uploads"
SUNBED_CATALOGUE_DIR = BASE_DIR / "static" / "sunbed-catalogue"
SUNBED_CATALOGUE_CSV_PATH = BASE_DIR / "docs" / "sunbed-manufacturer-model-catalogue-starter.csv"
SUNBED_IMAGE_PACK_CSV_PATH = BASE_DIR / "docs" / "sunbed-image-pack-starter.csv"
APP_ROLE = str(os.environ.get("SALONMAX_APP_ROLE") or "cloud").strip().lower() or "salon"


app = Flask(__name__)
app.secret_key = os.environ.get("SALONMAX_SECRET_KEY", "salonmax-local-dev-secret-change-before-production")
LOCAL_TIMEZONE = ZoneInfo("Europe/London")


def env_path(name: str, default: Path) -> Path:
    raw_value = str(os.environ.get(name) or "").strip()
    return Path(raw_value) if raw_value else default


def default_database_path() -> Path:
    if APP_ROLE == "cloud":
        return BASE_DIR / "salonmax_cloud_backoffice.db"
    live_db = Path("/home/benparker1984/till-build/till_backoffice.db")
    if live_db.exists():
        return live_db
    return BASE_DIR / "till_backoffice.db"


def default_platform_database_path() -> Path:
    return BASE_DIR / "salonmax_platform.db"


DATABASE_PATH = env_path("SALONMAX_DB_PATH", default_database_path())
PLATFORM_DATABASE_PATH = env_path("SALONMAX_PLATFORM_DB_PATH", default_platform_database_path())


def format_local_datetime(value) -> str:
    if not value:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    utc_dt = parse_utc_text(text)
    if utc_dt is None:
        return text
    return utc_dt.astimezone(LOCAL_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


app.jinja_env.filters["localdt"] = format_local_datetime


def now_utc_text() -> str:
    return datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def platform_auth_configured() -> bool:
    return bool(os.environ.get("SALONMAX_PLATFORM_ADMIN_PASSWORD", "").strip())


def platform_auth_disabled() -> bool:
    return os.environ.get("SALONMAX_PLATFORM_AUTH_DISABLED", "").strip() == "1"


def platform_login_redirect():
    return redirect(url_for("salonmax_platform_login", next=request.full_path.rstrip("?")))


@app.before_request
def require_platform_owner_login():
    path = request.path or ""
    if platform_auth_disabled():
        return None
    if not (path == "/salonmax-platform" or path.startswith("/platform")):
        return None
    if path in {"/platform-login", "/platform-logout"}:
        return None
    if session.get("platform_admin_authenticated") or session.get("salonmax_platform_authed"):
        return None
    return platform_login_redirect()


def parse_utc_text(value):
    text = str(value or "").strip()
    if not text:
        return None
    candidates = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
    ]
    for fmt in candidates:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=ZoneInfo("UTC"))
        except ValueError:
            continue
    return None


def get_db():
    if "db" not in g:
        g.db = connect(DATABASE_PATH, schema="salon_config")
    return g.db


def get_platform_db():
    if "platform_db" not in g:
        g.platform_db = connect(PLATFORM_DATABASE_PATH)
    return g.platform_db


@app.teardown_appcontext
def close_db(_error):
    db = g.pop("db", None)
    if db is not None:
        db.close()
    platform_db = g.pop("platform_db", None)
    if platform_db is not None:
        platform_db.close()


def query_all(sql, params=()):
    return get_db().execute(sql, params).fetchall()


def query_one(sql, params=()):
    return get_db().execute(sql, params).fetchone()


def execute(sql, params=()):
    db = get_db()
    cursor = db.execute(sql, params)
    db.commit()
    return cursor.lastrowid


def platform_query_all(sql, params=()):
    return get_platform_db().execute(sql, params).fetchall()


def platform_query_one(sql, params=()):
    return get_platform_db().execute(sql, params).fetchone()


def platform_execute(sql, params=()):
    db = get_platform_db()
    cursor = db.execute(sql, params)
    db.commit()
    return cursor.lastrowid


def next_customer_number():
    row = query_one(
        "select customer_number from customers order by cast(customer_number as integer) desc limit 1"
    )
    if row is None:
        return "1001"
    try:
        return str(int(row["customer_number"]) + 1)
    except (TypeError, ValueError):
        return "1001"


def parse_ivy_balance_text(balance_text: str):
    text = str(balance_text or "").strip()
    normal_match = re.search(r"Normal:\s*([0-9]+(?:\.[0-9]+)?)", text, re.IGNORECASE)
    mins_match = re.search(r"Tanning Courses:\s*([0-9]+(?:\.[0-9]+)?)", text, re.IGNORECASE)

    normal_value = float(normal_match.group(1)) if normal_match else 0.0
    minutes_value = mins_match.group(1) if mins_match else "0"
    try:
        package_minutes = int(round(float(minutes_value)))
    except ValueError:
        package_minutes = 0
    return normal_value, package_minutes


def import_ivy_customer_rows(raw_text: str):
    inserted = 0
    updated = 0
    skipped = 0

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.lower().startswith("card number\t") or line.lower().startswith("card number,"):
            continue

        parts = [part.strip() for part in re.split(r"\t+", line)]
        if len(parts) < 5:
            skipped += 1
            continue

        card_number, first_name, last_name, _creation_date = parts[:4]
        balance_text = "\t".join(parts[4:]).strip()

        if not card_number:
            skipped += 1
            continue

        if not first_name and not last_name:
            skipped += 1
            continue

        account_balance, package_minutes = parse_ivy_balance_text(balance_text)
        first_name = first_name or "Unknown"
        last_name = last_name or "-"

        existing = query_one("select * from customers where account_number = ?", (card_number,))
        if existing is None:
            execute(
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
                ) values (?, ?, ?, ?, '', '', ?, ?, 1)
                """,
                (next_customer_number(), card_number, first_name, last_name, account_balance, package_minutes),
            )
            inserted += 1
        else:
            execute(
                """
                update customers
                set first_name = ?,
                    last_name = ?,
                    account_balance = ?,
                    package_minutes = ?,
                    is_active = 1
                where id = ?
                """,
                (first_name, last_name, account_balance, package_minutes, existing["id"]),
            )
            updated += 1

    return inserted, updated, skipped


def ensure_business_settings_table():
    db = get_db()
    db.execute(
        """
        create table if not exists business_settings (
            id integer primary key check (id = 1),
            business_name text not null default 'Your Salon',
            currency_symbol text not null default 'GBP',
            default_package_validity_days integer not null default 365,
            default_prep_minutes integer not null default 3,
            default_cooldown_minutes integer not null default 3,
            till_logo_path text not null default '',
            till_theme_preset text not null default 'ultra_violet',
            till_primary_color text not null default '#155e75',
            till_button_color text not null default '#1976d2',
            till_background_color text not null default '#111111',
            till_text_color text not null default '#ffffff',
            peak_price_per_minute real not null default 0.65,
            happy_hour_price_per_minute real not null default 0.55,
            happy_hour_1_start text not null default '10:00',
            happy_hour_1_end text not null default '11:00',
            happy_hour_2_start text not null default '20:00',
            happy_hour_2_end text not null default '21:00',
            management_report_emails text not null default '',
            report_from_email text not null default '',
            smtp_host text not null default '',
            smtp_port integer not null default 587,
            smtp_username text not null default '',
            smtp_password text not null default '',
            smtp_use_tls integer not null default 1,
            email_reports_enabled integer not null default 0,
            auto_email_shift_reports integer not null default 1,
            auto_email_daily_reports integer not null default 0
        )
        """
    )
    existing_columns = {
        row["name"]
        for row in db.execute("select name from pragma_table_info('business_settings')").fetchall()
    }
    columns_to_add = {
        "till_theme_preset": "text not null default 'ultra_violet'",
        "peak_price_per_minute": "real not null default 0.65",
        "happy_hour_price_per_minute": "real not null default 0.55",
        "happy_hour_1_start": "text not null default '10:00'",
        "happy_hour_1_end": "text not null default '11:00'",
        "happy_hour_2_start": "text not null default '20:00'",
        "happy_hour_2_end": "text not null default '21:00'",
        "management_report_emails": "text not null default ''",
        "report_from_email": "text not null default ''",
        "smtp_host": "text not null default ''",
        "smtp_port": "integer not null default 587",
        "smtp_username": "text not null default ''",
        "smtp_password": "text not null default ''",
        "smtp_use_tls": "integer not null default 1",
        "email_reports_enabled": "integer not null default 0",
        "auto_email_shift_reports": "integer not null default 1",
        "auto_email_daily_reports": "integer not null default 0",
    }
    for column_name, column_sql in columns_to_add.items():
        if column_name not in existing_columns:
            db.execute(f"alter table business_settings add column {column_name} {column_sql}")
    db.execute(
        """
        insert into business_settings (
            id,
            business_name,
            currency_symbol,
            default_package_validity_days,
            default_prep_minutes,
            default_cooldown_minutes,
            till_logo_path,
            till_theme_preset,
            till_primary_color,
            till_button_color,
            till_background_color,
            till_text_color,
            peak_price_per_minute,
            happy_hour_price_per_minute,
            happy_hour_1_start,
            happy_hour_1_end,
            happy_hour_2_start,
            happy_hour_2_end,
            management_report_emails,
            report_from_email,
            smtp_host,
            smtp_port,
            smtp_username,
            smtp_password,
            smtp_use_tls,
            email_reports_enabled,
            auto_email_shift_reports,
            auto_email_daily_reports
        )
        select 1, 'Your Salon', 'GBP', 365, 3, 3, '', 'ultra_violet', '#155e75', '#1976d2', '#111111', '#ffffff', 0.65, 0.55, '10:00', '11:00', '20:00', '21:00', '', '', '', 587, '', '', 1, 0, 1, 0
        where not exists (select 1 from business_settings where id = 1)
        """
    )
    db.commit()


def ensure_platform_sync_tables():
    if app.config.get("PLATFORM_SCHEMA_READY"):
        return
    db = get_platform_db()
    db.execute(
        """
        create table if not exists cloud_business_accounts (
            id integer primary key autoincrement,
            business_account_public_id text not null unique,
            business_name text not null,
            product_type text not null default 'salon',
            status text not null default 'active',
            subscription_plan text not null default 'pilot',
            subscription_status text not null default 'active',
            contact_name text not null default '',
            contact_email text not null default '',
            contact_phone text not null default '',
            billing_email text not null default '',
            company_number text not null default '',
            address_line_1 text not null default '',
            address_line_2 text not null default '',
            city text not null default '',
            county text not null default '',
            postcode text not null default '',
            billing_address_line_1 text not null default '',
            billing_address_line_2 text not null default '',
            billing_city text not null default '',
            billing_county text not null default '',
            billing_postcode text not null default '',
            vat_number text not null default '',
            contract_start_date text not null default '',
            renewal_date text not null default '',
            monthly_fee text not null default '',
            notes text not null default '',
            created_at text not null default current_timestamp,
            updated_at text not null default current_timestamp
        )
        """
    )
    business_columns = {
        row["name"]
        for row in db.execute("select name from pragma_table_info('cloud_business_accounts')").fetchall()
    }
    business_columns_to_add = {
        "product_type": "text not null default 'salon'",
        "contact_name": "text not null default ''",
        "contact_email": "text not null default ''",
        "contact_phone": "text not null default ''",
        "billing_email": "text not null default ''",
        "company_number": "text not null default ''",
        "address_line_1": "text not null default ''",
        "address_line_2": "text not null default ''",
        "city": "text not null default ''",
        "county": "text not null default ''",
        "postcode": "text not null default ''",
        "billing_address_line_1": "text not null default ''",
        "billing_address_line_2": "text not null default ''",
        "billing_city": "text not null default ''",
        "billing_county": "text not null default ''",
        "billing_postcode": "text not null default ''",
        "vat_number": "text not null default ''",
        "contract_start_date": "text not null default ''",
        "renewal_date": "text not null default ''",
        "monthly_fee": "text not null default ''",
        "notes": "text not null default ''",
    }
    for column_name, column_type in business_columns_to_add.items():
        if column_name not in business_columns:
            db.execute(f"alter table cloud_business_accounts add column {column_name} {column_type}")
    db.execute(
        """
        update cloud_business_accounts
        set product_type = 'salon'
        where product_type is null
           or trim(product_type) = ''
        """
    )
    db.execute(
        """
        create table if not exists cloud_sync_events (
            id integer primary key autoincrement,
            business_account_public_id text not null,
            terminal_device_public_id text not null,
            local_event_id text not null,
            event_type text not null,
            occurred_at text not null,
            payload_json text not null,
            received_at text not null default current_timestamp,
            unique (business_account_public_id, terminal_device_public_id, local_event_id)
        )
        """
    )
    db.execute(
        """
        create table if not exists cloud_business_sites (
            id integer primary key autoincrement,
            business_account_public_id text not null,
            site_public_id text not null unique,
            site_name text not null,
            site_code text not null,
            status text not null default 'active',
            created_at text not null default current_timestamp,
            updated_at text not null default current_timestamp
        )
        """
    )
    db.execute(
        """
        create table if not exists cloud_terminal_registry (
            id integer primary key autoincrement,
            business_account_public_id text not null,
            site_public_id text not null,
            terminal_device_public_id text not null unique,
            terminal_name text not null,
            pairing_code text not null,
            install_mode text not null default 'fresh_install',
            status text not null default 'ready_to_pair',
            management_status text not null default 'active',
            app_version_reported text not null default '',
            desired_app_version text not null default '',
            app_update_channel text not null default 'stable',
            sync_status text not null default 'healthy',
            sync_pending_count integer not null default 0,
            sync_failed_count integer not null default 0,
            sync_oldest_outstanding_at text,
            sync_last_attempt_at text,
            sync_last_acknowledged_at text,
            sync_last_checkpoint_at text,
            sync_recent_failures_json text not null default '[]',
            sync_recent_pending_json text not null default '[]',
            created_at text not null default current_timestamp,
            updated_at text not null default current_timestamp,
            last_seen_at text
        )
        """
    )
    terminal_columns = {
        row["name"]
        for row in db.execute("select name from pragma_table_info('cloud_terminal_registry')").fetchall()
    }
    if "install_mode" not in terminal_columns:
        db.execute("alter table cloud_terminal_registry add column install_mode text not null default 'fresh_install'")
    if "management_status" not in terminal_columns:
        db.execute("alter table cloud_terminal_registry add column management_status text not null default 'active'")
    if "app_version_reported" not in terminal_columns:
        db.execute("alter table cloud_terminal_registry add column app_version_reported text not null default ''")
    if "desired_app_version" not in terminal_columns:
        db.execute("alter table cloud_terminal_registry add column desired_app_version text not null default ''")
    if "app_update_channel" not in terminal_columns:
        db.execute("alter table cloud_terminal_registry add column app_update_channel text not null default 'stable'")
    if "sync_status" not in terminal_columns:
        db.execute("alter table cloud_terminal_registry add column sync_status text not null default 'healthy'")
    if "sync_pending_count" not in terminal_columns:
        db.execute("alter table cloud_terminal_registry add column sync_pending_count integer not null default 0")
    if "sync_failed_count" not in terminal_columns:
        db.execute("alter table cloud_terminal_registry add column sync_failed_count integer not null default 0")
    if "sync_oldest_outstanding_at" not in terminal_columns:
        db.execute("alter table cloud_terminal_registry add column sync_oldest_outstanding_at text")
    if "sync_last_attempt_at" not in terminal_columns:
        db.execute("alter table cloud_terminal_registry add column sync_last_attempt_at text")
    if "sync_last_acknowledged_at" not in terminal_columns:
        db.execute("alter table cloud_terminal_registry add column sync_last_acknowledged_at text")
    if "sync_last_checkpoint_at" not in terminal_columns:
        db.execute("alter table cloud_terminal_registry add column sync_last_checkpoint_at text")
    if "sync_recent_failures_json" not in terminal_columns:
        db.execute("alter table cloud_terminal_registry add column sync_recent_failures_json text not null default '[]'")
    if "sync_recent_pending_json" not in terminal_columns:
        db.execute("alter table cloud_terminal_registry add column sync_recent_pending_json text not null default '[]'")
    db.execute(
        """
        create table if not exists cloud_device_licences (
            id integer primary key autoincrement,
            business_account_public_id text not null,
            terminal_device_public_id text not null unique,
            licence_status text not null default 'active',
            signed_token text not null,
            issued_at text not null,
            expires_at text not null,
            last_check_in_at text not null
        )
        """
    )
    db.execute(
        """
        create table if not exists cloud_customers (
            id integer primary key autoincrement,
            business_account_public_id text not null,
            customer_public_id text not null unique,
            customer_number text not null default '',
            account_number text not null default '',
            first_name text not null default '',
            last_name text not null default '',
            phone text not null default '',
            email text not null default '',
            marketing_opt_in integer not null default 0,
            notes text not null default '',
            status text not null default 'active',
            created_at text not null default current_timestamp,
            updated_at text not null default current_timestamp
        )
        """
    )
    db.execute(
        """
        create table if not exists cloud_customer_balance_ledger (
            id integer primary key autoincrement,
            business_account_public_id text not null,
            customer_public_id text not null,
            site_public_id text not null default '',
            terminal_device_public_id text not null default '',
            staff_user_public_id text not null default '',
            source_event_id text not null default '',
            source_reference text not null default '',
            entry_type text not null,
            delta_amount text not null default '0.00',
            balance_after text not null default '0.00',
            currency_code text not null default 'GBP',
            notes text not null default '',
            created_at text not null default current_timestamp
        )
        """
    )
    db.execute(
        """
        create table if not exists cloud_customer_minute_ledger (
            id integer primary key autoincrement,
            business_account_public_id text not null,
            customer_public_id text not null,
            site_public_id text not null default '',
            terminal_device_public_id text not null default '',
            staff_user_public_id text not null default '',
            source_event_id text not null default '',
            source_reference text not null default '',
            package_code text not null default '',
            entry_type text not null,
            delta_minutes integer not null default 0,
            minutes_after integer not null default 0,
            notes text not null default '',
            created_at text not null default current_timestamp
        )
        """
    )
    db.execute(
        """
        create table if not exists cloud_customer_balance_summary (
            customer_public_id text primary key,
            business_account_public_id text not null,
            current_balance text not null default '0.00',
            currency_code text not null default 'GBP',
            updated_at text not null default current_timestamp
        )
        """
    )
    db.execute(
        """
        create table if not exists cloud_customer_minute_summary (
            customer_public_id text primary key,
            business_account_public_id text not null,
            minutes_available integer not null default 0,
            updated_at text not null default current_timestamp
        )
        """
    )
    db.execute(
        """
        create table if not exists cloud_support_notes (
            id integer primary key autoincrement,
            business_account_public_id text not null,
            terminal_device_public_id text not null default '',
            customer_public_id text not null default '',
            note_type text not null default 'support_note',
            author_name text not null default '',
            note_text text not null,
            created_at text not null default current_timestamp
        )
        """
    )
    db.execute(
        "create index if not exists idx_cloud_customers_business_name on cloud_customers (business_account_public_id, last_name, first_name)"
    )
    db.execute(
        "create index if not exists idx_balance_ledger_business_customer_created on cloud_customer_balance_ledger (business_account_public_id, customer_public_id, created_at)"
    )
    db.execute(
        "create index if not exists idx_balance_ledger_source_event on cloud_customer_balance_ledger (source_event_id)"
    )
    db.execute(
        "create index if not exists idx_minute_ledger_business_customer_created on cloud_customer_minute_ledger (business_account_public_id, customer_public_id, created_at)"
    )
    db.execute(
        "create index if not exists idx_minute_ledger_source_event on cloud_customer_minute_ledger (source_event_id)"
    )
    db.execute(
        "create index if not exists idx_cloud_support_notes_business_created on cloud_support_notes (business_account_public_id, created_at desc)"
    )
    support_note_columns = {
        str(row["name"] or "")
        for row in db.execute("pragma table_info('cloud_support_notes')").fetchall()
    }
    if "customer_public_id" not in support_note_columns:
        db.execute("alter table cloud_support_notes add column customer_public_id text not null default ''")
    db.execute(
        "create index if not exists idx_cloud_support_notes_business_customer_created on cloud_support_notes (business_account_public_id, customer_public_id, created_at desc)"
    )
    db.commit()


def ensure_cloud_business_account(business_account_public_id: str, business_name: str):
    ensure_platform_sync_tables()
    account_id = str(business_account_public_id or "").strip()
    name = str(business_name or "").strip() or "Unnamed Business"
    if not account_id:
        return
    existing = platform_query_one(
        "select id, business_name, coalesce(product_type, 'salon') as product_type from cloud_business_accounts where business_account_public_id = ?",
        (account_id,),
    )
    if existing is None:
        platform_execute(
            """
            insert into cloud_business_accounts (
                business_account_public_id,
                business_name,
                status,
                subscription_plan,
                subscription_status
            ) values (?, ?, 'active', 'pilot', 'active')
            """,
            (account_id, name),
        )
        return
    # Gym accounts are provider-managed customer accounts; don't let legacy
    # salon/till imports rename them back to "Imported biz_*".
    if str(existing["product_type"] or "salon") == "gym":
        return
    if str(existing["business_name"] or "").strip() != name:
        platform_execute(
            """
            update cloud_business_accounts
            set business_name = ?,
                updated_at = current_timestamp
            where business_account_public_id = ?
            """,
            (name, account_id),
        )


def make_public_id(prefix: str, seed_text: str) -> str:
    safe = re.sub(r"[^a-z0-9]+", "-", str(seed_text or "").strip().lower()).strip("-")
    safe = safe[:24] or "item"
    return f"{prefix}_{safe}"


def make_pairing_code() -> str:
    return datetime.utcnow().strftime("%d%H%M") + os.urandom(2).hex().upper()


def make_cloud_customer_public_id(business_account_public_id: str, local_customer_id) -> str:
    account_safe = re.sub(r"[^a-z0-9]+", "-", str(business_account_public_id or "").strip().lower()).strip("-") or "biz"
    customer_safe = re.sub(r"[^a-z0-9]+", "-", str(local_customer_id or "").strip().lower()).strip("-") or "customer"
    return f"cust_{account_safe}_{customer_safe}"


def parse_customer_import_csv(raw_text: str) -> dict:
    text = str(raw_text or "").lstrip("\ufeff").strip()
    if not text:
        return {
            "rows": [],
            "error_count": 0,
            "skipped_count": 0,
            "warnings": ["No CSV data was provided."],
        }
    reader = csv.DictReader(StringIO(text, newline=""), strict=True)
    rows = []
    warnings = []
    skipped_count = 0
    error_count = 0
    row_number = 1
    seen = set()
    for source_row in reader:
        row_number += 1
        if not source_row:
            continue
        normalized = {
            str(key or "").strip().lower(): str(value or "").strip()
            for key, value in source_row.items()
        }
        first_name = normalized.get("first_name") or normalized.get("firstname") or normalized.get("forename") or ""
        last_name = normalized.get("last_name") or normalized.get("lastname") or normalized.get("surname") or ""
        customer_number = normalized.get("customer_number") or normalized.get("customernumber") or normalized.get("client_number") or ""
        account_number = normalized.get("account_number") or normalized.get("accountnumber") or normalized.get("member_number") or ""
        phone = normalized.get("phone") or normalized.get("mobile") or normalized.get("telephone") or ""
        email = normalized.get("email") or normalized.get("email_address") or ""
        notes = normalized.get("notes") or normalized.get("note") or ""
        balance_text = normalized.get("balance") or normalized.get("account_balance") or normalized.get("credit_balance")
        minutes_text = normalized.get("minutes") or normalized.get("package_minutes") or normalized.get("account_minutes")
        if not any(normalized.values()):
            skipped_count += 1
            continue
        email = email.lower()
        identities = {(key, value) for key, value in (
            ("customer_number", customer_number), ("account_number", account_number), ("email", email)
        ) if value}
        try:
            if None in source_row or any(value is None for value in source_row.values()):
                raise ValueError("wrong number of fields")
            if not identities:
                raise ValueError("provide a customer number, account number or email")
            if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                raise ValueError("invalid email")
            balance = Decimal(balance_text) if balance_text else None
            if balance is not None and (not balance.is_finite() or abs(balance) > 1_000_000 or balance != round(balance, 2)):
                raise ValueError("invalid balance")
            minutes = int(minutes_text) if minutes_text else None
            if minutes is not None and not 0 <= minutes <= 1_000_000:
                raise ValueError("invalid minutes")
        except InvalidOperation:
            error_count += 1
            warnings.append(f"Row {row_number}: invalid balance.")
            continue
        except ValueError as exc:
            error_count += 1
            message = "invalid minutes" if str(exc).startswith("invalid literal for int") else str(exc)
            warnings.append(f"Row {row_number}: {message}.")
            continue
        if identities & seen:
            skipped_count += 1
            warnings.append(f"Row {row_number}: duplicate identifier within the CSV.")
            continue
        seen.update(identities)
        rows.append(
            {
                "customer_number": customer_number,
                "account_number": account_number,
                "first_name": first_name,
                "last_name": last_name,
                "phone": phone,
                "email": email,
                "notes": notes,
                "current_balance": balance,
                "minutes_available": minutes,
            }
        )
    return {
        "rows": rows,
        "error_count": error_count,
        "skipped_count": skipped_count,
        "warnings": warnings[:12],
    }


def upsert_cloud_import_customer(
    db,
    *,
    business_account_public_id: str,
    row: dict,
    source_reference: str,
    author_name: str = "",
):
    customer_number = str(row.get("customer_number") or "").strip()
    account_number = str(row.get("account_number") or "").strip()
    first_name = str(row.get("first_name") or "").strip()
    last_name = str(row.get("last_name") or "").strip()
    phone = str(row.get("phone") or "").strip()
    email = str(row.get("email") or "").strip()
    notes = str(row.get("notes") or "").strip()
    target_balance = row.get("current_balance")
    target_minutes = row.get("minutes_available")
    customer_public_id = "cust_import_" + os.urandom(16).hex()
    matches = db.execute(
        """
        select * from cloud_customers
        where business_account_public_id = ? and (
            (customer_number <> '' and customer_number = ?)
            or (account_number <> '' and account_number = ?)
            or (email <> '' and lower(email) = ?)
        )
        """,
        (business_account_public_id, customer_number, account_number, email),
    ).fetchall()
    if len(matches) > 1:
        return {"conflict": True}
    existing = matches[0] if matches else None

    if existing is None:
        db.execute(
            """
            insert into cloud_customers (
                business_account_public_id,
                customer_public_id,
                customer_number,
                account_number,
                first_name,
                last_name,
                phone,
                email,
                notes,
                status,
                updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', current_timestamp)
            """,
            (
                business_account_public_id,
                customer_public_id,
                customer_number,
                account_number,
                first_name,
                last_name,
                phone,
                email,
                notes,
            ),
        )
        created = True
    else:
        customer_public_id = str(existing["customer_public_id"] or customer_public_id)
        db.execute(
            """
            update cloud_customers
            set customer_number = ?,
                account_number = ?,
                first_name = ?,
                last_name = ?,
                phone = ?,
                email = ?,
                notes = ?,
                updated_at = current_timestamp
            where business_account_public_id = ? and customer_public_id = ?
            """,
            (
                customer_number or existing["customer_number"],
                account_number or existing["account_number"],
                first_name or existing["first_name"],
                last_name or existing["last_name"],
                phone or existing["phone"],
                email or existing["email"],
                notes or existing["notes"],
                business_account_public_id,
                customer_public_id,
            ),
        )
        created = False

    minute_summary = ensure_cloud_customer_minute_summary(db, business_account_public_id, customer_public_id)
    balance_summary = ensure_cloud_customer_balance_summary(db, business_account_public_id, customer_public_id)
    current_minutes = int(minute_summary["minutes_available"] or 0)
    current_balance = round(float(balance_summary["current_balance"] or 0), 2)
    minute_delta = target_minutes - current_minutes if target_minutes is not None else 0
    balance_delta = round(float(target_balance) - current_balance, 2) if target_balance is not None else 0
    occurred_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    if minute_delta != 0:
        db.execute(
            """
            insert into cloud_customer_minute_ledger (
                business_account_public_id,
                customer_public_id,
                source_reference,
                entry_type,
                delta_minutes,
                minutes_after,
                notes,
                created_at
            ) values (?, ?, ?, 'migration_import', ?, ?, ?, ?)
            """,
            (
                business_account_public_id,
                customer_public_id,
                source_reference,
                minute_delta,
                target_minutes,
                f"Customer import by {author_name or 'Salon Max'}",
                occurred_at,
            ),
        )
        db.execute(
            """
            update cloud_customer_minute_summary
            set minutes_available = ?,
                updated_at = current_timestamp
            where business_account_public_id = ? and customer_public_id = ?
            """,
            (target_minutes, business_account_public_id, customer_public_id),
        )
    if balance_delta != 0:
        db.execute(
            """
            insert into cloud_customer_balance_ledger (
                business_account_public_id,
                customer_public_id,
                source_reference,
                entry_type,
                delta_amount,
                balance_after,
                currency_code,
                notes,
                created_at
            ) values (?, ?, ?, 'migration_import', ?, ?, 'GBP', ?, ?)
            """,
            (
                business_account_public_id,
                customer_public_id,
                source_reference,
                f"{balance_delta:.2f}",
                f"{target_balance:.2f}",
                f"Customer import by {author_name or 'Salon Max'}",
                occurred_at,
            ),
        )
        db.execute(
            """
            update cloud_customer_balance_summary
            set current_balance = ?,
                updated_at = current_timestamp
            where business_account_public_id = ? and customer_public_id = ?
            """,
            (f"{target_balance:.2f}", business_account_public_id, customer_public_id),
        )
    return {"created": created, "updated": not created, "customer_public_id": customer_public_id}


def cloud_staff_public_id_from_local(local_staff_user_id) -> str:
    value = str(local_staff_user_id or "").strip()
    return f"staff_local_{value}" if value else ""


def ensure_cloud_customer_from_event(db, business_account_public_id: str, payload: dict):
    local_customer_id = payload.get("customer_id")
    customer_text = str(local_customer_id or "").strip()
    if not customer_text:
        return ""
    customer_public_id = make_cloud_customer_public_id(business_account_public_id, customer_text)
    customer_number = str(payload.get("customer_number") or customer_text).strip()
    account_number = str(payload.get("account_number") or "").strip()
    first_name = str(payload.get("customer_first_name") or "").strip()
    last_name = str(payload.get("customer_last_name") or "").strip()
    phone = str(payload.get("customer_phone") or "").strip()
    email = str(payload.get("customer_email") or "").strip()
    existing = db.execute(
        """
        select id
        from cloud_customers
        where customer_public_id = ?
        """,
        (customer_public_id,),
    ).fetchone()
    if existing is None:
        db.execute(
            """
            insert into cloud_customers (
                business_account_public_id,
                customer_public_id,
                customer_number,
                account_number,
                first_name,
                last_name,
                phone,
                email,
                notes,
                status
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
            """,
            (
                business_account_public_id,
                customer_public_id,
                customer_number,
                account_number,
                first_name,
                last_name,
                phone,
                email,
                f"Imported from local customer id {customer_text}",
            ),
        )
    else:
        db.execute(
            """
            update cloud_customers
            set customer_number = ?,
                account_number = ?,
                first_name = ?,
                last_name = ?,
                phone = ?,
                email = ?,
                updated_at = current_timestamp
            where customer_public_id = ?
            """,
            (
                customer_number,
                account_number,
                first_name,
                last_name,
                phone,
                email,
                customer_public_id,
            ),
        )
    return customer_public_id


def ensure_cloud_customer_balance_summary(db, business_account_public_id: str, customer_public_id: str):
    row = db.execute(
        """
        select *
        from cloud_customer_balance_summary
        where customer_public_id = ?
        """,
        (customer_public_id,),
    ).fetchone()
    if row is None:
        db.execute(
            """
            insert into cloud_customer_balance_summary (
                customer_public_id,
                business_account_public_id,
                current_balance,
                currency_code
            ) values (?, ?, '0.00', 'GBP')
            """,
            (customer_public_id, business_account_public_id),
        )
        row = db.execute(
            """
            select *
            from cloud_customer_balance_summary
            where customer_public_id = ?
            """,
            (customer_public_id,),
        ).fetchone()
    return row


def ensure_cloud_customer_minute_summary(db, business_account_public_id: str, customer_public_id: str):
    row = db.execute(
        """
        select *
        from cloud_customer_minute_summary
        where customer_public_id = ?
        """,
        (customer_public_id,),
    ).fetchone()
    if row is None:
        db.execute(
            """
            insert into cloud_customer_minute_summary (
                customer_public_id,
                business_account_public_id,
                minutes_available
            ) values (?, ?, 0)
            """,
            (customer_public_id, business_account_public_id),
        )
        row = db.execute(
            """
            select *
            from cloud_customer_minute_summary
            where customer_public_id = ?
            """,
            (customer_public_id,),
        ).fetchone()
    return row


def backoffice_topup_minutes(payload):
    minutes = str(payload.get("minutes") or payload.get("minutes_included") or "").strip()
    if not minutes:
        match = re.fullmatch(r"Back office minutes top-up: ([0-9]{1,9}) mins", str(payload.get("notes") or "").strip())
        minutes = match[1] if match else ""
    return int(minutes) if re.fullmatch(r"[0-9]{1,9}", minutes) else 0


def restore_synced_topups():
    db = get_platform_db()
    rows = db.execute("""
        select e.* from cloud_sync_events e
        where e.event_type='transaction.completed' and e.payload_json like '%minutes_topup%'
          and not exists (
            select 1 from cloud_customer_minute_ledger l
            where l.business_account_public_id=e.business_account_public_id
              and l.terminal_device_public_id=e.terminal_device_public_id
              and l.source_event_id=e.local_event_id
          )
        order by e.business_account_public_id, e.id
    """).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        if (not isinstance(payload, dict) or str(payload.get("transaction_type") or "").strip() != "minutes_topup"
                or not payload.get("customer_id") or not backoffice_topup_minutes(payload)):
            continue
        business = row["business_account_public_id"]
        device = row["terminal_device_public_id"]
        event = row["local_event_id"]
        if hasattr(db, "raw"):
            db.raw.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (business,))
        if not db.execute("""
            select 1 from cloud_customer_minute_ledger where business_account_public_id=?
            and terminal_device_public_id=? and source_event_id=?
        """, (business, device, event)).fetchone():
            project_sync_event_to_ledgers(
                db, business_account_public_id=business, terminal_device_public_id=device,
                local_event_id=event, event_type=row["event_type"], occurred_at=row["occurred_at"], payload=payload,
            )
        db.commit()


def project_sync_event_to_ledgers(
    db,
    *,
    business_account_public_id: str,
    terminal_device_public_id: str,
    local_event_id: str,
    event_type: str,
    occurred_at: str,
    payload: dict,
):
    if event_type != "transaction.completed" or not isinstance(payload, dict):
        return

    transaction_type = str(payload.get("transaction_type") or "").strip()
    customer_public_id = ""
    if transaction_type == "minutes_topup" and str(payload.get("customer_id") or "").strip():
        candidate = make_cloud_customer_public_id(business_account_public_id, payload["customer_id"])
        if db.execute("""
            select 1 from cloud_customers where business_account_public_id=? and customer_public_id=?
        """, (business_account_public_id, candidate)).fetchone():
            customer_public_id = candidate
    if not customer_public_id:
        customer_public_id = ensure_cloud_customer_from_event(db, business_account_public_id, payload)
    if not customer_public_id:
        return

    terminal_row = db.execute(
        """
        select site_public_id
        from cloud_terminal_registry
        where terminal_device_public_id = ?
        """,
        (terminal_device_public_id,),
    ).fetchone()
    site_public_id = str((terminal_row["site_public_id"] if terminal_row else "") or "")
    staff_user_public_id = cloud_staff_public_id_from_local(payload.get("staff_user_id"))
    transaction_number = str(payload.get("transaction_number") or "").strip()
    notes = str(payload.get("notes") or "").strip()

    if transaction_type in {"package_sale", "minutes_topup"}:
        if transaction_type == "minutes_topup":
            minutes_delta = backoffice_topup_minutes(payload)
        else:
            minutes_delta = int(payload.get("minutes_included") or 0)
        if minutes_delta > 0:
            summary_row = ensure_cloud_customer_minute_summary(db, business_account_public_id, customer_public_id)
            minutes_after = int(summary_row["minutes_available"] or 0) + minutes_delta
            db.execute(
                """
                insert into cloud_customer_minute_ledger (
                    business_account_public_id,
                    customer_public_id,
                    site_public_id,
                    terminal_device_public_id,
                    staff_user_public_id,
                    source_event_id,
                    source_reference,
                    package_code,
                    entry_type,
                    delta_minutes,
                    minutes_after,
                    notes,
                    created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    business_account_public_id,
                    customer_public_id,
                    site_public_id,
                    terminal_device_public_id,
                    staff_user_public_id,
                    local_event_id,
                    transaction_number,
                    str(payload.get("package_product_id") or ""),
                    "backoffice_topup" if transaction_type == "minutes_topup" else "minutes_purchased",
                    minutes_delta,
                    minutes_after,
                    notes or str(payload.get("package_name") or "").strip(),
                    occurred_at,
                ),
            )
            db.execute(
                """
                update cloud_customer_minute_summary
                set minutes_available = ?,
                    updated_at = current_timestamp
                where customer_public_id = ?
                """,
                (minutes_after, customer_public_id),
            )

    if transaction_type == "tanning_sale":
        minutes_delta = int(payload.get("account_minutes_used") or 0)
        if minutes_delta > 0:
            summary_row = ensure_cloud_customer_minute_summary(db, business_account_public_id, customer_public_id)
            minutes_after = int(summary_row["minutes_available"] or 0) - minutes_delta
            db.execute(
                """
                insert into cloud_customer_minute_ledger (
                    business_account_public_id,
                    customer_public_id,
                    site_public_id,
                    terminal_device_public_id,
                    staff_user_public_id,
                    source_event_id,
                    source_reference,
                    package_code,
                    entry_type,
                    delta_minutes,
                    minutes_after,
                    notes,
                    created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    business_account_public_id,
                    customer_public_id,
                    site_public_id,
                    terminal_device_public_id,
                    staff_user_public_id,
                    local_event_id,
                    transaction_number,
                    "",
                    "minutes_redeemed",
                    -minutes_delta,
                    minutes_after,
                    notes,
                    occurred_at,
                ),
            )
            db.execute(
                """
                update cloud_customer_minute_summary
                set minutes_available = ?,
                    updated_at = current_timestamp
                where customer_public_id = ?
                """,
                (minutes_after, customer_public_id),
            )

    if transaction_type == "account_topup":
        amount_delta = float(payload.get("total_amount") or 0)
        if amount_delta > 0:
            summary_row = ensure_cloud_customer_balance_summary(db, business_account_public_id, customer_public_id)
            balance_after = round(float(summary_row["current_balance"] or 0) + amount_delta, 2)
            db.execute(
                """
                insert into cloud_customer_balance_ledger (
                    business_account_public_id,
                    customer_public_id,
                    site_public_id,
                    terminal_device_public_id,
                    staff_user_public_id,
                    source_event_id,
                    source_reference,
                    entry_type,
                    delta_amount,
                    balance_after,
                    currency_code,
                    notes,
                    created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'GBP', ?, ?)
                """,
                (
                    business_account_public_id,
                    customer_public_id,
                    site_public_id,
                    terminal_device_public_id,
                    staff_user_public_id,
                    local_event_id,
                    transaction_number,
                    "account_topup",
                    f"{amount_delta:.2f}",
                    f"{balance_after:.2f}",
                    notes,
                    occurred_at,
                ),
            )
            db.execute(
                """
                update cloud_customer_balance_summary
                set current_balance = ?,
                    updated_at = current_timestamp
                where customer_public_id = ?
                """,
                (f"{balance_after:.2f}", customer_public_id),
            )


def ensure_cloud_site_record(business_account_public_id: str, site_public_id: str, site_name: str, site_code: str):
    existing = platform_query_one(
        "select id from cloud_business_sites where site_public_id = ?",
        (site_public_id,),
    )
    if existing is None:
        platform_execute(
            """
            insert into cloud_business_sites (
                business_account_public_id,
                site_public_id,
                site_name,
                site_code,
                status
            ) values (?, ?, ?, ?, 'active')
            """,
            (business_account_public_id, site_public_id, site_name, site_code),
        )
        return
    platform_execute(
        """
        update cloud_business_sites
        set business_account_public_id = ?,
            site_name = ?,
            site_code = ?,
            updated_at = current_timestamp
        where site_public_id = ?
        """,
        (business_account_public_id, site_name, site_code, site_public_id),
    )


def ensure_cloud_terminal_record(
    business_account_public_id: str,
    site_public_id: str,
    terminal_device_public_id: str,
    terminal_name: str,
    install_mode: str = "fresh_install",
):
    existing = platform_query_one(
        "select id from cloud_terminal_registry where terminal_device_public_id = ?",
        (terminal_device_public_id,),
    )
    if existing is None:
        platform_execute(
            """
            insert into cloud_terminal_registry (
                business_account_public_id,
                site_public_id,
                terminal_device_public_id,
                terminal_name,
                pairing_code,
                install_mode,
                status
            ) values (?, ?, ?, ?, ?, ?, 'paired')
            """,
            (
                business_account_public_id,
                site_public_id,
                terminal_device_public_id,
                terminal_name,
                make_pairing_code(),
                str(install_mode or "fresh_install"),
            ),
        )
        return
    platform_execute(
        """
        update cloud_terminal_registry
        set business_account_public_id = ?,
            site_public_id = ?,
            terminal_name = ?,
            install_mode = ?,
            updated_at = current_timestamp
        where terminal_device_public_id = ?
        """,
        (
            business_account_public_id,
            site_public_id,
            terminal_name,
            str(install_mode or "fresh_install"),
            terminal_device_public_id,
        ),
    )


def mark_cloud_terminal_seen(terminal_device_public_id: str, seen_at: str, app_version: str = ""):
    platform_execute(
        """
        update cloud_terminal_registry
        set status = 'paired',
            app_version_reported = case when ? != '' then ? else app_version_reported end,
            last_seen_at = case when coalesce(last_seen_at, '') < ? then ? else last_seen_at end,
            updated_at = current_timestamp
        where terminal_device_public_id = ?
        """,
        (str(app_version or "").strip(), str(app_version or "").strip(), seen_at, seen_at, terminal_device_public_id),
    )


def ensure_cloud_runtime_terminal_record(
    *,
    business_account_public_id: str,
    terminal_device_public_id: str,
    payload: dict | None = None,
    app_version: str = "",
    seen_at: str = "",
):
    payload = payload if isinstance(payload, dict) else {}
    existing_terminal = platform_query_one(
        "select * from cloud_terminal_registry where terminal_device_public_id = ?",
        (terminal_device_public_id,),
    )

    site_public_id = str((existing_terminal["site_public_id"] if existing_terminal else "") or "").strip()
    if not site_public_id:
        site_row = platform_query_one(
            """
            select *
            from cloud_business_sites
            where business_account_public_id = ?
              and status != 'deleted'
            order by case when status = 'active' then 0 else 1 end, updated_at desc, site_name
            limit 1
            """,
            (business_account_public_id,),
        )
        if site_row is not None:
            site_public_id = str(site_row["site_public_id"] or "").strip()
        else:
            fallback_site_name = "Imported Main Site"
            fallback_site_code = "IMPORTED"
            site_public_id = make_public_id("site", f"{business_account_public_id}-imported-main")
            suffix = 2
            while platform_query_one(
                "select id from cloud_business_sites where site_public_id = ?",
                (site_public_id,),
            ) is not None:
                site_public_id = f"{make_public_id('site', f'{business_account_public_id}-imported-main')}-{suffix}"
                suffix += 1
            ensure_cloud_site_record(
                business_account_public_id,
                site_public_id,
                fallback_site_name,
                fallback_site_code,
            )

    terminal_name = (
        str(payload.get("terminal_name") or "").strip()
        or str((existing_terminal["terminal_name"] if existing_terminal else "") or "").strip()
        or str(terminal_device_public_id or "").strip()
        or "Imported Till"
    )
    install_mode = str((existing_terminal["install_mode"] if existing_terminal else "") or "fresh_install").strip() or "fresh_install"

    ensure_cloud_terminal_record(
        business_account_public_id=business_account_public_id,
        site_public_id=site_public_id,
        terminal_device_public_id=terminal_device_public_id,
        terminal_name=terminal_name,
        install_mode=install_mode,
    )
    if seen_at:
        mark_cloud_terminal_seen(terminal_device_public_id, seen_at, app_version=app_version)


def update_cloud_terminal_sync_health(terminal_device_public_id: str, sync_health: dict | None):
    sync_health = sync_health if isinstance(sync_health, dict) else {}
    sync_status = str(sync_health.get("sync_status") or "healthy").strip() or "healthy"
    sync_pending_count = int(sync_health.get("pending_count") or 0)
    sync_failed_count = int(sync_health.get("failed_count") or 0)
    sync_oldest_outstanding_at = str(sync_health.get("oldest_outstanding_at") or "").strip() or None
    sync_last_attempt_at = str(sync_health.get("last_attempt_at") or "").strip() or None
    sync_last_acknowledged_at = str(sync_health.get("last_acknowledged_at") or "").strip() or None
    sync_last_checkpoint_at = str(sync_health.get("last_checkpoint_at") or "").strip() or None
    recent_failed_items = sync_health.get("recent_failed_items") if isinstance(sync_health.get("recent_failed_items"), list) else []
    recent_pending_items = sync_health.get("recent_pending_items") if isinstance(sync_health.get("recent_pending_items"), list) else []
    platform_execute(
        """
        update cloud_terminal_registry
        set sync_status = ?,
            sync_pending_count = ?,
            sync_failed_count = ?,
            sync_oldest_outstanding_at = ?,
            sync_last_attempt_at = ?,
            sync_last_acknowledged_at = ?,
            sync_last_checkpoint_at = ?,
            sync_recent_failures_json = ?,
            sync_recent_pending_json = ?,
            updated_at = current_timestamp
        where terminal_device_public_id = ?
        """,
        (
            sync_status,
            sync_pending_count,
            sync_failed_count,
            sync_oldest_outstanding_at,
            sync_last_attempt_at,
            sync_last_acknowledged_at,
            sync_last_checkpoint_at,
            json.dumps(recent_failed_items, separators=(",", ":")),
            json.dumps(recent_pending_items, separators=(",", ":")),
            terminal_device_public_id,
        ),
    )


def reconcile_runtime_terminals_for_business(business_account_public_id: str):
    business_account_public_id = str(business_account_public_id or "").strip()
    if not business_account_public_id:
        return

    licence_rows = platform_query_all(
        """
        select terminal_device_public_id, last_check_in_at
        from cloud_device_licences
        where business_account_public_id = ?
        """,
        (business_account_public_id,),
    )
    event_rows = platform_query_all(
        """
        select terminal_device_public_id, payload_json, received_at
        from cloud_sync_events
        where business_account_public_id = ?
        order by received_at desc, id desc
        """,
        (business_account_public_id,),
    )

    terminal_payload_map = {}
    terminal_seen_map = {}
    for row in event_rows:
        terminal_id = str(row["terminal_device_public_id"] or "").strip()
        if not terminal_id:
            continue
        if terminal_id not in terminal_payload_map:
            payload = {}
            try:
                payload = json.loads(row["payload_json"] or "{}")
                if not isinstance(payload, dict):
                    payload = {}
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            terminal_payload_map[terminal_id] = payload
            terminal_seen_map[terminal_id] = str(row["received_at"] or "").strip()

    for row in licence_rows:
        terminal_id = str(row["terminal_device_public_id"] or "").strip()
        if not terminal_id:
            continue
        terminal_seen_map[terminal_id] = max(terminal_seen_map.get(terminal_id, ""), str(row["last_check_in_at"] or "").strip())

    terminal_ids = sorted(set(terminal_payload_map) | set(terminal_seen_map))
    for terminal_id in terminal_ids:
        existing = platform_query_one("select id from cloud_terminal_registry where terminal_device_public_id = ?", (terminal_id,))
        if existing:
            if terminal_seen_map.get(terminal_id):
                mark_cloud_terminal_seen(terminal_id, terminal_seen_map[terminal_id])
            continue
        ensure_cloud_runtime_terminal_record(
            business_account_public_id=business_account_public_id,
            terminal_device_public_id=terminal_id,
            payload=terminal_payload_map.get(terminal_id, {}),
            seen_at=terminal_seen_map.get(terminal_id, ""),
        )


def upsert_cloud_device_licence(
    *,
    business_account_public_id: str,
    terminal_device_public_id: str,
    licence_status: str,
    signed_token: str,
    issued_at: str,
    expires_at: str,
    last_check_in_at: str,
):
    db = get_platform_db()
    existing = db.execute(
        "select id from cloud_device_licences where terminal_device_public_id = ?",
        (terminal_device_public_id,),
    ).fetchone()
    if existing is None:
        db.execute(
            """
            insert into cloud_device_licences (
                business_account_public_id,
                terminal_device_public_id,
                licence_status,
                signed_token,
                issued_at,
                expires_at,
                last_check_in_at
            ) values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                business_account_public_id,
                terminal_device_public_id,
                str(licence_status or "active").strip() or "active",
                signed_token,
                issued_at,
                expires_at,
                last_check_in_at,
            ),
        )
    else:
        db.execute(
            """
            update cloud_device_licences
            set business_account_public_id = ?,
                licence_status = ?,
                signed_token = ?,
                issued_at = ?,
                expires_at = ?,
                last_check_in_at = ?
            where terminal_device_public_id = ?
            """,
            (
                business_account_public_id,
                str(licence_status or "active").strip() or "active",
                signed_token,
                issued_at,
                expires_at,
                last_check_in_at,
                terminal_device_public_id,
            ),
        )
    db.commit()


def ensure_sunbed_tables():
    db = get_db()
    db.execute(
        """
        create table if not exists sunbeds (
            id integer primary key autoincrement,
            site_id integer not null,
            terminal_id integer,
            room_number integer not null default 1,
            room_name text not null default 'Room 1',
            bed_name text not null default 'Bed 1',
            display_name text not null default 'Room 1 - Bed 1',
            manufacturer text not null default '',
            model text not null default '',
            default_catalogue_image_file text not null default '',
            bed_type text not null default 'lay_down',
            customer_display_image_path text not null default '',
            total_minutes_used integer not null default 0,
            retube_minutes_used integer not null default 0,
            session_count integer not null default 0,
            last_retube_reset_at text,
            is_active integer not null default 1,
            created_at text not null default current_timestamp,
            updated_at text not null default current_timestamp
        )
        """
    )
    db.execute(
        """
        create table if not exists sunbed_usage_log (
            id integer primary key autoincrement,
            sunbed_id integer not null,
            customer_id integer,
            transaction_id integer,
            minutes_used integer not null default 0,
            payment_method text not null default '',
            created_at text not null default current_timestamp
        )
        """
    )
    db.execute(
        """
        create table if not exists sunbed_model_catalogue (
            id integer primary key autoincrement,
            manufacturer text not null,
            model text not null,
            default_image_file text not null default '',
            sort_order integer not null default 0,
            is_active integer not null default 1,
            created_at text not null default current_timestamp,
            updated_at text not null default current_timestamp
        )
        """
    )
    db.execute(
        """
        create unique index if not exists idx_sunbed_model_catalogue_unique
        on sunbed_model_catalogue (manufacturer, model)
        """
    )
    existing_columns = {
        row["name"]
        for row in db.execute("pragma table_info('sunbeds')").fetchall()
    }
    columns_to_add = {
        "manufacturer": "text not null default ''",
        "model": "text not null default ''",
        "default_catalogue_image_file": "text not null default ''",
    }
    for column_name, column_sql in columns_to_add.items():
        if column_name not in existing_columns:
            db.execute(f"alter table sunbeds add column {column_name} {column_sql}")
    db.commit()
    seed_sunbed_model_catalogue()


def load_sunbed_image_seed_map() -> dict[tuple[str, str], str]:
    if not SUNBED_IMAGE_PACK_CSV_PATH.exists():
        return {}
    image_map = {}
    with SUNBED_IMAGE_PACK_CSV_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            manufacturer = str(row.get("Manufacturer") or "").strip()
            model = str(row.get("Model") or "").strip()
            image_file = str(row.get("ImageFile") or "").strip()
            if manufacturer and model:
                image_map[(manufacturer, model)] = image_file
    return image_map


def load_sunbed_catalogue_seed_rows() -> list[dict]:
    if not SUNBED_CATALOGUE_CSV_PATH.exists():
        return []
    image_map = load_sunbed_image_seed_map()
    rows = []
    seen = set()
    with SUNBED_CATALOGUE_CSV_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for index, row in enumerate(reader, start=1):
            manufacturer = str(row.get("Manufacturer") or "").strip()
            model = str(row.get("Model") or "").strip()
            if not manufacturer or not model:
                continue
            key = (manufacturer, model)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "manufacturer": manufacturer,
                    "model": model,
                    "default_image_file": image_map.get(key, ""),
                    "sort_order": index,
                }
            )
    return rows


def seed_sunbed_model_catalogue():
    db = get_db()
    existing = {
        (row["manufacturer"], row["model"]): row
        for row in db.execute(
            "select id, manufacturer, model, default_image_file from sunbed_model_catalogue"
        ).fetchall()
    }
    for row in load_sunbed_catalogue_seed_rows():
        key = (row["manufacturer"], row["model"])
        existing_row = existing.get(key)
        if existing_row is None:
            db.execute(
                """
                insert into sunbed_model_catalogue (
                    manufacturer,
                    model,
                    default_image_file,
                    sort_order,
                    is_active
                ) values (?, ?, ?, ?, 1)
                """,
                (
                    row["manufacturer"],
                    row["model"],
                    row["default_image_file"],
                    int(row["sort_order"]),
                ),
            )
            continue
        if str(existing_row["default_image_file"] or "") != row["default_image_file"]:
            db.execute(
                """
                update sunbed_model_catalogue
                set default_image_file = ?,
                    updated_at = current_timestamp
                where id = ?
                """,
                (row["default_image_file"], int(existing_row["id"])),
            )
    db.commit()


def sunbed_catalogue_rows():
    ensure_sunbed_tables()
    rows = query_all(
        """
        select manufacturer, model, default_image_file
        from sunbed_model_catalogue
        where is_active = 1
        order by
            case when manufacturer = 'Custom' then 1 else 0 end,
            manufacturer,
            sort_order,
            model
        """
    )
    prepared = [dict(row) for row in rows]
    by_manufacturer = {}
    for row in prepared:
        by_manufacturer.setdefault(row["manufacturer"], []).append(row)
    return prepared, by_manufacturer


def default_catalogue_image_path(image_file: str) -> str:
    filename = str(image_file or "").strip()
    if not filename:
        return ""
    candidate = SUNBED_CATALOGUE_DIR / filename
    if candidate.exists():
        return f"/static/sunbed-catalogue/{filename}"
    return ""


def default_sunbed_label(bed_number: int) -> str:
    return f"Bed {int(bed_number)}"


def default_legacy_sunbed_labels(bed_number: int) -> set[str]:
    bed_text = default_sunbed_label(bed_number)
    return {
        "",
        bed_text,
        f"Room {bed_number}",
        f"Room {bed_number} - Bed {bed_number}",
    }


def custom_sunbed_name_from_row(row) -> str:
    bed_number = int(row["room_number"] or 1)
    for value in (row["display_name"], row["bed_name"], row["room_name"]):
        text = str(value or "").strip()
        if text and text not in default_legacy_sunbed_labels(bed_number):
            return text
    return ""


def ensure_basic_sunbed_defaults():
    ensure_sunbed_tables()
    db = get_db()
    rows = db.execute("select id, room_number, room_name, bed_name, display_name from sunbeds").fetchall()
    for row in rows:
        bed_number = int(row["room_number"] or 1)
        basic_label = default_sunbed_label(bed_number)
        custom_name = custom_sunbed_name_from_row(row)
        db.execute(
            """
            update sunbeds
            set room_name = ?,
                bed_name = ?,
                display_name = ?,
                updated_at = current_timestamp
            where id = ?
            """,
            (
                basic_label,
                custom_name or basic_label,
                custom_name or basic_label,
                int(row["id"]),
            ),
        )
    db.commit()


def seed_sunbeds():
    ensure_sunbed_tables()
    for site in query_all("select * from sites where is_active = 1 order by id"):
        existing = query_one("select count(*) as value from sunbeds where site_id = ?", (site["id"],))
        if existing and existing["value"]:
            continue
        for room_number in range(1, 9):
            room_name = f"Bed {room_number}"
            bed_name = f"Bed {room_number}"
            display_name = f"Bed {room_number}"
            execute(
                """
                insert into sunbeds (
                    site_id,
                    terminal_id,
                    room_number,
                    room_name,
                    bed_name,
                    display_name,
                    bed_type,
                    customer_display_image_path,
                    total_minutes_used,
                    retube_minutes_used,
                    session_count,
                    last_retube_reset_at,
                    is_active
                ) values (?, null, ?, ?, ?, ?, 'lay_down', '', 0, 0, 0, current_timestamp, 1)
                """,
                (site["id"], room_number, room_name, bed_name, display_name),
            )


def sunbed_rows():
    ensure_sunbed_tables()
    rows = query_all(
        """
        select
            sunbeds.*,
            sites.name as site_name,
            coalesce(sites.is_active, 0) as site_is_active
        from sunbeds
        left join sites on sites.id = sunbeds.site_id
        order by
            case when coalesce(sites.is_active, 0) = 1 then 0 else 1 end,
            sunbeds.room_number,
            case when sunbeds.is_active = 1 then 0 else 1 end,
            sunbeds.updated_at desc,
            sunbeds.id desc
        """
    )
    prepared = []
    seen_beds = set()
    for row in rows:
        row_dict = dict(row)
        bed_number = int(row["room_number"] or 1)
        if bed_number in seen_beds:
            continue
        seen_beds.add(bed_number)
        row_dict["bed_number"] = bed_number
        row_dict["default_label"] = default_sunbed_label(bed_number)
        row_dict["custom_name"] = custom_sunbed_name_from_row(row)
        row_dict["default_catalogue_image_path"] = default_catalogue_image_path(
            row["default_catalogue_image_file"]
        )
        prepared.append(row_dict)
    return prepared


def business_settings_row():
    ensure_business_settings_table()
    row = query_one("select * from business_settings where id = 1")
    merged = dict(row)
    merged["business_name"] = row["business_name"] or "Your Salon"
    merged["platform_brand_name"] = "Salon Max"
    return merged


def seed_local_business_account_record():
    if APP_ROLE == "cloud":
        return
    ensure_platform_sync_tables()
    demo_row = platform_query_one(
        "select id from cloud_business_accounts where business_account_public_id = 'biz_demo'"
    )
    if demo_row is None:
        return
    has_sites = platform_query_one(
        "select id from cloud_business_sites where business_account_public_id = 'biz_demo' limit 1"
    )
    has_terminals = platform_query_one(
        "select id from cloud_terminal_registry where business_account_public_id = 'biz_demo' limit 1"
    )
    has_licences = platform_query_one(
        "select id from cloud_device_licences where business_account_public_id = 'biz_demo' limit 1"
    )
    has_events = platform_query_one(
        "select id from cloud_sync_events where business_account_public_id = 'biz_demo' limit 1"
    )
    if has_sites or has_terminals or has_licences or has_events:
        return
    platform_execute("delete from cloud_business_accounts where business_account_public_id = 'biz_demo'")


def salonmax_business_accounts_snapshot(search_text: str = ""):
    seed_local_business_account_record()
    search_text = str(search_text or "").strip().lower()
    rows = platform_query_all(
        """
        select *
        from cloud_business_accounts
        where status != 'deleted'
          and coalesce(product_type, 'salon') = 'salon'
        order by business_name, business_account_public_id
        """
    )
    archived_rows = platform_query_all(
        """
        select *
        from cloud_business_accounts
        where status = 'deleted'
          and coalesce(product_type, 'salon') = 'salon'
        order by updated_at desc, business_name, business_account_public_id
        """
    )
    businesses = []
    archived_businesses = []
    now_utc = datetime.now(ZoneInfo("UTC"))
    healthy_terminal_total = 0
    stale_terminal_total = 0
    expiring_terminal_total = 0
    unlicensed_terminal_total = 0
    paired_terminal_total = 0
    ready_terminal_total = 0
    unhealthy_business_count = 0
    setup_incomplete_business_count = 0
    for row in rows:
        account_id = str(row["business_account_public_id"])
        business_name = str(row["business_name"] or "")
        backoffice_tenant_id = f"bo_{account_id}"
        searchable = " ".join(
            [
                business_name,
                account_id,
                backoffice_tenant_id,
                str(row["contact_name"] or ""),
                str(row["contact_email"] or ""),
                str(row["contact_phone"] or ""),
                str(row["billing_email"] or ""),
                str(row["company_number"] or ""),
                str(row["city"] or ""),
                str(row["postcode"] or ""),
            ]
        ).lower()
        if search_text and search_text not in searchable:
            continue
        reconcile_runtime_terminals_for_business(account_id)
        site_count = platform_query_one(
            "select count(*) as value from cloud_business_sites where business_account_public_id = ? and status != 'deleted'",
            (account_id,),
        )["value"]
        active_site_count = platform_query_one(
            "select count(*) as value from cloud_business_sites where business_account_public_id = ? and status = 'active'",
            (account_id,),
        )["value"]
        suspended_site_count = platform_query_one(
            "select count(*) as value from cloud_business_sites where business_account_public_id = ? and status = 'suspended'",
            (account_id,),
        )["value"]
        sync_event_count = platform_query_one(
            "select count(*) as value from cloud_sync_events where business_account_public_id = ?",
            (account_id,),
        )["value"]
        terminal_count = platform_query_one(
            "select count(*) as value from cloud_terminal_registry where business_account_public_id = ? and management_status != 'retired'",
            (account_id,),
        )["value"]
        active_terminal_count = platform_query_one(
            "select count(*) as value from cloud_terminal_registry where business_account_public_id = ? and management_status = 'active'",
            (account_id,),
        )["value"]
        suspended_terminal_count = platform_query_one(
            "select count(*) as value from cloud_terminal_registry where business_account_public_id = ? and management_status = 'suspended'",
            (account_id,),
        )["value"]
        last_check_in_row = platform_query_one(
            """
            select max(last_check_in_at) as value
            from cloud_device_licences
            where business_account_public_id = ?
            """,
            (account_id,),
        )
        terminal_rows = platform_query_all(
            """
            select *
            from cloud_terminal_registry
            where business_account_public_id = ?
              and management_status != 'retired'
            """,
            (account_id,),
        )
        licence_rows = platform_query_all(
            """
            select terminal_device_public_id, licence_status, expires_at, last_check_in_at
            from cloud_device_licences
            where business_account_public_id = ?
            order by last_check_in_at desc, terminal_device_public_id
            """,
            (account_id,),
        )
        staff_bootstrap_count = platform_query_one(
            """
            select count(*) as value
            from cloud_sync_events
            where business_account_public_id = ?
              and event_type = 'staff.bootstrap_completed'
            """,
            (account_id,),
        )["value"]
        paired_count = sum(1 for terminal in terminal_rows if str(terminal["status"] or "") == "paired")
        ready_terminal_count = min(int(staff_bootstrap_count or 0), len(terminal_rows))
        terminal_ids = {
            str(terminal["terminal_device_public_id"] or "")
            for terminal in terminal_rows
        }
        licensed_terminal_ids = {
            str(licence["terminal_device_public_id"] or "")
            for licence in licence_rows
        }
        unlicensed_terminal_count = len(terminal_ids - licensed_terminal_ids)
        healthy_terminal_count = 0
        stale_terminal_count = 0
        expiring_terminal_count = 0
        expired_terminal_count = 0
        for licence in licence_rows:
            check_in_dt = parse_utc_text(licence["last_check_in_at"])
            expires_dt = parse_utc_text(licence["expires_at"])
            minutes_since_check_in = None
            if check_in_dt is not None:
                minutes_since_check_in = int((now_utc - check_in_dt).total_seconds() // 60)
            hours_to_expiry = None
            if expires_dt is not None:
                hours_to_expiry = int((expires_dt - now_utc).total_seconds() // 3600)
            is_stale = minutes_since_check_in is None or minutes_since_check_in > 30
            is_expired = hours_to_expiry is not None and hours_to_expiry < 0
            is_expiring = hours_to_expiry is not None and 0 <= hours_to_expiry <= 72
            if is_expired:
                expired_terminal_count += 1
            elif is_expiring:
                expiring_terminal_count += 1
            elif is_stale:
                stale_terminal_count += 1
            elif str(licence["licence_status"] or "").strip().lower() == "active":
                healthy_terminal_count += 1
        onboarding_stage = "business_created"
        onboarding_label = "Business Created"
        if int(site_count or 0) > 0:
            onboarding_stage = "site_added"
            onboarding_label = "Site Added"
        if int(terminal_count or 0) > 0:
            onboarding_stage = "terminal_provisioned"
            onboarding_label = "Terminal Provisioned"
        if paired_count > 0:
            onboarding_stage = "paired"
            onboarding_label = "Pi Paired"
        if ready_terminal_count > 0:
            onboarding_stage = "ready_to_trade"
            onboarding_label = "Ready To Trade"
        account_status = str(row["status"] or "active")
        operational_status = "active"
        operational_label = "Operational"
        if int(site_count or 0) == 0:
            operational_status = "warning"
            operational_label = "No Active Estate"
        elif int(active_site_count or 0) == 0 and int(suspended_site_count or 0) > 0:
            operational_status = "suspended"
            operational_label = "Sites Suspended"
        elif int(active_terminal_count or 0) == 0 and int(suspended_terminal_count or 0) > 0:
            operational_status = "warning"
            operational_label = "Terminals Suspended"
        health_status = "good"
        health_label = "Healthy"
        if int(terminal_count or 0) == 0:
            health_status = "neutral"
            health_label = "No Terminals"
        elif expired_terminal_count > 0:
            health_status = "danger"
            health_label = "Expired Licence"
        elif unlicensed_terminal_count > 0:
            health_status = "danger"
            health_label = "Unlicensed Terminal"
        elif stale_terminal_count > 0:
            health_status = "warning"
            health_label = "Check-In Stale"
        elif expiring_terminal_count > 0:
            health_status = "warning"
            health_label = "Licence Expiring"
        elif onboarding_stage != "ready_to_trade":
            health_status = "neutral"
            health_label = "Setup Incomplete"
        healthy_terminal_total += healthy_terminal_count
        stale_terminal_total += stale_terminal_count
        expiring_terminal_total += expiring_terminal_count
        unlicensed_terminal_total += unlicensed_terminal_count
        paired_terminal_total += paired_count
        ready_terminal_total += ready_terminal_count
        if health_status in {"warning", "danger"}:
            unhealthy_business_count += 1
        if onboarding_stage != "ready_to_trade":
            setup_incomplete_business_count += 1
        businesses.append(
            {
                "business_account_public_id": account_id,
                "backoffice_tenant_id": backoffice_tenant_id,
                "business_name": business_name,
                "status": row["status"],
                "account_status": account_status,
                "operational_status": operational_status,
                "operational_label": operational_label,
                "subscription_plan": row["subscription_plan"],
                "subscription_status": row["subscription_status"],
                "site_count": int(site_count or 0),
                "active_site_count": int(active_site_count or 0),
                "suspended_site_count": int(suspended_site_count or 0),
                "terminal_count": int(terminal_count or 0),
                "active_terminal_count": int(active_terminal_count or 0),
                "suspended_terminal_count": int(suspended_terminal_count or 0),
                "sync_event_count": int(sync_event_count or 0),
                "last_check_in_at": (last_check_in_row["value"] if last_check_in_row else "") or "",
                "paired_terminal_count": paired_count,
                "ready_terminal_count": ready_terminal_count,
                "staff_bootstrap_count": int(staff_bootstrap_count or 0),
                "onboarding_stage": onboarding_stage,
                "onboarding_label": onboarding_label,
                "health_status": health_status,
                "health_label": health_label,
                "healthy_terminal_count": healthy_terminal_count,
                "stale_terminal_count": stale_terminal_count,
                "expiring_terminal_count": expiring_terminal_count,
                "expired_terminal_count": expired_terminal_count,
                "unlicensed_terminal_count": unlicensed_terminal_count,
            }
        )
    for row in archived_rows:
        account_id = str(row["business_account_public_id"] or "")
        business_name = str(row["business_name"] or account_id)
        backoffice_tenant_id = f"bo_{account_id}"
        searchable = " ".join(
            [
                business_name,
                account_id,
                backoffice_tenant_id,
                str(row["contact_name"] or ""),
                str(row["contact_email"] or ""),
                str(row["contact_phone"] or ""),
                str(row["billing_email"] or ""),
                str(row["company_number"] or ""),
                str(row["city"] or ""),
                str(row["postcode"] or ""),
            ]
        ).lower()
        if search_text and search_text not in searchable:
            continue
        archived_businesses.append(
            {
                "business_account_public_id": account_id,
                "business_name": business_name,
                "backoffice_tenant_id": backoffice_tenant_id,
                "subscription_plan": str(row["subscription_plan"] or "pilot"),
                "subscription_status": str(row["subscription_status"] or "active"),
                "site_count": int(
                    platform_query_one(
                        "select count(*) as value from cloud_business_sites where business_account_public_id = ?",
                        (account_id,),
                    )["value"]
                ),
                "terminal_count": int(
                    platform_query_one(
                        "select count(*) as value from cloud_terminal_registry where business_account_public_id = ?",
                        (account_id,),
                    )["value"]
                ),
                "sync_event_count": int(
                    platform_query_one(
                        "select count(*) as value from cloud_sync_events where business_account_public_id = ?",
                        (account_id,),
                    )["value"]
                ),
            }
        )
    return {
        "platform_name": "Salon Max",
        "search_text": search_text,
        "business_count": len(businesses),
        "healthy_terminal_total": healthy_terminal_total,
        "stale_terminal_total": stale_terminal_total,
        "expiring_terminal_total": expiring_terminal_total,
        "unlicensed_terminal_total": unlicensed_terminal_total,
        "paired_terminal_total": paired_terminal_total,
        "ready_terminal_total": ready_terminal_total,
        "unhealthy_business_count": unhealthy_business_count,
        "setup_incomplete_business_count": setup_incomplete_business_count,
        "businesses": businesses,
        "archived_businesses": archived_businesses,
    }


def salonmax_platform_snapshot(business_account_public_id=None):
    seed_local_business_account_record()
    if business_account_public_id:
        business_account_public_id = str(business_account_public_id).strip()
    else:
        first_account = platform_query_one(
            "select business_account_public_id from cloud_business_accounts order by business_name, business_account_public_id limit 1"
        )
        business_account_public_id = str(first_account["business_account_public_id"]) if first_account else ""
    account_row = platform_query_one(
        "select * from cloud_business_accounts where business_account_public_id = ?",
        (business_account_public_id,),
    )
    if account_row is None:
        return None
    reconcile_runtime_terminals_for_business(business_account_public_id)
    sites = platform_query_all(
        """
        select *
        from cloud_business_sites
        where business_account_public_id = ?
          and status != 'deleted'
        order by site_name
        """,
        (business_account_public_id,),
    )
    archived_sites = platform_query_all(
        """
        select *
        from cloud_business_sites
        where business_account_public_id = ?
          and status = 'deleted'
        order by updated_at desc, site_name
        """,
        (business_account_public_id,),
    )
    terminals = platform_query_all(
        """
        select *
        from cloud_terminal_registry
        where business_account_public_id = ?
          and management_status != 'retired'
        order by site_public_id, terminal_name
        """,
        (business_account_public_id,),
    )
    site_name_map = {
        str(site["site_public_id"]): str(site["site_name"] or "")
        for site in sites
    }
    devices = []
    licences = platform_query_all(
        """
        select *
        from cloud_device_licences
        where business_account_public_id = ?
        order by last_check_in_at desc, terminal_device_public_id
        """,
        (business_account_public_id,),
    )
    recent_events = platform_query_all(
        """
        select *
        from cloud_sync_events
        where business_account_public_id = ?
        order by id desc
        limit 20
        """,
        (business_account_public_id,),
    )
    support_note_rows = platform_query_all(
        """
        select *
        from cloud_support_notes
        where business_account_public_id = ?
        order by created_at desc, id desc
        limit 25
        """,
        (business_account_public_id,),
    )
    timeline_events = []
    for event in recent_events:
        event_type = str(event["event_type"] or "").strip()
        payload = {}
        try:
            payload = json.loads(event["payload_json"] or "{}")
            if not isinstance(payload, dict):
                payload = {}
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        timeline_label = event_type.replace(".", " ").replace("_", " ").title() or "Event"
        timeline_summary = ""
        if event_type == "transaction.completed":
            timeline_label = "Sale Synced"
            transaction_number = str(payload.get("transaction_number") or "").strip()
            transaction_type = str(payload.get("transaction_type") or "").replace("_", " ").title()
            total_amount = payload.get("total_amount")
            amount_text = ""
            if total_amount not in (None, ""):
                amount_text = f"GBP {float(total_amount):.2f}"
            timeline_summary = " | ".join(part for part in [transaction_number, transaction_type, amount_text] if part)
        elif event_type == "staff.bootstrap_completed":
            timeline_label = "First Staff Created"
            staff_name = str(payload.get("staff_name") or "").strip()
            staff_role = str(payload.get("staff_role") or "").replace("_", " ").title()
            timeline_summary = " | ".join(part for part in [staff_name, staff_role] if part)
        timeline_events.append(
            {
                "local_event_id": str(event["local_event_id"] or ""),
                "terminal_device_public_id": str(event["terminal_device_public_id"] or ""),
                "event_type": event_type,
                "label": timeline_label,
                "summary": timeline_summary,
                "received_at": str(event["received_at"] or ""),
                "occurred_at": str(event["occurred_at"] or ""),
            }
        )
    licence_timeline = []
    for licence in licences:
        licence_timeline.append(
            {
                "kind": "licence",
                "label": "Licence Check-In",
                "summary": str(licence["licence_status"] or "").title(),
                "terminal_device_public_id": str(licence["terminal_device_public_id"] or ""),
                "event_id": str(licence["signed_token"] or ""),
                "occurred_at": str(licence["last_check_in_at"] or ""),
            }
        )
    support_note_timeline = []
    for note in support_note_rows:
        support_note_timeline.append(
            {
                "kind": "support_note",
                "label": str(note["note_type"] or "support_note").replace("_", " ").title(),
                "summary": str(note["note_text"] or ""),
                "terminal_device_public_id": str(note["terminal_device_public_id"] or ""),
                "event_id": f"note_{note['id']}",
                "occurred_at": str(note["created_at"] or ""),
                "author_name": str(note["author_name"] or ""),
            }
        )
    support_timeline = timeline_events + licence_timeline + support_note_timeline
    support_timeline.sort(key=lambda item: str(item.get("occurred_at") or item.get("received_at") or ""), reverse=True)
    support_timeline = support_timeline[:25]
    customer_rows = platform_query_all(
        """
        select *
        from cloud_customers
        where business_account_public_id = ?
        order by last_name, first_name, customer_number
        limit 50
        """,
        (business_account_public_id,),
    )
    customer_minute_summary_rows = platform_query_all(
        """
        select *
        from cloud_customer_minute_summary
        where business_account_public_id = ?
        """,
        (business_account_public_id,),
    )
    customer_balance_summary_rows = platform_query_all(
        """
        select *
        from cloud_customer_balance_summary
        where business_account_public_id = ?
        """,
        (business_account_public_id,),
    )
    minute_summary_map = {
        str(row["customer_public_id"]): int(row["minutes_available"] or 0)
        for row in customer_minute_summary_rows
    }
    balance_summary_map = {
        str(row["customer_public_id"]): str(row["current_balance"] or "0.00")
        for row in customer_balance_summary_rows
    }
    prepared_customers = []
    for row in customer_rows:
        customer_public_id = str(row["customer_public_id"] or "")
        full_name = " ".join(part for part in [str(row["first_name"] or "").strip(), str(row["last_name"] or "").strip()] if part).strip()
        prepared_customers.append(
            {
                "customer_public_id": customer_public_id,
                "customer_number": str(row["customer_number"] or ""),
                "full_name": full_name or customer_public_id,
                "phone": str(row["phone"] or ""),
                "email": str(row["email"] or ""),
                "minutes_available": minute_summary_map.get(customer_public_id, 0),
                "current_balance": balance_summary_map.get(customer_public_id, "0.00"),
                "status": str(row["status"] or "active"),
            }
        )
    sync_event_count = platform_query_one(
        "select count(*) as value from cloud_sync_events where business_account_public_id = ?",
        (business_account_public_id,),
    )["value"]
    sync_event_count_24h = platform_query_one(
        """
        select count(*) as value
        from cloud_sync_events
        where business_account_public_id = ?
          and datetime(received_at) >= datetime('now', '-1 day')
        """,
        (business_account_public_id,),
    )["value"]
    terminal_sync_rows = platform_query_all(
        """
        select
            terminal_device_public_id,
            count(*) as event_count,
            max(received_at) as last_received_at
        from cloud_sync_events
        where business_account_public_id = ?
        group by terminal_device_public_id
        order by event_count desc, terminal_device_public_id
        """,
        (business_account_public_id,),
    )
    terminal_sync_map = {
        str(row["terminal_device_public_id"]): {
            "event_count": int(row["event_count"] or 0),
            "last_received_at": row["last_received_at"] or "",
        }
        for row in terminal_sync_rows
    }
    bootstrap_rows = platform_query_all(
        """
        select terminal_device_public_id, max(received_at) as received_at
        from cloud_sync_events
        where business_account_public_id = ?
          and event_type = 'staff.bootstrap_completed'
        group by terminal_device_public_id
        """,
        (business_account_public_id,),
    )
    bootstrap_map = {
        str(row["terminal_device_public_id"] or ""): str(row["received_at"] or "")
        for row in bootstrap_rows
    }
    now_utc = datetime.now(ZoneInfo("UTC"))
    last_check_in = ""
    stale_licence_count = 0
    expiring_licence_count = 0
    active_licence_count = 0
    prepared_licences = []
    for licence in licences:
        licence_dict = dict(licence)
        check_in_dt = parse_utc_text(licence["last_check_in_at"])
        expires_dt = parse_utc_text(licence["expires_at"])
        if not last_check_in and licence["last_check_in_at"]:
            last_check_in = licence["last_check_in_at"]
        minutes_since_check_in = None
        if check_in_dt is not None:
            minutes_since_check_in = int((now_utc - check_in_dt).total_seconds() // 60)
        hours_to_expiry = None
        if expires_dt is not None:
            hours_to_expiry = int((expires_dt - now_utc).total_seconds() // 3600)
        is_stale = minutes_since_check_in is None or minutes_since_check_in > 30
        is_expired = hours_to_expiry is not None and hours_to_expiry < 0
        is_expiring = hours_to_expiry is not None and 0 <= hours_to_expiry <= 72
        if is_stale:
            stale_licence_count += 1
        if is_expiring or is_expired:
            expiring_licence_count += 1
        if str(licence["licence_status"]).strip().lower() == "active" and not is_expired:
            active_licence_count += 1
        if is_expired:
            health = "expired"
            health_label = "Expired"
        elif is_expiring:
            health = "warning"
            health_label = "Expiring Soon"
        elif is_stale:
            health = "warning"
            health_label = "Check-In Stale"
        else:
            health = "good"
            health_label = "Healthy"
        sync_info = terminal_sync_map.get(str(licence["terminal_device_public_id"]), {})
        licence_dict.update(
            {
                "health": health,
                "health_label": health_label,
                "minutes_since_check_in": minutes_since_check_in,
                "hours_to_expiry": hours_to_expiry,
                "event_count": sync_info.get("event_count", 0),
                "last_event_at": sync_info.get("last_received_at", ""),
            }
        )
        prepared_licences.append(licence_dict)
    expected_terminal_ids = {
        str(row["terminal_device_public_id"])
        for row in terminals
    }
    licensed_terminal_ids = {str(row["terminal_device_public_id"]) for row in licences}
    missing_terminal_ids = sorted(expected_terminal_ids - licensed_terminal_ids)
    prepared_terminals = []
    for row in terminals:
        row_dict = dict(row)
        terminal_id = str(row["terminal_device_public_id"] or "")
        bootstrap_at = bootstrap_map.get(terminal_id, "")
        status_text = str(row["status"] or "")
        install_mode = str(row["install_mode"] or "fresh_install")
        onboarding_stage = "terminal_provisioned"
        onboarding_label = "Provisioned"
        if status_text == "paired":
            onboarding_stage = "paired"
            onboarding_label = "Pi Paired"
        if bootstrap_at:
            onboarding_stage = "ready_to_trade"
            onboarding_label = "Ready To Trade"
        row_dict["bootstrap_completed_at"] = bootstrap_at
        row_dict["onboarding_stage"] = onboarding_stage
        row_dict["onboarding_label"] = onboarding_label
        row_dict["install_mode"] = install_mode
        row_dict["install_mode_label"] = "Replacement Pi" if install_mode == "replacement_pi" else "Fresh Install"
        row_dict["management_status"] = str(row["management_status"] or "active")
        row_dict["site_name"] = site_name_map.get(str(row["site_public_id"] or ""), str(row["site_public_id"] or ""))
        prepared_terminals.append(row_dict)

    site_summaries = []
    for site in sites:
        site_public_id = str(site["site_public_id"])
        site_terminals = [row for row in prepared_terminals if str(row["site_public_id"]) == site_public_id]
        site_terminal_ids = {
            str(row["terminal_device_public_id"])
            for row in site_terminals
        }
        site_event_count = sum(
            terminal_sync_map.get(terminal_id, {}).get("event_count", 0)
            for terminal_id in site_terminal_ids
        )
        site_summaries.append(
            {
                "name": site["site_name"],
                "code": site["site_code"],
                "status": str(site["status"] or "active"),
                "site_public_id": site_public_id,
                "terminal_count": len(site_terminals),
                "device_count": 0,
                "sync_event_count": site_event_count,
            }
        )
    archived_site_summaries = []
    for site in archived_sites:
        site_public_id = str(site["site_public_id"])
        archived_terminal_count = platform_query_one(
            """
            select count(*) as value
            from cloud_terminal_registry
            where business_account_public_id = ?
              and site_public_id = ?
              and management_status = 'retired'
            """,
            (business_account_public_id, site_public_id),
        )["value"]
        archived_site_summaries.append(
            {
                "name": site["site_name"],
                "code": site["site_code"],
                "status": str(site["status"] or "deleted"),
                "site_public_id": site_public_id,
                "archived_terminal_count": int(archived_terminal_count or 0),
                "updated_at": str(site["updated_at"] or ""),
            }
        )
    return {
        "platform_name": "Salon Max",
        "business_account_public_id": business_account_public_id,
        "backoffice_tenant_id": f"bo_{business_account_public_id}",
        "business_name": account_row["business_name"],
        "contact_name": account_row["contact_name"],
        "contact_email": account_row["contact_email"],
        "contact_phone": account_row["contact_phone"],
        "billing_email": account_row["billing_email"],
        "company_number": account_row["company_number"],
        "address_line_1": account_row["address_line_1"],
        "address_line_2": account_row["address_line_2"],
        "city": account_row["city"],
        "county": account_row["county"],
        "postcode": account_row["postcode"],
        "billing_address_line_1": account_row["billing_address_line_1"],
        "billing_address_line_2": account_row["billing_address_line_2"],
        "billing_city": account_row["billing_city"],
        "billing_county": account_row["billing_county"],
        "billing_postcode": account_row["billing_postcode"],
        "vat_number": account_row["vat_number"],
        "contract_start_date": account_row["contract_start_date"],
        "renewal_date": account_row["renewal_date"],
        "monthly_fee": account_row["monthly_fee"],
        "notes": account_row["notes"],
        "status": account_row["status"],
        "subscription_plan": account_row["subscription_plan"],
        "subscription_status": account_row["subscription_status"],
        "site_count": len(sites),
        "terminal_count": len(terminals),
        "device_count": 0,
        "licence_count": len(licences),
        "active_licence_count": active_licence_count,
        "stale_licence_count": stale_licence_count,
        "expiring_licence_count": expiring_licence_count,
        "missing_terminal_count": len(missing_terminal_ids),
        "sync_event_count": sync_event_count,
        "sync_event_count_24h": sync_event_count_24h,
        "last_check_in_at": last_check_in,
        "sites": site_summaries,
        "archived_sites": archived_site_summaries,
        "devices": [],
        "licences": prepared_licences,
        "terminals": prepared_terminals,
        "missing_terminal_ids": missing_terminal_ids,
        "recent_events": recent_events,
        "support_timeline": support_timeline,
        "support_notes": support_note_rows,
        "customers": prepared_customers,
        "terminal_sync_rows": terminal_sync_rows,
        "staff_bootstrap_count": len([v for v in bootstrap_map.values() if v]),
        "ready_terminal_count": len([row for row in terminals if bootstrap_map.get(str(row["terminal_device_public_id"] or ""))]),
        "onboarding_steps": {
            "business_created": True,
            "site_added": len(sites) > 0,
            "terminal_provisioned": len(terminals) > 0,
            "paired": any(str(row["status"] or "") == "paired" for row in terminals),
            "first_staff_created": bool(bootstrap_map),
        },
        "backoffice_scope_summary": f"{len(sites)} site{'s' if len(sites) != 1 else ''} / {len(terminals)} terminal{'s' if len(terminals) != 1 else ''} inside this salon business tenant",
    }


def salonmax_customer_ledger_snapshot(business_account_public_id: str, customer_public_id: str):
    business_account_public_id = str(business_account_public_id or "").strip()
    customer_public_id = str(customer_public_id or "").strip()
    business_row = platform_query_one(
        "select * from cloud_business_accounts where business_account_public_id = ?",
        (business_account_public_id,),
    )
    customer_row = platform_query_one(
        """
        select *
        from cloud_customers
        where business_account_public_id = ?
          and customer_public_id = ?
        """,
        (business_account_public_id, customer_public_id),
    )
    if business_row is None or customer_row is None:
        return None
    minute_summary_row = platform_query_one(
        """
        select *
        from cloud_customer_minute_summary
        where business_account_public_id = ?
          and customer_public_id = ?
        """,
        (business_account_public_id, customer_public_id),
    )
    balance_summary_row = platform_query_one(
        """
        select *
        from cloud_customer_balance_summary
        where business_account_public_id = ?
          and customer_public_id = ?
        """,
        (business_account_public_id, customer_public_id),
    )
    minute_ledger_rows = platform_query_all(
        """
        select *
        from cloud_customer_minute_ledger
        where business_account_public_id = ?
          and customer_public_id = ?
        order by created_at desc, id desc
        """,
        (business_account_public_id, customer_public_id),
    )
    balance_ledger_rows = platform_query_all(
        """
        select *
        from cloud_customer_balance_ledger
        where business_account_public_id = ?
          and customer_public_id = ?
        order by created_at desc, id desc
        """,
        (business_account_public_id, customer_public_id),
    )
    adjustment_note_rows = platform_query_all(
        """
        select *
        from cloud_support_notes
        where business_account_public_id = ?
          and customer_public_id = ?
        order by created_at desc, id desc
        limit 50
        """,
        (business_account_public_id, customer_public_id),
    )
    full_name = " ".join(
        part for part in [str(customer_row["first_name"] or "").strip(), str(customer_row["last_name"] or "").strip()] if part
    ).strip()
    return {
        "platform_name": "Salon Max",
        "business_account_public_id": business_account_public_id,
        "business_name": str(business_row["business_name"] or ""),
        "customer_public_id": customer_public_id,
        "customer_number": str(customer_row["customer_number"] or ""),
        "full_name": full_name or customer_public_id,
        "phone": str(customer_row["phone"] or ""),
        "email": str(customer_row["email"] or ""),
        "status": str(customer_row["status"] or "active"),
        "minutes_available": int((minute_summary_row["minutes_available"] if minute_summary_row else 0) or 0),
        "current_balance": str((balance_summary_row["current_balance"] if balance_summary_row else "0.00") or "0.00"),
        "currency_code": str((balance_summary_row["currency_code"] if balance_summary_row else "GBP") or "GBP"),
        "minute_ledger_rows": minute_ledger_rows,
        "balance_ledger_rows": balance_ledger_rows,
        "adjustment_note_rows": adjustment_note_rows,
        "adjustment_type_options": [
            {"value": "manual_credit", "label": "Manual Credit"},
            {"value": "refund_credit", "label": "Refund Credit"},
            {"value": "manual_debit", "label": "Manual Debit"},
            {"value": "reversal_debit", "label": "Reversal Debit"},
        ],
        "reason_category_options": [
            {"value": "refund", "label": "Refund"},
            {"value": "reversal", "label": "Reversal"},
            {"value": "goodwill", "label": "Goodwill"},
            {"value": "correction", "label": "Correction"},
            {"value": "support_resolution", "label": "Support Resolution"},
        ],
    }


def cloud_customer_correction_snapshot(business_account_public_id: str, *, since_minute_ledger_id: int = 0, since_balance_ledger_id: int = 0):
    business_account_public_id = str(business_account_public_id or "").strip()
    minute_rows = platform_query_all(
        """
        select
            ledger.id,
            ledger.customer_public_id,
            ledger.delta_minutes,
            ledger.entry_type,
            ledger.notes,
            ledger.created_at,
            customers.customer_number,
            customers.account_number,
            customers.first_name,
            customers.last_name
        from cloud_customer_minute_ledger ledger
        join cloud_customers customers
          on customers.business_account_public_id = ledger.business_account_public_id
         and customers.customer_public_id = ledger.customer_public_id
        where ledger.business_account_public_id = ?
          and ledger.id > ?
          and ledger.entry_type in ('manual_credit', 'refund_credit', 'manual_debit', 'reversal_debit')
        order by ledger.id
        """,
        (business_account_public_id, int(since_minute_ledger_id)),
    )
    balance_rows = platform_query_all(
        """
        select
            ledger.id,
            ledger.customer_public_id,
            ledger.delta_amount,
            ledger.entry_type,
            ledger.notes,
            ledger.created_at,
            customers.customer_number,
            customers.account_number,
            customers.first_name,
            customers.last_name
        from cloud_customer_balance_ledger ledger
        join cloud_customers customers
          on customers.business_account_public_id = ledger.business_account_public_id
         and customers.customer_public_id = ledger.customer_public_id
        where ledger.business_account_public_id = ?
          and ledger.id > ?
          and ledger.entry_type in ('manual_credit', 'refund_credit', 'manual_debit', 'reversal_debit')
        order by ledger.id
        """,
        (business_account_public_id, int(since_balance_ledger_id)),
    )
    minute_entries = [
        {
            "id": int(row["id"] or 0),
            "customer_public_id": str(row["customer_public_id"] or ""),
            "customer_number": str(row["customer_number"] or ""),
            "account_number": str(row["account_number"] or ""),
            "customer_first_name": str(row["first_name"] or ""),
            "customer_last_name": str(row["last_name"] or ""),
            "delta_minutes": int(row["delta_minutes"] or 0),
            "entry_type": str(row["entry_type"] or ""),
            "notes": str(row["notes"] or ""),
            "created_at": str(row["created_at"] or ""),
        }
        for row in minute_rows
    ]
    balance_entries = [
        {
            "id": int(row["id"] or 0),
            "customer_public_id": str(row["customer_public_id"] or ""),
            "customer_number": str(row["customer_number"] or ""),
            "account_number": str(row["account_number"] or ""),
            "customer_first_name": str(row["first_name"] or ""),
            "customer_last_name": str(row["last_name"] or ""),
            "delta_amount": float(row["delta_amount"] or 0),
            "entry_type": str(row["entry_type"] or ""),
            "notes": str(row["notes"] or ""),
            "created_at": str(row["created_at"] or ""),
        }
        for row in balance_rows
    ]
    last_minute_ledger_id = max([int(since_minute_ledger_id)] + [entry["id"] for entry in minute_entries])
    last_balance_ledger_id = max([int(since_balance_ledger_id)] + [entry["id"] for entry in balance_entries])
    return {
        "minute_entries": minute_entries,
        "balance_entries": balance_entries,
        "last_minute_ledger_id": last_minute_ledger_id,
        "last_balance_ledger_id": last_balance_ledger_id,
    }


def cloud_customer_directory_snapshot(business_account_public_id: str, *, updated_since: str = ""):
    sql = """
        select
            customers.customer_public_id,
            customers.customer_number,
            customers.account_number,
            customers.first_name,
            customers.last_name,
            customers.phone,
            customers.email,
            customers.notes,
            customers.status,
            customers.updated_at,
            coalesce(minute_summary.minutes_available, 0) as minutes_available,
            coalesce(balance_summary.current_balance, '0.00') as current_balance
        from cloud_customers customers
        left join cloud_customer_minute_summary minute_summary
          on minute_summary.customer_public_id = customers.customer_public_id
        left join cloud_customer_balance_summary balance_summary
          on balance_summary.customer_public_id = customers.customer_public_id
        where customers.business_account_public_id = ?
          and customers.status != 'archived'
    """
    params: list[object] = [business_account_public_id]
    updated_since = str(updated_since or "").strip()
    if updated_since:
        sql += " and datetime(coalesce(customers.updated_at, customers.created_at)) > datetime(?)"
        params.append(updated_since)
    sql += " order by datetime(coalesce(customers.updated_at, customers.created_at)) asc, customers.customer_public_id"
    rows = platform_query_all(sql, tuple(params))
    customers = []
    latest_updated_at = updated_since
    for row in rows:
        row_updated_at = str(row["updated_at"] or "").strip()
        if row_updated_at and (not latest_updated_at or row_updated_at > latest_updated_at):
            latest_updated_at = row_updated_at
        customers.append(
            {
                "customer_public_id": str(row["customer_public_id"] or ""),
                "customer_number": str(row["customer_number"] or ""),
                "account_number": str(row["account_number"] or ""),
                "first_name": str(row["first_name"] or ""),
                "last_name": str(row["last_name"] or ""),
                "phone": str(row["phone"] or ""),
                "email": str(row["email"] or ""),
                "notes": str(row["notes"] or ""),
                "status": str(row["status"] or "active"),
                "updated_at": row_updated_at,
                "minutes_available": int(row["minutes_available"] or 0),
                "current_balance": f"{float(row['current_balance'] or 0):.2f}",
            }
        )
    return {
        "business_account_public_id": business_account_public_id,
        "latest_updated_at": latest_updated_at,
        "customers": customers,
    }


def save_cloud_support_note(
    business_account_public_id: str,
    *,
    terminal_device_public_id: str = "",
    customer_public_id: str = "",
    note_type: str = "support_note",
    author_name: str = "",
    note_text: str = "",
):
    if not note_text.strip():
        return
    platform_execute(
        """
        insert into cloud_support_notes (
            business_account_public_id,
            terminal_device_public_id,
            customer_public_id,
            note_type,
            author_name,
            note_text
        ) values (?, ?, ?, ?, ?, ?)
        """,
        (
            business_account_public_id,
            terminal_device_public_id.strip(),
            customer_public_id.strip(),
            note_type.strip() or "support_note",
            author_name.strip(),
            note_text.strip(),
        ),
    )


def normalise_manual_adjustment_entry_type(adjustment_type: str, delta_value: float) -> str:
    adjustment_type = str(adjustment_type or "").strip() or "manual_credit"
    positive = float(delta_value) > 0
    if adjustment_type in {"refund_credit", "reversal_debit"}:
        return "refund_credit" if positive else "reversal_debit"
    return "manual_credit" if positive else "manual_debit"


def salonmax_owner_queries_snapshot(
    *,
    business_account_public_id: str = "",
    terminal_device_public_id: str = "",
    transaction_type: str = "",
    payment_method: str = "",
    transaction_search: str = "",
    customer_search: str = "",
    days: int = 30,
):
    seed_local_business_account_record()
    business_rows = platform_query_all(
        """
        select business_account_public_id, business_name
        from cloud_business_accounts
        order by business_name, business_account_public_id
        """
    )
    businesses = [
        {
            "business_account_public_id": str(row["business_account_public_id"] or ""),
            "business_name": str(row["business_name"] or ""),
        }
        for row in business_rows
    ]
    business_name_map = {
        str(row["business_account_public_id"] or ""): str(row["business_name"] or "")
        for row in business_rows
    }

    selected_business_id = str(business_account_public_id or "").strip()
    selected_terminal_id = str(terminal_device_public_id or "").strip()
    selected_transaction_type = str(transaction_type or "").strip()
    selected_payment_method = str(payment_method or "").strip()
    transaction_search_text = str(transaction_search or "").strip().lower()
    customer_search_text = str(customer_search or "").strip().lower()
    try:
        selected_days = max(1, min(int(days), 365))
    except (TypeError, ValueError):
        selected_days = 30

    terminal_params = []
    terminal_sql = """
        select terminal_device_public_id, terminal_name, business_account_public_id
        from cloud_terminal_registry
        where management_status != 'retired'
    """
    if selected_business_id:
        terminal_sql += " and business_account_public_id = ?"
        terminal_params.append(selected_business_id)
    terminal_sql += " order by terminal_name, terminal_device_public_id"
    terminal_rows = platform_query_all(terminal_sql, tuple(terminal_params))
    terminals = [
        {
            "terminal_device_public_id": str(row["terminal_device_public_id"] or ""),
            "terminal_name": str(row["terminal_name"] or ""),
            "business_account_public_id": str(row["business_account_public_id"] or ""),
        }
        for row in terminal_rows
    ]
    terminal_name_map = {
        str(row["terminal_device_public_id"] or ""): str(row["terminal_name"] or "")
        for row in terminal_rows
    }

    customer_sql = """
        select
            c.*,
            ms.minutes_available,
            bs.current_balance,
            bs.currency_code
        from cloud_customers c
        left join cloud_customer_minute_summary ms
          on ms.business_account_public_id = c.business_account_public_id
         and ms.customer_public_id = c.customer_public_id
        left join cloud_customer_balance_summary bs
          on bs.business_account_public_id = c.business_account_public_id
         and bs.customer_public_id = c.customer_public_id
        where 1 = 1
    """
    customer_params = []
    if selected_business_id:
        customer_sql += " and c.business_account_public_id = ?"
        customer_params.append(selected_business_id)
    customer_sql += " order by c.last_name, c.first_name, c.customer_number limit 250"
    customer_rows = platform_query_all(customer_sql, tuple(customer_params))
    prepared_customers = []
    customer_label_map = {}
    for row in customer_rows:
        full_name = " ".join(
            part for part in [str(row["first_name"] or "").strip(), str(row["last_name"] or "").strip()] if part
        ).strip()
        customer_public_id = str(row["customer_public_id"] or "")
        customer_dict = {
            "business_account_public_id": str(row["business_account_public_id"] or ""),
            "business_name": business_name_map.get(str(row["business_account_public_id"] or ""), ""),
            "customer_public_id": customer_public_id,
            "customer_number": str(row["customer_number"] or ""),
            "full_name": full_name or customer_public_id,
            "phone": str(row["phone"] or ""),
            "email": str(row["email"] or ""),
            "status": str(row["status"] or "active"),
            "minutes_available": int((row["minutes_available"] or 0)),
            "current_balance": str((row["current_balance"] or "0.00")),
            "currency_code": str((row["currency_code"] or "GBP")),
        }
        customer_label_map[customer_public_id] = customer_dict
        if customer_search_text:
            searchable = " ".join(
                [
                    customer_dict["full_name"],
                    customer_dict["customer_number"],
                    customer_dict["phone"],
                    customer_dict["email"],
                    customer_public_id,
                ]
            ).lower()
            if customer_search_text not in searchable:
                continue
        prepared_customers.append(customer_dict)
    prepared_customers = prepared_customers[:80]

    cutoff_text = (datetime.now(ZoneInfo("UTC")) - timedelta(days=selected_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    event_sql = """
        select *
        from cloud_sync_events
        where event_type = 'transaction.completed'
          and occurred_at >= ?
    """
    event_params = [cutoff_text]
    if selected_business_id:
        event_sql += " and business_account_public_id = ?"
        event_params.append(selected_business_id)
    if selected_terminal_id:
        event_sql += " and terminal_device_public_id = ?"
        event_params.append(selected_terminal_id)
    event_sql += " order by occurred_at desc, id desc limit 400"
    event_rows = platform_query_all(event_sql, tuple(event_params))

    transaction_rows = []
    gross_sales_total = 0.0
    package_sale_count = 0
    tanning_sale_count = 0
    card_sale_total = 0.0
    cash_sale_total = 0.0
    for row in event_rows:
        payload = {}
        try:
            payload = json.loads(row["payload_json"] or "{}")
            if not isinstance(payload, dict):
                payload = {}
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        row_transaction_type = str(payload.get("transaction_type") or "").strip()
        row_payment_method = str(payload.get("payment_method") or "").strip()
        if selected_transaction_type and row_transaction_type != selected_transaction_type:
            continue
        if selected_payment_method and row_payment_method != selected_payment_method:
            continue

        customer_id = payload.get("customer_id")
        customer_public_id = ""
        if customer_id not in (None, ""):
            customer_public_id = make_cloud_customer_public_id(str(row["business_account_public_id"] or ""), customer_id)
        customer_info = customer_label_map.get(customer_public_id)
        customer_label = customer_info["full_name"] if customer_info else (customer_public_id or "Walk-in / Unknown")
        if customer_info and customer_info.get("customer_number"):
            customer_label = f"{customer_info['full_name']} ({customer_info['customer_number']})"

        transaction_number = str(payload.get("transaction_number") or "")
        total_amount = float(payload.get("total_amount") or 0.0)
        if transaction_search_text:
            search_blob = " ".join(
                [
                    str(row["business_account_public_id"] or ""),
                    business_name_map.get(str(row["business_account_public_id"] or ""), ""),
                    str(row["terminal_device_public_id"] or ""),
                    terminal_name_map.get(str(row["terminal_device_public_id"] or ""), ""),
                    str(row["local_event_id"] or ""),
                    transaction_number,
                    row_transaction_type,
                    row_payment_method,
                    customer_label,
                    str(payload.get("package_name") or ""),
                    str(payload.get("notes") or ""),
                ]
            ).lower()
            if transaction_search_text not in search_blob:
                continue

        gross_sales_total += total_amount
        if row_transaction_type == "package_sale":
            package_sale_count += 1
        if row_transaction_type == "tanning_sale":
            tanning_sale_count += 1
        if row_payment_method == "card":
            card_sale_total += total_amount
        if row_payment_method == "cash":
            cash_sale_total += total_amount

        transaction_rows.append(
            {
                "business_account_public_id": str(row["business_account_public_id"] or ""),
                "business_name": business_name_map.get(str(row["business_account_public_id"] or ""), ""),
                "terminal_device_public_id": str(row["terminal_device_public_id"] or ""),
                "terminal_name": terminal_name_map.get(str(row["terminal_device_public_id"] or ""), str(row["terminal_device_public_id"] or "")),
                "local_event_id": str(row["local_event_id"] or ""),
                "transaction_number": transaction_number or str(row["local_event_id"] or ""),
                "transaction_type": row_transaction_type or "unknown",
                "payment_method": row_payment_method or "",
                "total_amount": total_amount,
                "occurred_at": str(row["occurred_at"] or ""),
                "received_at": str(row["received_at"] or ""),
                "customer_public_id": customer_public_id,
                "customer_label": customer_label,
                "customer_known": bool(customer_info),
                "minutes": int(payload.get("minutes") or 0),
                "account_minutes_used": int(payload.get("account_minutes_used") or 0),
                "package_name": str(payload.get("package_name") or ""),
                "notes": str(payload.get("notes") or ""),
            }
        )
    transaction_rows = transaction_rows[:120]

    return {
        "platform_name": "Salon Max",
        "filters": {
            "business_account_public_id": selected_business_id,
            "terminal_device_public_id": selected_terminal_id,
            "transaction_type": selected_transaction_type,
            "payment_method": selected_payment_method,
            "transaction_search": transaction_search.strip(),
            "customer_search": customer_search.strip(),
            "days": selected_days,
        },
        "businesses": businesses,
        "terminals": terminals,
        "customers": prepared_customers,
        "transactions": transaction_rows,
        "transaction_count": len(transaction_rows),
        "customer_count": len(prepared_customers),
        "gross_sales_total": gross_sales_total,
        "package_sale_count": package_sale_count,
        "tanning_sale_count": tanning_sale_count,
        "card_sale_total": card_sale_total,
        "cash_sale_total": cash_sale_total,
    }


def csv_download_response(filename: str, headers: list[str], rows: list[list[object]]) -> Response:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    response = Response(buffer.getvalue(), mimetype="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def salonmax_owner_analytics_snapshot(*, business_account_public_id: str = "", days: int = 30):
    seed_local_business_account_record()
    selected_business_id = str(business_account_public_id or "").strip()
    try:
        selected_days = max(1, min(int(days), 365))
    except (TypeError, ValueError):
        selected_days = 30

    business_rows = platform_query_all(
        """
        select business_account_public_id, business_name
        from cloud_business_accounts
        order by business_name, business_account_public_id
        """
    )
    businesses = [
        {
            "business_account_public_id": str(row["business_account_public_id"] or ""),
            "business_name": str(row["business_name"] or ""),
        }
        for row in business_rows
    ]

    cutoff_text = (datetime.now(ZoneInfo("UTC")) - timedelta(days=selected_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    event_sql = """
        select *
        from cloud_sync_events
        where event_type = 'transaction.completed'
          and occurred_at >= ?
    """
    event_params = [cutoff_text]
    if selected_business_id:
        event_sql += " and business_account_public_id = ?"
        event_params.append(selected_business_id)
    event_sql += " order by occurred_at desc, id desc"
    event_rows = platform_query_all(event_sql, tuple(event_params))

    peak_hours = {hour: {"hour": hour, "transaction_count": 0, "sales_total": 0.0} for hour in range(24)}
    weekday_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    weekday_stats = {name: {"name": name, "transaction_count": 0, "sales_total": 0.0} for name in weekday_order}
    bed_stats = {}
    product_stats = {}
    package_stats = {}
    transaction_count = 0
    tanning_session_count = 0
    retail_sale_count = 0
    package_sale_count = 0
    retail_units_total = 0
    average_sale_value = 0.0
    total_sales = 0.0

    for event in event_rows:
        payload = {}
        try:
            payload = json.loads(event["payload_json"] or "{}")
            if not isinstance(payload, dict):
                payload = {}
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}

        occurred_dt = parse_utc_text(event["occurred_at"])
        if occurred_dt is not None:
            local_dt = occurred_dt.astimezone(LOCAL_TIMEZONE)
            peak_hours[local_dt.hour]["transaction_count"] += 1
            peak_hours[local_dt.hour]["sales_total"] += float(payload.get("total_amount") or 0.0)
            weekday_name = local_dt.strftime("%A")
            if weekday_name in weekday_stats:
                weekday_stats[weekday_name]["transaction_count"] += 1
                weekday_stats[weekday_name]["sales_total"] += float(payload.get("total_amount") or 0.0)

        transaction_type = str(payload.get("transaction_type") or "").strip()
        total_amount = float(payload.get("total_amount") or 0.0)
        transaction_count += 1
        total_sales += total_amount

        if transaction_type == "tanning_sale":
            tanning_session_count += 1
            device_name = str(payload.get("device_name") or "").strip()
            if not device_name:
                notes_text = str(payload.get("notes") or "").strip()
                if " - " in notes_text:
                    device_name = notes_text.split(" - ", 1)[1].split(" | ", 1)[0].strip()
            if not device_name:
                device_name = str(payload.get("device_id") or "Unknown Bed").strip() or "Unknown Bed"
            if device_name not in bed_stats:
                bed_stats[device_name] = {
                    "device_name": device_name,
                    "session_count": 0,
                    "minutes_total": 0,
                    "sales_total": 0.0,
                }
            bed_stats[device_name]["session_count"] += 1
            bed_stats[device_name]["minutes_total"] += int(payload.get("minutes") or 0)
            bed_stats[device_name]["sales_total"] += total_amount
        elif transaction_type == "retail_sale":
            retail_sale_count += 1
            product_name = str(payload.get("product_name") or "Unknown Product").strip() or "Unknown Product"
            quantity = int(payload.get("quantity") or 0)
            retail_units_total += quantity
            if product_name not in product_stats:
                product_stats[product_name] = {
                    "product_name": product_name,
                    "sale_count": 0,
                    "quantity_total": 0,
                    "sales_total": 0.0,
                }
            product_stats[product_name]["sale_count"] += 1
            product_stats[product_name]["quantity_total"] += quantity
            product_stats[product_name]["sales_total"] += total_amount
        elif transaction_type == "package_sale":
            package_sale_count += 1
            package_name = str(payload.get("package_name") or "Unknown Package").strip() or "Unknown Package"
            if package_name not in package_stats:
                package_stats[package_name] = {
                    "package_name": package_name,
                    "sale_count": 0,
                    "minutes_total": 0,
                    "sales_total": 0.0,
                }
            package_stats[package_name]["sale_count"] += 1
            package_stats[package_name]["minutes_total"] += int(payload.get("minutes_included") or 0)
            package_stats[package_name]["sales_total"] += total_amount

    if transaction_count > 0:
        average_sale_value = total_sales / transaction_count

    peak_hour_rows = sorted(
        peak_hours.values(),
        key=lambda item: (int(item["transaction_count"]), float(item["sales_total"])),
        reverse=True,
    )
    for row in peak_hour_rows:
        row["hour_label"] = f"{int(row['hour']):02d}:00"
    weekday_rows = [weekday_stats[name] for name in weekday_order]
    top_bed_rows = sorted(
        bed_stats.values(),
        key=lambda item: (int(item["session_count"]), float(item["sales_total"]), int(item["minutes_total"])),
        reverse=True,
    )[:15]
    top_product_rows = sorted(
        product_stats.values(),
        key=lambda item: (int(item["quantity_total"]), float(item["sales_total"]), int(item["sale_count"])),
        reverse=True,
    )[:15]
    top_package_rows = sorted(
        package_stats.values(),
        key=lambda item: (int(item["sale_count"]), float(item["sales_total"]), int(item["minutes_total"])),
        reverse=True,
    )[:15]

    return {
        "platform_name": "Salon Max",
        "filters": {
            "business_account_public_id": selected_business_id,
            "days": selected_days,
        },
        "businesses": businesses,
        "transaction_count": transaction_count,
        "total_sales": total_sales,
        "average_sale_value": average_sale_value,
        "tanning_session_count": tanning_session_count,
        "retail_sale_count": retail_sale_count,
        "package_sale_count": package_sale_count,
        "retail_units_total": retail_units_total,
        "peak_hour_rows": peak_hour_rows[:8],
        "weekday_rows": weekday_rows,
        "top_bed_rows": top_bed_rows,
        "top_product_rows": top_product_rows,
        "top_package_rows": top_package_rows,
    }


def salonmax_owner_customer_insights_snapshot(*, business_account_public_id: str = "", days: int = 90):
    seed_local_business_account_record()
    selected_business_id = str(business_account_public_id or "").strip()
    try:
        selected_days = max(7, min(int(days), 365))
    except (TypeError, ValueError):
        selected_days = 90

    business_rows = platform_query_all(
        """
        select business_account_public_id, business_name
        from cloud_business_accounts
        order by business_name, business_account_public_id
        """
    )
    businesses = [
        {
            "business_account_public_id": str(row["business_account_public_id"] or ""),
            "business_name": str(row["business_name"] or ""),
        }
        for row in business_rows
    ]
    business_name_map = {
        str(row["business_account_public_id"] or ""): str(row["business_name"] or "")
        for row in business_rows
    }

    customer_sql = """
        select
            c.*,
            ms.minutes_available,
            bs.current_balance,
            bs.currency_code
        from cloud_customers c
        left join cloud_customer_minute_summary ms
          on ms.business_account_public_id = c.business_account_public_id
         and ms.customer_public_id = c.customer_public_id
        left join cloud_customer_balance_summary bs
          on bs.business_account_public_id = c.business_account_public_id
         and bs.customer_public_id = c.customer_public_id
        where 1 = 1
    """
    customer_params = []
    if selected_business_id:
        customer_sql += " and c.business_account_public_id = ?"
        customer_params.append(selected_business_id)
    customer_sql += " order by c.last_name, c.first_name, c.customer_number"
    customer_rows = platform_query_all(customer_sql, tuple(customer_params))

    customer_metrics = {}
    for row in customer_rows:
        customer_public_id = str(row["customer_public_id"] or "")
        full_name = " ".join(
            part for part in [str(row["first_name"] or "").strip(), str(row["last_name"] or "").strip()] if part
        ).strip()
        current_balance = float(row["current_balance"] or 0.0)
        minutes_available = int(row["minutes_available"] or 0)
        customer_metrics[customer_public_id] = {
            "business_account_public_id": str(row["business_account_public_id"] or ""),
            "business_name": business_name_map.get(str(row["business_account_public_id"] or ""), ""),
            "customer_public_id": customer_public_id,
            "customer_number": str(row["customer_number"] or ""),
            "full_name": full_name or customer_public_id,
            "phone": str(row["phone"] or ""),
            "email": str(row["email"] or ""),
            "status": str(row["status"] or "active"),
            "minutes_available": minutes_available,
            "current_balance": current_balance,
            "currency_code": str(row["currency_code"] or "GBP"),
            "last_activity_at": "",
            "last_tanning_at": "",
            "lifetime_sales_total": 0.0,
            "window_sales_total": 0.0,
            "window_transaction_count": 0,
            "window_tanning_count": 0,
            "window_package_count": 0,
            "window_retail_count": 0,
            "days_since_last_activity": None,
        }

    event_sql = """
        select *
        from cloud_sync_events
        where event_type = 'transaction.completed'
    """
    event_params = []
    if selected_business_id:
        event_sql += " and business_account_public_id = ?"
        event_params.append(selected_business_id)
    event_sql += " order by occurred_at desc, id desc"
    event_rows = platform_query_all(event_sql, tuple(event_params))

    now_utc = datetime.now(ZoneInfo("UTC"))
    cutoff_dt = now_utc - timedelta(days=selected_days)
    for event in event_rows:
        payload = {}
        try:
            payload = json.loads(event["payload_json"] or "{}")
            if not isinstance(payload, dict):
                payload = {}
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        customer_id = payload.get("customer_id")
        if customer_id in (None, ""):
            continue
        business_id = str(event["business_account_public_id"] or "")
        customer_public_id = make_cloud_customer_public_id(business_id, customer_id)
        if customer_public_id not in customer_metrics:
            continue
        metric = customer_metrics[customer_public_id]
        occurred_at = str(event["occurred_at"] or "")
        occurred_dt = parse_utc_text(occurred_at)
        transaction_type = str(payload.get("transaction_type") or "").strip()
        total_amount = float(payload.get("total_amount") or 0.0)
        metric["lifetime_sales_total"] += total_amount
        if not metric["last_activity_at"]:
            metric["last_activity_at"] = occurred_at
            if occurred_dt is not None:
                metric["days_since_last_activity"] = int((now_utc - occurred_dt).total_seconds() // 86400)
        if transaction_type == "tanning_sale" and not metric["last_tanning_at"]:
            metric["last_tanning_at"] = occurred_at
        if occurred_dt is not None and occurred_dt >= cutoff_dt:
            metric["window_sales_total"] += total_amount
            metric["window_transaction_count"] += 1
            if transaction_type == "tanning_sale":
                metric["window_tanning_count"] += 1
            elif transaction_type == "package_sale":
                metric["window_package_count"] += 1
            elif transaction_type == "retail_sale":
                metric["window_retail_count"] += 1

    for metric in customer_metrics.values():
        metric["has_unused_value"] = metric["minutes_available"] > 0 or metric["current_balance"] > 0.009
        days_since = metric["days_since_last_activity"]
        metric["retention_status"] = "active"
        metric["retention_label"] = "Active"
        if days_since is None:
            metric["retention_status"] = "never_seen"
            metric["retention_label"] = "No Activity Yet"
        elif days_since >= 90:
            metric["retention_status"] = "dormant"
            metric["retention_label"] = "Dormant 90+ Days"
        elif days_since >= 60:
            metric["retention_status"] = "warning"
            metric["retention_label"] = "Inactive 60+ Days"
        elif days_since >= 30:
            metric["retention_status"] = "warning"
            metric["retention_label"] = "Inactive 30+ Days"
        elif metric["window_tanning_count"] >= 2:
            metric["retention_status"] = "good"
            metric["retention_label"] = "Repeat Visitor"

    metric_rows = list(customer_metrics.values())
    active_30_count = sum(
        1 for row in metric_rows if row["days_since_last_activity"] is not None and row["days_since_last_activity"] < 30
    )
    dormant_60_count = sum(
        1 for row in metric_rows if row["days_since_last_activity"] is not None and row["days_since_last_activity"] >= 60
    )
    dormant_with_value_count = sum(
        1
        for row in metric_rows
        if row["has_unused_value"]
        and row["days_since_last_activity"] is not None
        and row["days_since_last_activity"] >= 30
    )
    repeat_visitor_count = sum(1 for row in metric_rows if row["window_tanning_count"] >= 2)

    top_spender_rows = sorted(
        metric_rows,
        key=lambda row: (float(row["window_sales_total"]), int(row["window_transaction_count"])),
        reverse=True,
    )[:20]
    repeat_visitor_rows = sorted(
        metric_rows,
        key=lambda row: (int(row["window_tanning_count"]), float(row["window_sales_total"])),
        reverse=True,
    )[:20]
    dormant_rows = sorted(
        [
            row
            for row in metric_rows
            if row["days_since_last_activity"] is not None and row["days_since_last_activity"] >= 30
        ],
        key=lambda row: (int(row["days_since_last_activity"]), float(row["minutes_available"]), float(row["current_balance"])),
        reverse=True,
    )[:25]
    unused_value_rows = sorted(
        [row for row in metric_rows if row["has_unused_value"]],
        key=lambda row: (int(row["minutes_available"]), float(row["current_balance"]), float(row["window_sales_total"])),
        reverse=True,
    )[:25]

    return {
        "platform_name": "Salon Max",
        "filters": {
            "business_account_public_id": selected_business_id,
            "days": selected_days,
        },
        "businesses": businesses,
        "customer_count": len(metric_rows),
        "active_30_count": active_30_count,
        "dormant_60_count": dormant_60_count,
        "dormant_with_value_count": dormant_with_value_count,
        "repeat_visitor_count": repeat_visitor_count,
        "top_spender_rows": top_spender_rows,
        "repeat_visitor_rows": repeat_visitor_rows,
        "dormant_rows": dormant_rows,
        "unused_value_rows": unused_value_rows,
    }


def salonmax_owner_updates_snapshot(*, business_account_public_id: str = "", version_filter: str = "", sync_filter: str = ""):
    seed_local_business_account_record()
    selected_business_id = str(business_account_public_id or "").strip()
    selected_version_filter = str(version_filter or "").strip().lower()
    selected_sync_filter = str(sync_filter or "").strip().lower()
    business_rows = platform_query_all(
        """
        select business_account_public_id, business_name
        from cloud_business_accounts
        order by business_name, business_account_public_id
        """
    )
    businesses = [
        {
            "business_account_public_id": str(row["business_account_public_id"] or ""),
            "business_name": str(row["business_name"] or ""),
        }
        for row in business_rows
    ]
    if selected_business_id:
        reconcile_runtime_terminals_for_business(selected_business_id)
    else:
        for business in businesses:
            reconcile_runtime_terminals_for_business(business["business_account_public_id"])
    business_name_map = {
        str(row["business_account_public_id"] or ""): str(row["business_name"] or "")
        for row in business_rows
    }
    terminal_sql = """
        select *
        from cloud_terminal_registry
        where management_status != 'retired'
    """
    params = []
    if selected_business_id:
        terminal_sql += " and business_account_public_id = ?"
        params.append(selected_business_id)
    terminal_sql += " order by business_account_public_id, terminal_name, terminal_device_public_id"
    terminal_rows = platform_query_all(terminal_sql, tuple(params))

    versions = set()
    unknown_version_count = 0
    needs_update_count = 0
    current_version_count = 0
    sync_failing_count = 0
    sync_pending_count = 0
    terminals = []
    business_summary_map = {
        business["business_account_public_id"]: {
            "business_account_public_id": business["business_account_public_id"],
            "business_name": business["business_name"],
            "terminal_count": 0,
            "current_version_count": 0,
            "needs_update_count": 0,
            "unknown_version_count": 0,
            "sync_failing_count": 0,
            "sync_pending_count": 0,
            "versions_seen": set(),
            "latest_target_version": "",
            "update_channel": "stable",
        }
        for business in businesses
    }
    for row in terminal_rows:
        business_account_id = str(row["business_account_public_id"] or "")
        reported = str(row["app_version_reported"] or "").strip()
        desired = str(row["desired_app_version"] or "").strip()
        channel = str(row["app_update_channel"] or "stable").strip() or "stable"
        if reported:
            versions.add(reported)
        status = "unknown"
        label = "Version Unknown"
        if desired and reported and desired == reported:
            status = "current"
            label = "Current"
            current_version_count += 1
        elif desired and not reported:
            status = "warning"
            label = "Awaiting Report"
            unknown_version_count += 1
        elif desired and reported and desired != reported:
            status = "warning"
            label = "Needs Update"
            needs_update_count += 1
        elif reported:
            status = "neutral"
            label = "Tracked"
        else:
            unknown_version_count += 1
        sync_status = str(row["sync_status"] or "healthy").strip() or "healthy"
        sync_label = "Healthy"
        if sync_status == "failing":
            sync_label = "Sync Failing"
            sync_failing_count += 1
        elif sync_status == "pending":
            sync_label = "Sync Pending"
            sync_pending_count += 1
        business_summary = business_summary_map.setdefault(
            business_account_id,
            {
                "business_account_public_id": business_account_id,
                "business_name": business_name_map.get(business_account_id, business_account_id),
                "terminal_count": 0,
                "current_version_count": 0,
                "needs_update_count": 0,
                "unknown_version_count": 0,
                "sync_failing_count": 0,
                "sync_pending_count": 0,
                "versions_seen": set(),
                "latest_target_version": "",
                "update_channel": channel,
            },
        )
        business_summary["terminal_count"] += 1
        if reported:
            business_summary["versions_seen"].add(reported)
        if desired and not business_summary["latest_target_version"]:
            business_summary["latest_target_version"] = desired
        if channel:
            business_summary["update_channel"] = channel
        if status == "current":
            business_summary["current_version_count"] += 1
        elif status == "warning" and label == "Needs Update":
            business_summary["needs_update_count"] += 1
        elif status == "warning" and label == "Awaiting Report":
            business_summary["unknown_version_count"] += 1
        elif status == "unknown":
            business_summary["unknown_version_count"] += 1
        if sync_status == "failing":
            business_summary["sync_failing_count"] += 1
        elif sync_status == "pending":
            business_summary["sync_pending_count"] += 1
        if selected_version_filter and status != selected_version_filter:
            continue
        if selected_sync_filter and sync_status != selected_sync_filter:
            continue
        terminals.append(
            {
                "business_account_public_id": business_account_id,
                "business_name": business_name_map.get(business_account_id, ""),
                "terminal_device_public_id": str(row["terminal_device_public_id"] or ""),
                "terminal_name": str(row["terminal_name"] or ""),
                "site_public_id": str(row["site_public_id"] or ""),
                "management_status": str(row["management_status"] or "active"),
                "reported_version": reported,
                "desired_version": desired,
                "update_channel": channel,
                "last_seen_at": str(row["last_seen_at"] or ""),
                "version_status": status,
                "version_label": label,
                "sync_status": sync_status,
                "sync_label": sync_label,
                "sync_pending_count": int(row["sync_pending_count"] or 0),
                "sync_failed_count": int(row["sync_failed_count"] or 0),
                "next_action_label": (
                    "Open diagnostics"
                    if sync_status == "failing"
                    else "Check version target"
                    if status == "warning" and label == "Needs Update"
                    else "Await report"
                    if status == "warning" and label == "Awaiting Report"
                    else "No action"
                ),
            }
        )
    latest_target_version = ""
    if terminals:
        latest_target_version = next((row["desired_version"] for row in terminals if row["desired_version"]), "")
    business_summaries = []
    for business in businesses:
        summary = business_summary_map.get(business["business_account_public_id"], {}).copy()
        if not summary:
            continue
        summary["versions_seen"] = sorted(summary["versions_seen"])
        business_summaries.append(summary)
    return {
        "platform_name": "Salon Max",
        "filters": {
            "business_account_public_id": selected_business_id,
            "version_filter": selected_version_filter,
            "sync_filter": selected_sync_filter,
        },
        "businesses": businesses,
        "business_summaries": business_summaries,
        "terminals": terminals,
        "terminal_count": len(terminals),
        "versions_seen": sorted(versions),
        "unknown_version_count": unknown_version_count,
        "needs_update_count": needs_update_count,
        "current_version_count": current_version_count,
        "sync_failing_count": sync_failing_count,
        "sync_pending_count": sync_pending_count,
        "latest_target_version": latest_target_version,
        "update_channels": [
            {"value": "stable", "label": "Stable"},
            {"value": "pilot", "label": "Pilot"},
            {"value": "beta", "label": "Beta"},
        ],
        "version_filters": [
            {"value": "", "label": "All version states"},
            {"value": "current", "label": "Current"},
            {"value": "warning", "label": "Needs update / awaiting report"},
            {"value": "neutral", "label": "Tracked"},
            {"value": "unknown", "label": "Unknown"},
        ],
        "sync_filters": [
            {"value": "", "label": "All sync states"},
            {"value": "healthy", "label": "Healthy"},
            {"value": "pending", "label": "Pending"},
            {"value": "failing", "label": "Failing"},
        ],
    }


def salonmax_owner_licences_snapshot(*, business_account_public_id: str = "", health_filter: str = ""):
    seed_local_business_account_record()
    selected_business_id = str(business_account_public_id or "").strip()
    selected_health_filter = str(health_filter or "").strip().lower()
    business_rows = platform_query_all(
        """
        select business_account_public_id, business_name
        from cloud_business_accounts
        where status != 'archived'
        order by business_name, business_account_public_id
        """
    )
    businesses = [
        {
            "business_account_public_id": str(row["business_account_public_id"] or ""),
            "business_name": str(row["business_name"] or ""),
        }
        for row in business_rows
    ]
    sql = """
        select
            licences.*,
            businesses.business_name,
            terminals.terminal_name,
            terminals.site_public_id,
            terminals.management_status,
            terminals.status as terminal_status,
            sites.site_name as site_name,
            sites.status as site_status,
            businesses.status as business_status,
            businesses.subscription_status
        from cloud_device_licences licences
        left join cloud_business_accounts businesses
          on businesses.business_account_public_id = licences.business_account_public_id
        left join cloud_terminal_registry terminals
          on terminals.terminal_device_public_id = licences.terminal_device_public_id
        left join cloud_business_sites sites
          on sites.site_public_id = terminals.site_public_id
        where businesses.status != 'archived'
    """
    params = []
    if selected_business_id:
        sql += " and licences.business_account_public_id = ?"
        params.append(selected_business_id)
    sql += " order by businesses.business_name, licences.last_check_in_at desc, licences.terminal_device_public_id"
    licence_rows = platform_query_all(sql, tuple(params))

    unlicensed_sql = """
        select count(*) as value
        from cloud_terminal_registry terminals
        left join cloud_device_licences licences
          on licences.terminal_device_public_id = terminals.terminal_device_public_id
        left join cloud_business_accounts businesses
          on businesses.business_account_public_id = terminals.business_account_public_id
        where terminals.management_status != 'retired'
          and businesses.status != 'archived'
          and licences.id is null
    """
    unlicensed_params = []
    if selected_business_id:
        unlicensed_sql += " and terminals.business_account_public_id = ?"
        unlicensed_params.append(selected_business_id)
    unlicensed_count = int((platform_query_one(unlicensed_sql, tuple(unlicensed_params))["value"]) or 0)

    now_utc = datetime.now(ZoneInfo("UTC"))
    active_count = 0
    stale_count = 0
    expiring_count = 0
    blocked_count = 0
    prepared_rows = []
    for row in licence_rows:
        business_status = str(row["business_status"] or "active").strip().lower()
        site_status = str(row["site_status"] or "active").strip().lower()
        management_status = str(row["management_status"] or "active").strip().lower()
        licence_status = str(row["licence_status"] or "active").strip().lower()
        check_in_dt = parse_utc_text(row["last_check_in_at"])
        expires_dt = parse_utc_text(row["expires_at"])
        minutes_since_check_in = None
        if check_in_dt is not None:
            minutes_since_check_in = int((now_utc - check_in_dt).total_seconds() // 60)
        hours_to_expiry = None
        if expires_dt is not None:
            hours_to_expiry = int((expires_dt - now_utc).total_seconds() // 3600)
        is_stale = minutes_since_check_in is None or minutes_since_check_in > 30
        is_expired = hours_to_expiry is not None and hours_to_expiry < 0
        is_expiring = hours_to_expiry is not None and 0 <= hours_to_expiry <= 72
        blocked_reason = ""
        if business_status in {"suspended", "paused", "deleted"}:
            blocked_reason = f"Business {business_status}"
        elif site_status in {"suspended", "deleted"}:
            blocked_reason = f"Site {site_status}"
        elif management_status in {"suspended", "retired"}:
            blocked_reason = f"Terminal {management_status}"

        if blocked_reason:
            health = "blocked"
            health_label = blocked_reason
            blocked_count += 1
        elif is_expired:
            health = "expired"
            health_label = "Expired"
            blocked_count += 1
        elif is_expiring:
            health = "expiring"
            health_label = "Expiring Soon"
            expiring_count += 1
        elif is_stale:
            health = "stale"
            health_label = "Check-In Stale"
            stale_count += 1
        else:
            health = "healthy"
            health_label = "Healthy"
            active_count += 1

        row_dict = {
            "business_account_public_id": str(row["business_account_public_id"] or ""),
            "business_name": str(row["business_name"] or ""),
            "terminal_device_public_id": str(row["terminal_device_public_id"] or ""),
            "terminal_name": str(row["terminal_name"] or row["terminal_device_public_id"] or ""),
            "site_name": str(row["site_name"] or ""),
            "site_public_id": str(row["site_public_id"] or ""),
            "management_status": management_status,
            "terminal_status": str(row["terminal_status"] or ""),
            "licence_status": licence_status,
            "business_status": business_status,
            "site_status": site_status,
            "expires_at": str(row["expires_at"] or ""),
            "issued_at": str(row["issued_at"] or ""),
            "last_check_in_at": str(row["last_check_in_at"] or ""),
            "health": health,
            "health_label": health_label,
            "minutes_since_check_in": minutes_since_check_in,
            "hours_to_expiry": hours_to_expiry,
        }
        if selected_health_filter and health != selected_health_filter:
            continue
        prepared_rows.append(row_dict)

    return {
        "platform_name": "Salon Max",
        "businesses": businesses,
        "filters": {
            "business_account_public_id": selected_business_id,
            "health_filter": selected_health_filter,
        },
        "rows": prepared_rows,
        "row_count": len(prepared_rows),
        "healthy_count": active_count,
        "stale_count": stale_count,
        "expiring_count": expiring_count,
        "blocked_count": blocked_count,
        "unlicensed_count": unlicensed_count,
        "health_filter_options": [
            {"value": "", "label": "All health states"},
            {"value": "healthy", "label": "Healthy"},
            {"value": "stale", "label": "Check-In Stale"},
            {"value": "expiring", "label": "Expiring Soon"},
            {"value": "blocked", "label": "Blocked / Expired"},
        ],
    }


def salonmax_terminal_diagnostics_snapshot(terminal_device_public_id: str):
    ensure_platform_sync_tables()
    terminal_id = str(terminal_device_public_id or "").strip()
    if not terminal_id:
        return None
    terminal_row = platform_query_one(
        "select * from cloud_terminal_registry where terminal_device_public_id = ?",
        (terminal_id,),
    )
    if terminal_row is None:
        return None
    business_account_public_id = str(terminal_row["business_account_public_id"] or "")
    business_row = platform_query_one(
        "select * from cloud_business_accounts where business_account_public_id = ?",
        (business_account_public_id,),
    )
    site_row = platform_query_one(
        "select * from cloud_business_sites where site_public_id = ?",
        (str(terminal_row["site_public_id"] or ""),),
    )
    licence_row = platform_query_one(
        "select * from cloud_device_licences where terminal_device_public_id = ?",
        (terminal_id,),
    )
    event_rows = platform_query_all(
        """
        select *
        from cloud_sync_events
        where terminal_device_public_id = ?
        order by occurred_at desc, id desc
        limit 25
        """,
        (terminal_id,),
    )
    support_note_rows = platform_query_all(
        """
        select *
        from cloud_support_notes
        where terminal_device_public_id = ?
        order by created_at desc, id desc
        limit 25
        """,
        (terminal_id,),
    )
    recent_transactions = []
    recent_event_types = {}
    for event in event_rows:
        event_type = str(event["event_type"] or "").strip()
        recent_event_types[event_type] = recent_event_types.get(event_type, 0) + 1
        payload = {}
        try:
            payload = json.loads(event["payload_json"] or "{}")
            if not isinstance(payload, dict):
                payload = {}
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        if event_type == "transaction.completed":
            recent_transactions.append(
                {
                    "local_event_id": str(event["local_event_id"] or ""),
                    "occurred_at": str(event["occurred_at"] or ""),
                    "transaction_number": str(payload.get("transaction_number") or ""),
                    "transaction_type": str(payload.get("transaction_type") or "").replace("_", " ").title(),
                    "payment_method": str(payload.get("payment_method") or "").replace("_", " ").title(),
                    "customer_name": " ".join(
                        part
                        for part in [
                            str(payload.get("customer_first_name") or "").strip(),
                            str(payload.get("customer_last_name") or "").strip(),
                        ]
                        if part
                    ).strip(),
                    "total_amount": float(payload.get("total_amount") or 0.0),
                    "notes": str(payload.get("notes") or ""),
                }
            )
    recent_transactions = recent_transactions[:12]
    version_status = "unknown"
    version_label = "Version Unknown"
    reported_version = str(terminal_row["app_version_reported"] or "").strip()
    desired_version = str(terminal_row["desired_app_version"] or "").strip()
    if desired_version and reported_version and desired_version == reported_version:
        version_status = "current"
        version_label = "Current"
    elif desired_version and reported_version and desired_version != reported_version:
        version_status = "warning"
        version_label = "Needs Update"
    elif desired_version and not reported_version:
        version_status = "warning"
        version_label = "Awaiting Report"
    elif reported_version:
        version_status = "neutral"
        version_label = "Tracked"

    check_in_status = "unknown"
    check_in_label = "No Check-In"
    if licence_row is not None:
        now_utc = datetime.now(ZoneInfo("UTC"))
        check_in_dt = parse_utc_text(licence_row["last_check_in_at"])
        expires_dt = parse_utc_text(licence_row["expires_at"])
        minutes_since_check_in = None
        hours_to_expiry = None
        if check_in_dt is not None:
            minutes_since_check_in = int((now_utc - check_in_dt).total_seconds() // 60)
        if expires_dt is not None:
            hours_to_expiry = int((expires_dt - now_utc).total_seconds() // 3600)
        if hours_to_expiry is not None and hours_to_expiry < 0:
            check_in_status = "danger"
            check_in_label = "Licence Expired"
        elif hours_to_expiry is not None and hours_to_expiry <= 72:
            check_in_status = "warning"
            check_in_label = "Licence Expiring"
        elif minutes_since_check_in is None or minutes_since_check_in > 30:
            check_in_status = "warning"
            check_in_label = "Check-In Stale"
        else:
            check_in_status = "good"
            check_in_label = "Healthy"
    sync_status = str(terminal_row["sync_status"] or "healthy").strip() or "healthy"
    sync_label = "Healthy"
    if sync_status == "failing":
        sync_label = "Sync Failing"
    elif sync_status == "pending":
        sync_label = "Sync Pending"
    try:
        recent_failed_items = json.loads(terminal_row["sync_recent_failures_json"] or "[]")
        if not isinstance(recent_failed_items, list):
            recent_failed_items = []
    except (TypeError, ValueError, json.JSONDecodeError):
        recent_failed_items = []
    try:
        recent_pending_items = json.loads(terminal_row["sync_recent_pending_json"] or "[]")
        if not isinstance(recent_pending_items, list):
            recent_pending_items = []
    except (TypeError, ValueError, json.JSONDecodeError):
        recent_pending_items = []

    return {
        "platform_name": "Salon Max",
        "business_account_public_id": business_account_public_id,
        "business_name": str((business_row["business_name"] if business_row else "") or ""),
        "terminal_device_public_id": terminal_id,
        "terminal_name": str(terminal_row["terminal_name"] or terminal_id),
        "site_name": str((site_row["site_name"] if site_row else terminal_row["site_public_id"]) or ""),
        "site_public_id": str(terminal_row["site_public_id"] or ""),
        "install_mode": str(terminal_row["install_mode"] or "fresh_install"),
        "install_mode_label": "Replacement Pi" if str(terminal_row["install_mode"] or "") == "replacement_pi" else "Fresh Install",
        "status": str(terminal_row["status"] or ""),
        "management_status": str(terminal_row["management_status"] or "active"),
        "pairing_code": str(terminal_row["pairing_code"] or ""),
        "last_seen_at": str(terminal_row["last_seen_at"] or ""),
        "reported_version": reported_version,
        "desired_version": desired_version,
        "update_channel": str(terminal_row["app_update_channel"] or "stable"),
        "version_status": version_status,
        "version_label": version_label,
        "sync_status": sync_status,
        "sync_label": sync_label,
        "sync_pending_count": int(terminal_row["sync_pending_count"] or 0),
        "sync_failed_count": int(terminal_row["sync_failed_count"] or 0),
        "sync_oldest_outstanding_at": str(terminal_row["sync_oldest_outstanding_at"] or ""),
        "sync_last_attempt_at": str(terminal_row["sync_last_attempt_at"] or ""),
        "sync_last_acknowledged_at": str(terminal_row["sync_last_acknowledged_at"] or ""),
        "sync_last_checkpoint_at": str(terminal_row["sync_last_checkpoint_at"] or ""),
        "recent_failed_items": recent_failed_items,
        "recent_pending_items": recent_pending_items,
        "licence_row": licence_row,
        "check_in_status": check_in_status,
        "check_in_label": check_in_label,
        "event_count": len(event_rows),
        "event_type_breakdown": [
            {"event_type": event_type, "count": count}
            for event_type, count in sorted(recent_event_types.items(), key=lambda item: (-item[1], item[0]))
        ],
        "recent_transactions": recent_transactions,
        "support_notes": support_note_rows,
        "recent_events": event_rows,
    }


def parse_checkbox(name: str) -> int:
    return 1 if request.form.get(name, "").strip() in {"1", "true", "on", "yes"} else 0


def provider_notice_redirect(endpoint: str, **values):
    notice = request.args.get("notice", "").strip()
    if notice:
        values["notice"] = notice
    return redirect(url_for(endpoint, **values))


def recipient_list(value: str):
    cleaned = (value or "").replace(";", ",").replace("\n", ",")
    return [part.strip() for part in cleaned.split(",") if part.strip()]


def totals_between(start_text, end_text):
    return query_one(
        """
        select
            count(*) as transaction_count,
            coalesce(sum(total_amount), 0) as sales_total,
            coalesce(sum(case when payment_method = 'cash' then total_amount else 0 end), 0) as cash_total,
            coalesce(sum(case when payment_method = 'card' then total_amount else 0 end), 0) as card_total,
            coalesce(sum(case when transaction_type = 'retail_sale' then total_amount else 0 end), 0) as retail_total,
            coalesce(sum(case when transaction_type = 'package_sale' then total_amount else 0 end), 0) as package_total,
            coalesce(sum(case when transaction_type = 'tanning_sale' then total_amount else 0 end), 0) as tanning_total
        from transactions
        where status = 'completed'
          and datetime(created_at) >= datetime(?)
          and datetime(created_at) <= datetime(?)
        """,
        (start_text, end_text),
    )


def detailed_totals_between(start_text, end_text, site_id=None, terminal_id=None):
    clauses = [
        "status = 'completed'",
        "datetime(created_at) >= datetime(?)",
        "datetime(created_at) <= datetime(?)",
    ]
    params = [start_text, end_text]

    if site_id is not None:
        clauses.append("site_id = ?")
        params.append(site_id)
    if terminal_id is not None:
        clauses.append("terminal_id = ?")
        params.append(terminal_id)

    where_sql = " and ".join(clauses)
    return query_one(
        f"""
        select
            count(*) as transaction_count,
            count(distinct case when customer_id is not null then customer_id end) as customer_count,
            coalesce(sum(total_amount), 0) as sales_total,
            coalesce(sum(case when payment_method = 'cash' then total_amount else 0 end), 0) as cash_total,
            coalesce(sum(case when payment_method = 'card' then total_amount else 0 end), 0) as card_total,
            count(case when payment_method = 'cash' then 1 end) as cash_count,
            count(case when payment_method = 'card' then 1 end) as card_count,
            count(case when payment_method = 'package_minutes' then 1 end) as package_minutes_count,
            count(case when transaction_type = 'tanning_sale' and notes like '%Used % account mins%' then 1 end) as account_minutes_used_count,
            coalesce(sum(case when transaction_type = 'retail_sale' then total_amount else 0 end), 0) as retail_total,
            coalesce(sum(case when transaction_type = 'package_sale' then total_amount else 0 end), 0) as package_total,
            coalesce(sum(case when transaction_type = 'tanning_sale' then total_amount else 0 end), 0) as tanning_total,
            coalesce(sum(case when transaction_type = 'account_topup' then total_amount else 0 end), 0) as topup_total
        from transactions
        where {where_sql}
        """,
        tuple(params),
    )


def best_seller_rows(start_text, end_text):
    return query_all(
        """
        select
            description,
            count(*) as sale_count,
            coalesce(sum(quantity), 0) as units_sold,
            coalesce(sum(line_total), 0) as sales_total
        from transaction_lines
        left join transactions on transactions.id = transaction_lines.transaction_id
        where transactions.status = 'completed'
          and datetime(transactions.created_at) >= datetime(?)
          and datetime(transactions.created_at) <= datetime(?)
        group by description
        order by sales_total desc, units_sold desc, sale_count desc
        limit 10
        """,
        (start_text, end_text),
    )


def parse_date_or_default(value: str, fallback_date):
    text = (value or "").strip()
    if not text:
        return fallback_date
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return fallback_date


def transactions_for_day(day_value):
    start_text = f"{day_value.isoformat()} 00:00:00"
    end_text = f"{day_value.isoformat()} 23:59:59"
    return query_all(
        """
        select
            transactions.*,
            customers.first_name,
            customers.last_name,
            sites.name as site_name,
            staff_users.name as staff_name
        from transactions
        left join customers on customers.id = transactions.customer_id
        left join sites on sites.id = transactions.site_id
        left join staff_users on staff_users.id = transactions.staff_user_id
        where datetime(transactions.created_at) >= datetime(?)
          and datetime(transactions.created_at) <= datetime(?)
        order by transactions.id desc
        """,
        (start_text, end_text),
    )


def email_report_settings():
    return business_settings_row()


def build_shift_summary_text(session_row, totals):
    return "\n".join(
        [
            f"Shift summary for {session_row['site_name']} / {session_row['terminal_name']}",
            "",
            f"Opened: {session_row['opened_at']}",
            f"Closed: {session_row['closed_at'] or '-'}",
            f"Opened by: {session_row['opened_by_name'] or '-'}",
            f"Closed by: {session_row['closed_by_name'] or '-'}",
            "",
            f"Opening float: GBP {float(session_row['opening_float'] or 0):.2f}",
            f"Expected cash: GBP {float(session_row['expected_cash'] or 0):.2f}",
            f"Counted cash: GBP {float(session_row['counted_cash'] or 0):.2f}",
            f"Variance: GBP {float(session_row['variance'] or 0):.2f}",
            "",
            f"Total sales: GBP {float(totals['sales_total'] or 0):.2f}",
            f"Cash sales: GBP {float(totals['cash_total'] or 0):.2f}",
            f"Card sales: GBP {float(totals['card_total'] or 0):.2f}",
            f"Customers served: {int(totals['customer_count'] or 0)}",
            f"Transactions: {int(totals['transaction_count'] or 0)}",
            f"Cash payments: {int(totals['cash_count'] or 0)}",
            f"Card payments: {int(totals['card_count'] or 0)}",
            f"Used account mins: {int(totals['account_minutes_used_count'] or 0)}",
            "",
            f"Tanning total: GBP {float(totals['tanning_total'] or 0):.2f}",
            f"Course total: GBP {float(totals['package_total'] or 0):.2f}",
            f"Retail total: GBP {float(totals['retail_total'] or 0):.2f}",
            f"Top-up total: GBP {float(totals['topup_total'] or 0):.2f}",
            "",
            f"Closing notes: {session_row['closing_notes'] or '-'}",
        ]
    )


def build_daily_summary_text(day_value, totals):
    day_label = day_value.isoformat() if hasattr(day_value, "isoformat") else str(day_value)
    return "\n".join(
        [
            f"Daily management summary for {day_label}",
            "",
            f"Total sales: GBP {float(totals['sales_total'] or 0):.2f}",
            f"Cash sales: GBP {float(totals['cash_total'] or 0):.2f}",
            f"Card sales: GBP {float(totals['card_total'] or 0):.2f}",
            f"Customers served: {int(totals['customer_count'] or 0)}",
            f"Transactions: {int(totals['transaction_count'] or 0)}",
            f"Cash payments: {int(totals['cash_count'] or 0)}",
            f"Card payments: {int(totals['card_count'] or 0)}",
            f"Used account mins: {int(totals['account_minutes_used_count'] or 0)}",
            "",
            f"Tanning total: GBP {float(totals['tanning_total'] or 0):.2f}",
            f"Course total: GBP {float(totals['package_total'] or 0):.2f}",
            f"Retail total: GBP {float(totals['retail_total'] or 0):.2f}",
            f"Top-up total: GBP {float(totals['topup_total'] or 0):.2f}",
        ]
    )


def json_error(code: str, message: str, status: int = 400):
    response = jsonify(
        {
            "ok": False,
            "error": {
                "code": code,
                "message": message,
            },
        }
    )
    response.status_code = status
    return response


def bearer_token_value() -> str:
    header = request.headers.get("Authorization", "").strip()
    if not header.lower().startswith("bearer "):
        return ""
    return header[7:].strip()


def token_is_supported(token: str) -> bool:
    token = str(token or "").strip()
    if not token:
        return True
    return token.startswith("local-dev:") or token.startswith("sm-lease:")


def business_access_state(account_row) -> tuple[bool, str, str]:
    if account_row is None:
        return False, "inactive", "Business account not found."
    account_status = str(account_row["status"] or "active").strip().lower()
    subscription_status = str(account_row["subscription_status"] or "active").strip().lower()
    if account_status == "deleted":
        return False, "deleted", "This business has been archived."
    if account_status == "suspended":
        return False, "suspended", "This business is suspended and trading is locked."
    if account_status == "paused":
        return False, "paused", "This business is paused and trading is locked."
    if subscription_status == "paused":
        return False, "paused", "This subscription is paused and trading is locked."
    return True, "active", ""


def terminal_access_state(account_row, site_row, terminal_row) -> tuple[bool, str, str]:
    access_allowed, licence_status, access_message = business_access_state(account_row)
    if not access_allowed:
        return access_allowed, licence_status, access_message
    if site_row is None:
        return False, "site_missing", "This site record is missing, so trading is locked."
    site_status = str(site_row["status"] or "active").strip().lower()
    if site_status == "deleted":
        return False, "site_deleted", "This site has been archived and trading is locked."
    if site_status == "suspended":
        return False, "site_suspended", "This site is suspended and trading is locked."
    if terminal_row is None:
        return False, "terminal_missing", "This terminal is not provisioned correctly, so trading is locked."
    management_status = str(terminal_row["management_status"] or "active").strip().lower()
    if management_status == "retired":
        return False, "terminal_retired", "This terminal has been retired and trading is locked."
    if management_status == "suspended":
        return False, "terminal_suspended", "This terminal is suspended and trading is locked."
    return True, "active", ""


def parse_local_dev_terminal_ids(terminal_device_id: str):
    match = re.fullmatch(r"term_(\d+)_(\d+)", str(terminal_device_id or "").strip())
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def gpio_mapping_for_bed_number(bed_number: int):
    relay_map = {
        1: 17,
        2: 22,
        3: 24,
        4: 5,
        5: 12,
        6: 16,
        7: 20,
        8: 26,
    }
    feedback_map = {
        1: 18,
        2: 23,
        3: 25,
        4: 6,
        5: 13,
        6: 19,
        7: 21,
        8: 27,
    }
    trigger_override_map = {
        1: 7,
    }
    feedback_override_map = {
        1: 4,
    }
    return {
        "relay_output_pin": relay_map.get(bed_number),
        "trigger_output_pin": trigger_override_map.get(bed_number),
        "feedback_input_pin": feedback_override_map.get(bed_number, feedback_map.get(bed_number)),
    }


def build_device_config_payload(terminal_device_id: str):
    settings = business_settings_row()
    platform_terminal = platform_query_one(
        "select * from cloud_terminal_registry where terminal_device_public_id = ?",
        (terminal_device_id,),
    )

    local_site_row = None
    local_terminal_row = None
    site_name = ""
    terminal_name = ""
    business_name = str(settings.get("business_name") or "Your Salon")
    local_site_id = None
    business_status = "active"
    subscription_status = "active"

    if platform_terminal is not None:
        business_account_id = str(platform_terminal["business_account_public_id"] or "").strip()
        site_public_id = str(platform_terminal["site_public_id"] or "").strip()
        platform_business = platform_query_one(
            "select * from cloud_business_accounts where business_account_public_id = ?",
            (business_account_id,),
        )
        platform_site = platform_query_one(
            "select * from cloud_business_sites where site_public_id = ?",
            (site_public_id,),
        )
        if platform_business is not None:
            business_name = str(platform_business["business_name"] or business_name)
            business_status = str(platform_business["status"] or "active").strip().lower() or "active"
            subscription_status = str(platform_business["subscription_status"] or "active").strip().lower() or "active"
        if platform_site is not None:
            site_name = str(platform_site["site_name"] or "").strip()
        terminal_name = str(platform_terminal["terminal_name"] or "").strip()
        local_terminal_row = query_one("select * from terminals order by id limit 1")
        if local_terminal_row is not None:
            local_site_id = int(local_terminal_row["site_id"])
            local_site_row = query_one("select * from sites where id = ?", (local_site_id,))
    else:
        site_id, terminal_id = parse_local_dev_terminal_ids(terminal_device_id)
        if site_id is None or terminal_id is None:
            return None
        local_site_id = site_id
        local_site_row = query_one("select * from sites where id = ?", (site_id,))
        local_terminal_row = query_one("select * from terminals where id = ?", (terminal_id,))
        if local_site_row is None or local_terminal_row is None:
            return None
        site_name = str(local_site_row["name"] or "").strip()
        terminal_name = str(local_terminal_row["name"] or "").strip()

    if not site_name and local_site_row is not None:
        site_name = str(local_site_row["name"] or "").strip()
    if not terminal_name and local_terminal_row is not None:
        terminal_name = str(local_terminal_row["name"] or "").strip()

    device_rows = []
    if local_site_id is not None:
        device_rows = query_all(
            "select * from devices where is_active = 1 and site_id = ? order by device_number, device_name",
            (int(local_site_id),),
        )
    beds = []
    pricing = []
    for row in device_rows:
        bed_number = int(row["device_number"] or 0)
        gpio_map = gpio_mapping_for_bed_number(bed_number)
        pricing_row = query_one(
            """
                select price_per_minute
                from pricing_rules
                where is_active = 1
                  and site_id = ?
                  and device_id = ?
                limit 1
                """,
            (int(local_site_id), int(row["id"])),
        )
        if pricing_row is None:
            pricing_row = query_one(
                """
                select price_per_minute
                from pricing_rules
                where is_active = 1
                  and site_id = ?
                  and device_id is null
                limit 1
                """,
                (int(local_site_id),),
            )
        price_per_minute = float(pricing_row["price_per_minute"]) if pricing_row is not None else 0.55
        beds.append(
            {
                "device_id": f"bed_{bed_number}",
                "device_name": row["device_name"],
                "prep_minutes": int(settings.get("default_prep_minutes") or 3),
                "cooldown_minutes": int(settings.get("default_cooldown_minutes") or 3),
                "auto_start_after_prep": True,
                "relay_output_pin": gpio_map["relay_output_pin"],
                "trigger_output_pin": gpio_map["trigger_output_pin"],
                "feedback_input_pin": gpio_map["feedback_input_pin"],
            }
        )
        pricing.append(
            {
                "device_id": f"bed_{bed_number}",
                "price_per_minute": price_per_minute,
            }
        )

    return {
        "platform_name": "Salon Max",
        "business": {
            "business_name": business_name,
            "business_status": business_status,
            "subscription_status": subscription_status,
            "timezone": "Europe/London",
            "currency_code": settings.get("currency_symbol") or "GBP",
            "receipt_footer_text": "",
        },
        "site": {
            "site_name": site_name or "Main Site",
        },
        "terminal": {
            "terminal_name": terminal_name or "Front Desk Till",
        },
        "beds": beds,
        "pricing": pricing,
    }


@app.get("/v1/devices/<terminal_device_id>/config")
def device_config(terminal_device_id: str):
    device_header = str(request.headers.get("X-SalonMax-Device-Id") or "").strip()
    token = bearer_token_value()
    if device_header and device_header != terminal_device_id:
        return json_error("DEVICE_MISMATCH", "Device header does not match path.", status=403)
    if not token_is_supported(token):
        return json_error("AUTH_INVALID", "Unsupported token format.", status=403)

    payload = build_device_config_payload(terminal_device_id)
    if payload is None:
        return json_error("DEVICE_NOT_FOUND", "No config found for this device.", status=404)
    platform_terminal = platform_query_one(
        "select * from cloud_terminal_registry where terminal_device_public_id = ?",
        (terminal_device_id,),
    )
    platform_business = None
    platform_site = None
    if platform_terminal is not None:
        platform_business = platform_query_one(
            "select * from cloud_business_accounts where business_account_public_id = ?",
            (str(platform_terminal["business_account_public_id"] or "").strip(),),
        )
        platform_site = platform_query_one(
            "select * from cloud_business_sites where site_public_id = ?",
            (str(platform_terminal["site_public_id"] or "").strip(),),
        )
    access_allowed, _, access_message = terminal_access_state(platform_business, platform_site, platform_terminal)
    business_info = payload.get("business") if isinstance(payload.get("business"), dict) else {}
    business_status = str(business_info.get("business_status") or "active").strip().lower()
    subscription_status = str(business_info.get("subscription_status") or "active").strip().lower()
    if (business_status in {"suspended", "paused", "deleted"} or subscription_status == "paused" or not access_allowed):
        return json_error("BUSINESS_SUSPENDED", access_message or "This business is suspended and device config is locked.", status=403)

    return jsonify(
        {
            "ok": True,
            "data": payload,
        }
    )


@app.get("/v1/customers/corrections")
def customer_corrections():
    ensure_platform_sync_tables()
    business_account_public_id = str(request.headers.get("X-SalonMax-Business-Id") or "").strip()
    terminal_device_public_id = str(request.headers.get("X-SalonMax-Device-Id") or "").strip()
    token = bearer_token_value()
    if not business_account_public_id or not terminal_device_public_id:
        return json_error("IDENTITY_REQUIRED", "Business and terminal identity are required.", status=400)
    if not token_is_supported(token):
        return json_error("AUTH_INVALID", "Unsupported token format.", status=403)
    terminal_row = platform_query_one(
        """
        select *
        from cloud_terminal_registry
        where business_account_public_id = ?
          and terminal_device_public_id = ?
        """,
        (business_account_public_id, terminal_device_public_id),
    )
    licence_row = platform_query_one(
        """
        select *
        from cloud_device_licences
        where business_account_public_id = ?
          and terminal_device_public_id = ?
          and signed_token = ?
        order by id desc
        limit 1
        """,
        (business_account_public_id, terminal_device_public_id, token),
    )
    if terminal_row is None or licence_row is None:
        return json_error("DEVICE_NOT_AUTHORISED", "This terminal is not authorised for cloud corrections.", status=403)
    try:
        since_minute_ledger_id = max(0, int(str(request.args.get("since_minute_ledger_id") or "0").strip() or "0"))
    except ValueError:
        since_minute_ledger_id = 0
    try:
        since_balance_ledger_id = max(0, int(str(request.args.get("since_balance_ledger_id") or "0").strip() or "0"))
    except ValueError:
        since_balance_ledger_id = 0
    return jsonify(
        {
            "ok": True,
            "data": cloud_customer_correction_snapshot(
                business_account_public_id,
                since_minute_ledger_id=since_minute_ledger_id,
                since_balance_ledger_id=since_balance_ledger_id,
            ),
        }
    )


@app.get("/v1/customers/directory")
def customer_directory_feed():
    ensure_platform_sync_tables()
    business_account_public_id = str(request.headers.get("X-SalonMax-Business-Id") or "").strip()
    terminal_device_public_id = str(request.headers.get("X-SalonMax-Device-Id") or "").strip()
    token = bearer_token_value()
    if not business_account_public_id or not terminal_device_public_id:
        return json_error("IDENTITY_REQUIRED", "Business and terminal identity are required.", status=400)
    if not token_is_supported(token):
        return json_error("AUTH_INVALID", "Unsupported token format.", status=403)
    terminal_row = platform_query_one(
        """
        select *
        from cloud_terminal_registry
        where business_account_public_id = ?
          and terminal_device_public_id = ?
        """,
        (business_account_public_id, terminal_device_public_id),
    )
    licence_row = platform_query_one(
        """
        select *
        from cloud_device_licences
        where business_account_public_id = ?
          and terminal_device_public_id = ?
          and signed_token = ?
        order by id desc
        limit 1
        """,
        (business_account_public_id, terminal_device_public_id, token),
    )
    if terminal_row is None or licence_row is None:
        return json_error("DEVICE_NOT_AUTHORISED", "This terminal is not authorised for customer import sync.", status=403)
    platform_business = platform_query_one(
        "select * from cloud_business_accounts where business_account_public_id = ?",
        (business_account_public_id,),
    )
    platform_site = platform_query_one(
        "select * from cloud_business_sites where site_public_id = ?",
        (str(terminal_row["site_public_id"] or "").strip(),),
    )
    access_allowed, _, access_message = terminal_access_state(platform_business, platform_site, terminal_row)
    if not access_allowed:
        return json_error("BUSINESS_SUSPENDED", access_message or "This business is suspended and customer sync is locked.", status=403)
    updated_since = str(request.args.get("updated_since") or "").strip()
    return jsonify(
        {
            "ok": True,
            "data": cloud_customer_directory_snapshot(
                business_account_public_id,
                updated_since=updated_since,
            ),
        }
    )


@app.post("/v1/devices/pair")
def device_pair():
    ensure_platform_sync_tables()
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return json_error("BAD_REQUEST", "Request body must be valid JSON.")

    pairing_code = str(payload.get("pairing_code") or "").strip().upper()
    device_serial = str(payload.get("device_serial") or "").strip()
    if not pairing_code:
        return json_error("BAD_REQUEST", "pairing_code is required.")

    terminal_row = platform_query_one(
        "select * from cloud_terminal_registry where pairing_code = ?",
        (pairing_code,),
    )
    if terminal_row is None:
        return json_error("PAIRING_NOT_FOUND", "No terminal was found for that pairing code.", status=404)
    management_status = str(terminal_row["management_status"] or "active").strip().lower()
    if management_status == "retired":
        return json_error("PAIRING_RETIRED", "That terminal has been retired.", status=409)
    if management_status == "suspended":
        return json_error("PAIRING_SUSPENDED", "That terminal is suspended and cannot be paired right now.", status=409)

    business_account_id = str(terminal_row["business_account_public_id"])
    site_public_id = str(terminal_row["site_public_id"])
    terminal_device_id = str(terminal_row["terminal_device_public_id"])
    business_row = platform_query_one(
        "select * from cloud_business_accounts where business_account_public_id = ?",
        (business_account_id,),
    )
    site_row = platform_query_one(
        "select * from cloud_business_sites where site_public_id = ?",
        (site_public_id,),
    )
    if business_row is None or site_row is None:
        return json_error("PAIRING_INVALID", "The pairing code points to incomplete platform records.", status=409)
    access_allowed, licence_status, access_message = terminal_access_state(business_row, site_row, terminal_row)
    if not access_allowed:
        return json_error("PAIRING_SUSPENDED", access_message or "This business cannot pair devices right now.", status=409)

    now_text = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    expires_at = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    signed_token = f"sm-lease:{terminal_device_id}:{now_text}"
    platform_execute(
        """
        update cloud_terminal_registry
        set status = 'paired',
            last_seen_at = ?,
            updated_at = current_timestamp
        where terminal_device_public_id = ?
        """,
        (now_text, terminal_device_id),
    )
    upsert_cloud_device_licence(
        business_account_public_id=business_account_id,
        terminal_device_public_id=terminal_device_id,
        licence_status=licence_status,
        signed_token=signed_token,
        issued_at=now_text,
        expires_at=expires_at,
        last_check_in_at=now_text,
    )

    return jsonify(
        {
            "ok": True,
            "data": {
                "business_account_public_id": business_account_id,
                "business_name": business_row["business_name"],
                "site_public_id": site_public_id,
                "site_name": site_row["site_name"],
                "terminal_device_public_id": terminal_device_id,
                "terminal_name": terminal_row["terminal_name"],
                "install_mode": str(terminal_row["install_mode"] or "fresh_install"),
                "device_serial": device_serial,
                "licence_status": licence_status,
                "signed_token": signed_token,
                "issued_at": now_text,
                "expires_at": expires_at,
            },
        }
    )


@app.post("/v1/licence/check-in")
def licence_check_in():
    ensure_platform_sync_tables()
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return json_error("BAD_REQUEST", "Request body must be valid JSON.")

    terminal_device_id = str(payload.get("terminal_device_id") or "").strip()
    if not terminal_device_id:
        return json_error("BAD_REQUEST", "terminal_device_id is required.")

    device_header = str(request.headers.get("X-SalonMax-Device-Id") or "").strip()
    if device_header and device_header != terminal_device_id:
        return json_error("DEVICE_MISMATCH", "Device header does not match body.", status=403)

    token = bearer_token_value()
    if not token_is_supported(token):
        return json_error("AUTH_INVALID", "Unsupported token format.", status=403)

    business_account_id = str(request.headers.get("X-SalonMax-Business-Id") or "").strip() or "biz_local_dev"
    app_version = str(payload.get("app_version") or "").strip()
    sync_health = payload.get("sync_health") if isinstance(payload.get("sync_health"), dict) else {}
    ensure_cloud_business_account(business_account_id, f"Imported {business_account_id}")
    business_row = platform_query_one(
        "select * from cloud_business_accounts where business_account_public_id = ?",
        (business_account_id,),
    )
    access_allowed, licence_status, access_message = business_access_state(business_row)
    now_text = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    expires_at = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    signed_token = f"sm-lease:{terminal_device_id}:{now_text}"
    ensure_cloud_runtime_terminal_record(
        business_account_public_id=business_account_id,
        terminal_device_public_id=terminal_device_id,
        payload=payload,
        app_version=app_version,
        seen_at=now_text,
    )
    terminal_row = platform_query_one(
        "select * from cloud_terminal_registry where terminal_device_public_id = ?",
        (terminal_device_id,),
    )
    site_row = None
    if terminal_row is not None:
        site_row = platform_query_one(
            "select * from cloud_business_sites where site_public_id = ?",
            (str(terminal_row["site_public_id"] or "").strip(),),
        )
    access_allowed, licence_status, access_message = terminal_access_state(business_row, site_row, terminal_row)
    update_cloud_terminal_sync_health(terminal_device_id, sync_health)

    upsert_cloud_device_licence(
        business_account_public_id=business_account_id,
        terminal_device_public_id=terminal_device_id,
        licence_status=licence_status,
        signed_token=signed_token,
        issued_at=now_text,
        expires_at=expires_at,
        last_check_in_at=now_text,
    )

    return jsonify(
        {
            "ok": True,
            "data": {
                "licence_status": licence_status,
                "signed_token": signed_token,
                "issued_at": now_text,
                "expires_at": expires_at,
                "grace_ends_at": (datetime.utcnow() + timedelta(days=37)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "access_message": access_message,
            },
        }
    )


@app.post("/v1/sync/events/push")
def sync_events_push():
    ensure_platform_sync_tables()
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return json_error("BAD_REQUEST", "Request body must be valid JSON.")

    terminal_device_id = str(payload.get("terminal_device_id") or "").strip()
    events = payload.get("events")
    sync_health = payload.get("sync_health") if isinstance(payload.get("sync_health"), dict) else {}
    if not terminal_device_id:
        return json_error("BAD_REQUEST", "terminal_device_id is required.")
    if not isinstance(events, list) or not events:
        return json_error("BAD_REQUEST", "events must be a non-empty list.")

    business_header = str(request.headers.get("X-SalonMax-Business-Id") or "").strip()
    device_header = str(request.headers.get("X-SalonMax-Device-Id") or "").strip()
    token = bearer_token_value()
    if device_header and device_header != terminal_device_id:
        return json_error("DEVICE_MISMATCH", "Device header does not match body.", status=403)

    accepted = []
    rejected = []
    db = get_platform_db()

    for event in events:
        if not isinstance(event, dict):
            rejected.append({"local_event_id": "", "reason": "Event item must be an object."})
            continue

        local_event_id = str(event.get("local_event_id") or "").strip()
        event_type = str(event.get("event_type") or "").strip()
        occurred_at = str(event.get("created_at") or "").strip() or datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        event_business_id = str(event.get("business_account_id") or business_header or "").strip()
        event_payload = event.get("payload")

        if not local_event_id or not event_type:
            rejected.append(
                {
                    "local_event_id": local_event_id,
                    "reason": "local_event_id and event_type are required.",
                }
            )
            continue

        if not event_business_id:
            rejected.append(
                {
                    "local_event_id": local_event_id,
                    "reason": "business_account_id is required.",
                }
            )
            continue
        ensure_cloud_business_account(event_business_id, f"Imported {event_business_id}")
        ensure_cloud_runtime_terminal_record(
            business_account_public_id=event_business_id,
            terminal_device_public_id=terminal_device_id,
            payload=event_payload if isinstance(event_payload, dict) else {},
            seen_at=occurred_at,
        )
        business_row = platform_query_one(
            "select * from cloud_business_accounts where business_account_public_id = ?",
            (event_business_id,),
        )
        terminal_row = platform_query_one(
            "select * from cloud_terminal_registry where terminal_device_public_id = ?",
            (terminal_device_id,),
        )
        site_row = None
        if terminal_row is not None:
            site_row = platform_query_one(
                "select * from cloud_business_sites where site_public_id = ?",
                (str(terminal_row["site_public_id"] or "").strip(),),
            )
        access_allowed, _, access_message = terminal_access_state(business_row, site_row, terminal_row)
        if not access_allowed:
            rejected.append(
                {
                    "local_event_id": local_event_id,
                    "reason": access_message or "Business is suspended.",
                }
            )
            continue
        if not token_is_supported(token):
            rejected.append(
                {
                    "local_event_id": local_event_id,
                    "reason": "Unsupported token format.",
                }
            )
            continue

        payload_json = json.dumps(event_payload if event_payload is not None else {}, separators=(",", ":"), sort_keys=True)
        existing = db.execute(
            """
            select id
            from cloud_sync_events
            where business_account_public_id = ?
              and terminal_device_public_id = ?
              and local_event_id = ?
            """,
            (event_business_id, terminal_device_id, local_event_id),
        ).fetchone()
        if existing is None:
            db.execute(
                """
                insert into cloud_sync_events (
                    business_account_public_id,
                    terminal_device_public_id,
                    local_event_id,
                    event_type,
                    occurred_at,
                    payload_json
                ) values (?, ?, ?, ?, ?, ?)
                """,
                (event_business_id, terminal_device_id, local_event_id, event_type, occurred_at, payload_json),
            )
            project_sync_event_to_ledgers(
                db,
                business_account_public_id=event_business_id,
                terminal_device_public_id=terminal_device_id,
                local_event_id=local_event_id,
                event_type=event_type,
                occurred_at=occurred_at,
                payload=event_payload if isinstance(event_payload, dict) else {},
            )
        accepted.append(
            {
                "local_event_id": local_event_id,
                "cloud_event_id": local_event_id,
            }
        )

    db.commit()
    if terminal_device_id:
        update_cloud_terminal_sync_health(terminal_device_id, sync_health)
    return jsonify(
        {
            "ok": True,
            "data": {
                "accepted": accepted,
                "rejected": rejected,
            },
            "meta": {
                "terminal_device_id": terminal_device_id,
                "business_account_id": business_header or "",
                "accepted_count": len(accepted),
                "rejected_count": len(rejected),
            },
        }
    )


def send_management_email(subject: str, body: str):
    settings = email_report_settings()
    if not int(settings.get("email_reports_enabled") or 0):
        return False, "Email reporting is disabled."

    recipients = recipient_list(settings.get("management_report_emails", ""))
    smtp_host = (settings.get("smtp_host") or "").strip()
    from_email = (settings.get("report_from_email") or settings.get("smtp_username") or "").strip()

    if not recipients:
        return False, "No management email addresses are configured."
    if not smtp_host:
        return False, "SMTP host is not configured."
    if not from_email:
        return False, "From email is not configured."

    port = int(settings.get("smtp_port") or 587)
    username = (settings.get("smtp_username") or "").strip()
    password = settings.get("smtp_password") or ""
    use_tls = int(settings.get("smtp_use_tls") or 0) == 1

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_email
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    try:
        with smtplib.SMTP(smtp_host, port, timeout=20) as server:
            server.ehlo()
            if use_tls:
                server.starttls()
                server.ehlo()
            if username:
                server.login(username, password)
            server.send_message(message)
    except Exception as exc:
        return False, str(exc)
    return True, f"Email sent to {', '.join(recipients)}"


def till_session_detail_row(session_id: int):
    return query_one(
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
        (session_id,),
    )


def send_shift_summary_email_for_session(session_id: int):
    session_row = till_session_detail_row(session_id)
    if session_row is None:
        return False, "Till session not found."

    totals = detailed_totals_between(
        session_row["opened_at"],
        session_row["closed_at"] or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        site_id=session_row["site_id"],
        terminal_id=session_row["terminal_id"],
    )
    subject = (
        f"Shift Summary - {session_row['site_name']} / {session_row['terminal_name']} - "
        f"{(session_row['closed_at'] or session_row['opened_at'])[:10]}"
    )
    body = build_shift_summary_text(session_row, totals)
    return send_management_email(subject, body)


def send_daily_summary_email_for_date(day_value):
    start_text = f"{day_value.isoformat()} 00:00:00"
    end_text = f"{day_value.isoformat()} 23:59:59"
    totals = detailed_totals_between(start_text, end_text)
    subject = f"Daily Summary - {day_value.isoformat()}"
    body = build_daily_summary_text(day_value, totals)
    return send_management_email(subject, body)


def next_transaction_number():
    row = query_one("select transaction_number from transactions order by id desc limit 1")
    if row is None:
        return "TX-1001"
    current = row["transaction_number"].split("-")[-1]
    return f"TX-{int(current) + 1}"


def seed_data():
    db = get_db()

    if query_one("select id from sites limit 1") is None:
        db.executemany(
            "insert into sites (name, code, is_active) values (?, ?, 1)",
            [
                ("Liscard Road 2", "LR2"),
            ],
        )

    if query_one("select id from devices limit 1") is None:
        db.executemany(
            "insert into devices (site_id, device_number, device_name, is_active) values (?, ?, ?, 1)",
            [
                (1, 1, "Megasun Hurricane"),
                (1, 5, "Ergoline Prestige"),
            ],
        )

    if query_one("select id from terminals limit 1") is None:
        db.executemany(
            "insert into terminals (site_id, name, is_active) values (?, ?, 1)",
            [
                (1, "Till 1"),
            ],
        )

    if query_one("select id from staff_users limit 1") is None:
        db.executemany(
            "insert into staff_users (name, pin_code, role, is_active) values (?, ?, ?, 1)",
            [
                ("Ruby", "1111", "staff"),
                ("Lily", "2222", "staff"),
                ("Georgi", "3333", "staff"),
                ("Manager", "9999", "manager"),
            ],
        )

    if query_one("select id from pricing_rules limit 1") is None:
        db.executemany(
            "insert into pricing_rules (site_id, device_id, price_per_minute, is_active) values (?, ?, ?, 1)",
            [
                (1, None, 0.55),
            ],
        )

    if query_one("select id from package_products limit 1") is None:
        db.executemany(
            """
            insert into package_products (
                name,
                code,
                minutes_included,
                price,
                validity_days,
                is_active
            ) values (?, ?, ?, ?, ?, 1)
            """,
            [
                ("30 Minute Package", "PKG30", 30, 30.0, 365),
                ("45 Minute Package", "PKG45", 45, 45.0, 365),
                ("60 Minute Package", "PKG60", 60, 60.0, 365),
            ],
        )

    if query_one("select id from product_groups limit 1") is None:
        db.executemany(
            "insert into product_groups (name, sort_order, is_active) values (?, ?, 1)",
            [
                ("Creams", 10),
                ("Lotions", 20),
                ("Accelerators", 30),
            ],
        )

    if query_one("select id from retail_products limit 1") is None:
        db.executemany(
            """
            insert into retail_products (
                group_id,
                name,
                sku,
                size_label,
                unit_label,
                price,
                stock_quantity,
                commission_rate,
                is_active
            ) values (?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            [
                (1, "Cream POT", "4474-12730", "30", "pcs / ml", 2.00, 18, 0),
                (2, "Bronzing Lotion", "BRNZ-200", "200", "ml", 18.00, 12, 5),
                (3, "Rapid Accelerator", "RAP-150", "150", "ml", 22.50, 8, 5),
            ],
        )

    if query_one("select id from customers limit 1") is None:
        db.executemany(
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
            ) values (?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            [
                ("1001", "1775727870", "Emma", "Owens", "", "", 0.0, 0),
                ("1002", "1775727999", "Sarah", "Jones", "", "", 12.5, 30),
            ],
        )

    db.commit()


def ensure_single_site_mode():
    db = get_db()
    active_sites = query_all("select id from sites where is_active = 1 order by id")
    if len(active_sites) <= 1:
        return

    keep_site_id = int(active_sites[0]["id"])
    other_site_ids = [int(row["id"]) for row in active_sites[1:]]
    placeholders = ",".join("?" for _ in other_site_ids)

    db.execute(f"update sites set is_active = 0 where id in ({placeholders})", other_site_ids)
    db.execute(f"update devices set is_active = 0 where site_id in ({placeholders})", other_site_ids)
    db.execute(f"update terminals set is_active = 0 where site_id in ({placeholders})", other_site_ids)
    db.execute(f"update pricing_rules set is_active = 0 where site_id in ({placeholders})", other_site_ids)
    db.execute(f"update sunbeds set is_active = 0 where site_id in ({placeholders})", other_site_ids)
    db.commit()


def init_db():
    db = connect(DATABASE_PATH, schema="salon_config")
    with open(SCHEMA_PATH, "r", encoding="utf-8") as schema_file:
        db.executescript(schema_file.read())
    db.commit()
    db.close()

    with app.app_context():
        ensure_business_settings_table()
        ensure_platform_sync_tables()
        if APP_ROLE != "cloud":
            ensure_sunbed_tables()
            seed_data()
            seed_sunbeds()
            ensure_single_site_mode()
            ensure_basic_sunbed_defaults()
            seed_local_business_account_record()


def run_app():
    init_db()
    debug_enabled = os.environ.get("SALONMAX_DEBUG", "").strip() == "1"
    bind_host = str(os.environ.get("SALONMAX_BIND_HOST") or "0.0.0.0").strip() or "0.0.0.0"
    bind_port_text = str(os.environ.get("SALONMAX_PORT") or "5001").strip() or "5001"
    try:
        bind_port = int(bind_port_text)
    except ValueError:
        bind_port = 5001
    app.run(host=bind_host, port=bind_port, debug=debug_enabled)


def dashboard_totals():
    totals = {}
    totals["customer_count"] = query_one("select count(*) as value from customers")["value"]
    totals["active_staff_count"] = query_one(
        "select count(*) as value from staff_users where is_active = 1"
    )["value"]
    totals["site_count"] = query_one("select count(*) as value from sites where is_active = 1")["value"]
    totals["device_count"] = query_one(
        "select count(*) as value from devices where is_active = 1"
    )["value"]
    totals["open_till_count"] = query_one(
        "select count(*) as value from till_sessions where status = 'open'"
    )["value"]
    totals["transaction_count"] = query_one("select count(*) as value from transactions")["value"]
    totals["sales_total"] = query_one(
        "select coalesce(sum(total_amount), 0) as value from transactions where status = 'completed'"
    )["value"]
    totals["package_minutes_total"] = query_one(
        "select coalesce(sum(package_minutes), 0) as value from customers"
    )["value"]
    totals["account_balance_total"] = query_one(
        "select coalesce(sum(account_balance), 0) as value from customers"
    )["value"]
    totals["active_product_count"] = query_one(
        "select count(*) as value from retail_products where is_active = 1"
    )["value"]
    totals["stock_units_total"] = query_one(
        "select coalesce(sum(stock_quantity), 0) as value from retail_products where is_active = 1"
    )["value"]
    return totals


def product_group_rows():
    return query_all(
        """
        select
            product_groups.*,
            count(retail_products.id) as product_count
        from product_groups
        left join retail_products
            on retail_products.group_id = product_groups.id
           and retail_products.is_active = 1
        where product_groups.is_active = 1
        group by product_groups.id
        order by product_groups.sort_order, product_groups.name
        """
    )


def retail_product_rows():
    return query_all(
        """
        select
            retail_products.*,
            product_groups.name as group_name
        from retail_products
        left join product_groups on product_groups.id = retail_products.group_id
        where retail_products.is_active = 1
        order by coalesce(product_groups.sort_order, 9999), product_groups.name, retail_products.name
        """
    )


def retail_product_rows_filtered(search_text=""):
    search_text = search_text.strip()
    if not search_text:
        return retail_product_rows()

    like = f"%{search_text}%"
    return query_all(
        """
        select
            retail_products.*,
            product_groups.name as group_name
        from retail_products
        left join product_groups on product_groups.id = retail_products.group_id
        where retail_products.is_active = 1
          and (
            retail_products.name like ?
            or retail_products.sku like ?
            or coalesce(product_groups.name, '') like ?
          )
        order by coalesce(product_groups.sort_order, 9999), product_groups.name, retail_products.name
        """,
        (like, like, like),
    )


def stock_adjustment_rows():
    return query_all(
        """
        select
            stock_adjustments.*,
            retail_products.name as product_name,
            retail_products.sku as product_sku
        from stock_adjustments
        left join retail_products on retail_products.id = stock_adjustments.product_id
        order by stock_adjustments.id desc
        limit 40
        """
    )


@app.route("/")
def dashboard():
    return render_template(
        "dashboard.html",
        totals=dashboard_totals(),
        sites=query_all("select * from sites order by name"),
        staff=query_all("select * from staff_users order by name limit 6"),
    )


@app.route("/salonmax-platform")
def salonmax_platform():
    return redirect(url_for("salonmax_owner_console"))


@app.route("/platform-login", methods=["GET", "POST"])
def salonmax_platform_login():
    notice = ""
    next_url = request.values.get("next", "/platform").strip() or "/platform"
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/platform"

    configured_username = os.environ.get("SALONMAX_PLATFORM_ADMIN_USERNAME", "admin").strip() or "admin"
    configured_password = os.environ.get("SALONMAX_PLATFORM_ADMIN_PASSWORD", "").strip()

    if request.method == "POST":
        if not configured_password:
            notice = "Platform login is not configured. Set SALONMAX_PLATFORM_ADMIN_PASSWORD before exposing Salon Max online."
        else:
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            username_ok = hmac.compare_digest(username, configured_username)
            password_ok = hmac.compare_digest(password, configured_password)
            if username_ok and password_ok:
                session["platform_admin_authenticated"] = True
                session["salonmax_platform_authed"] = True
                session["platform_admin_username"] = username
                return redirect(next_url)
            notice = "Login failed. Check the Salon Max owner username and password."

    return render_template(
        "platform_login.html",
        title="Salon Max Platform Login",
        notice=notice or request.args.get("notice", "").strip(),
        next_url=next_url,
        auth_configured=bool(configured_password),
        configured_username=configured_username,
    )


@app.route("/platform-logout")
def salonmax_platform_logout():
    session.pop("platform_admin_authenticated", None)
    session.pop("salonmax_platform_authed", None)
    session.pop("platform_admin_username", None)
    return redirect(url_for("salonmax_platform_login", notice="Signed out."))


@app.route("/platform")
@app.route("/platform/owner")
def salonmax_owner_console():
    return render_template(
        "salonmax_platform_accounts.html",
        snapshot=salonmax_business_accounts_snapshot(
            search_text=request.args.get("search", "").strip(),
        ),
        title="Salon Max Platform",
        notice=request.args.get("notice", "").strip(),
    )


@app.route("/platform/onboard")
def salonmax_onboard_business():
    return render_template(
        "salonmax_onboard_business.html",
        title="Salon Max Onboarding",
        notice=request.args.get("notice", "").strip(),
        pairing_code=request.args.get("pairing_code", "").strip(),
    )


@app.route("/platform/queries")
def salonmax_owner_queries():
    snapshot = salonmax_owner_queries_snapshot(
        business_account_public_id=request.args.get("business_account_public_id", "").strip(),
        terminal_device_public_id=request.args.get("terminal_device_public_id", "").strip(),
        transaction_type=request.args.get("transaction_type", "").strip(),
        payment_method=request.args.get("payment_method", "").strip(),
        transaction_search=request.args.get("transaction_search", "").strip(),
        customer_search=request.args.get("customer_search", "").strip(),
        days=request.args.get("days", "30").strip() or 30,
    )
    return render_template(
        "salonmax_queries.html",
        snapshot=snapshot,
        title="Salon Max Queries",
        notice=request.args.get("notice", "").strip(),
    )


@app.route("/platform/queries/transactions.csv")
def salonmax_owner_queries_transactions_export():
    snapshot = salonmax_owner_queries_snapshot(
        business_account_public_id=request.args.get("business_account_public_id", "").strip(),
        terminal_device_public_id=request.args.get("terminal_device_public_id", "").strip(),
        transaction_type=request.args.get("transaction_type", "").strip(),
        payment_method=request.args.get("payment_method", "").strip(),
        transaction_search=request.args.get("transaction_search", "").strip(),
        customer_search=request.args.get("customer_search", "").strip(),
        days=request.args.get("days", "30").strip() or 30,
    )
    headers = [
        "Occurred At",
        "Business",
        "Business Account ID",
        "Terminal",
        "Terminal Device ID",
        "Transaction Number",
        "Local Event ID",
        "Customer",
        "Customer Public ID",
        "Transaction Type",
        "Payment Method",
        "Minutes",
        "Account Minutes Used",
        "Package Name",
        "Total Amount",
        "Notes",
    ]
    rows = [
        [
            row["occurred_at"],
            row["business_name"],
            row["business_account_public_id"],
            row["terminal_name"],
            row["terminal_device_public_id"],
            row["transaction_number"],
            row["local_event_id"],
            row["customer_label"],
            row["customer_public_id"],
            row["transaction_type"],
            row["payment_method"],
            row["minutes"],
            row["account_minutes_used"],
            row["package_name"],
            f"{float(row['total_amount']):.2f}",
            row["notes"],
        ]
        for row in snapshot["transactions"]
    ]
    return csv_download_response("salonmax-transactions-export.csv", headers, rows)


@app.route("/platform/queries/customers.csv")
def salonmax_owner_queries_customers_export():
    snapshot = salonmax_owner_queries_snapshot(
        business_account_public_id=request.args.get("business_account_public_id", "").strip(),
        terminal_device_public_id=request.args.get("terminal_device_public_id", "").strip(),
        transaction_type=request.args.get("transaction_type", "").strip(),
        payment_method=request.args.get("payment_method", "").strip(),
        transaction_search=request.args.get("transaction_search", "").strip(),
        customer_search=request.args.get("customer_search", "").strip(),
        days=request.args.get("days", "30").strip() or 30,
    )
    headers = [
        "Business",
        "Business Account ID",
        "Customer",
        "Customer Number",
        "Customer Public ID",
        "Phone",
        "Email",
        "Minutes Available",
        "Current Balance",
        "Currency",
        "Status",
    ]
    rows = [
        [
            row["business_name"],
            row["business_account_public_id"],
            row["full_name"],
            row["customer_number"],
            row["customer_public_id"],
            row["phone"],
            row["email"],
            row["minutes_available"],
            row["current_balance"],
            row["currency_code"],
            row["status"],
        ]
        for row in snapshot["customers"]
    ]
    return csv_download_response("salonmax-customers-export.csv", headers, rows)


@app.route("/platform/analytics")
def salonmax_owner_analytics():
    snapshot = salonmax_owner_analytics_snapshot(
        business_account_public_id=request.args.get("business_account_public_id", "").strip(),
        days=request.args.get("days", "30").strip() or 30,
    )
    return render_template(
        "salonmax_analytics.html",
        snapshot=snapshot,
        title="Salon Max Analytics",
        notice=request.args.get("notice", "").strip(),
    )


@app.route("/platform/customer-insights")
def salonmax_owner_customer_insights():
    snapshot = salonmax_owner_customer_insights_snapshot(
        business_account_public_id=request.args.get("business_account_public_id", "").strip(),
        days=request.args.get("days", "90").strip() or 90,
    )
    return render_template(
        "salonmax_customer_insights.html",
        snapshot=snapshot,
        title="Salon Max Customer Insights",
        notice=request.args.get("notice", "").strip(),
    )


@app.route("/platform/updates")
def salonmax_owner_updates():
    snapshot = salonmax_owner_updates_snapshot(
        business_account_public_id=request.args.get("business_account_public_id", "").strip(),
        version_filter=request.args.get("version_filter", "").strip(),
        sync_filter=request.args.get("sync_filter", "").strip(),
    )
    return render_template(
        "salonmax_updates.html",
        snapshot=snapshot,
        title="Salon Max Updates",
        notice=request.args.get("notice", "").strip(),
    )


@app.route("/platform/licences")
def salonmax_owner_licences():
    snapshot = salonmax_owner_licences_snapshot(
        business_account_public_id=request.args.get("business_account_public_id", "").strip(),
        health_filter=request.args.get("health_filter", "").strip(),
    )
    return render_template(
        "salonmax_licences.html",
        snapshot=snapshot,
        title="Salon Max Licences",
        notice=request.args.get("notice", "").strip(),
    )


@app.route("/platform/diagnostics")
def salonmax_owner_diagnostics():
    snapshot = salonmax_owner_updates_snapshot(
        business_account_public_id=request.args.get("business_account_public_id", "").strip(),
    )
    return render_template(
        "salonmax_diagnostics_index.html",
        snapshot=snapshot,
        title="Salon Max Diagnostics",
        notice=request.args.get("notice", "").strip(),
    )


@app.route("/platform/terminal/<terminal_device_public_id>")
def salonmax_terminal_diagnostics(terminal_device_public_id: str):
    snapshot = salonmax_terminal_diagnostics_snapshot(terminal_device_public_id)
    if snapshot is None:
        return json_error("TERMINAL_NOT_FOUND", "No Salon Max terminal was found for that id.", status=404)
    return render_template(
        "salonmax_terminal_diagnostics.html",
        snapshot=snapshot,
        title="Salon Max Terminal Diagnostics",
        notice=request.args.get("notice", "").strip(),
    )


@app.route("/platform/business/<business_account_public_id>")
def salonmax_owner_business_detail(business_account_public_id: str):
    snapshot = salonmax_platform_snapshot(business_account_public_id)
    if snapshot is None:
        return json_error("BUSINESS_NOT_FOUND", "No Salon Max business account was found for that id.", status=404)
    return render_template(
        "salonmax_platform.html",
        snapshot=snapshot,
        title="Salon Max Platform",
        notice=request.args.get("notice", "").strip(),
        pairing_code=request.args.get("pairing_code", "").strip(),
    )


@app.post("/platform/business/<business_account_public_id>/support-notes/create")
def salonmax_create_support_note(business_account_public_id: str):
    ensure_platform_sync_tables()
    account_row = platform_query_one(
        "select * from cloud_business_accounts where business_account_public_id = ?",
        (business_account_public_id,),
    )
    if account_row is None:
        return redirect(url_for("salonmax_owner_console", notice="Business not found."))
    author_name = request.form.get("author_name", "").strip()
    note_type = request.form.get("note_type", "").strip() or "support_note"
    terminal_device_public_id = request.form.get("terminal_device_public_id", "").strip()
    note_text = request.form.get("note_text", "").strip()
    if not note_text:
        return redirect(
            url_for(
                "salonmax_owner_business_detail",
                business_account_public_id=business_account_public_id,
                notice="Support note text is required.",
            )
        )
    platform_execute(
        """
        insert into cloud_support_notes (
            business_account_public_id,
            terminal_device_public_id,
            customer_public_id,
            note_type,
            author_name,
            note_text
        ) values (?, ?, '', ?, ?, ?)
        """,
        (business_account_public_id, terminal_device_public_id, note_type, author_name, note_text),
    )
    return redirect(
        url_for(
            "salonmax_owner_business_detail",
            business_account_public_id=business_account_public_id,
            notice="Support note saved.",
        )
    )


@app.post("/platform/business/<business_account_public_id>/customers/import")
def salonmax_import_business_customers(business_account_public_id: str):
    ensure_platform_sync_tables()
    business_row = platform_query_one(
        "select * from cloud_business_accounts where business_account_public_id = ?",
        (business_account_public_id,),
    )
    if business_row is None:
        return redirect(url_for("salonmax_owner_console", notice="Business not found."))
    csv_text = request.form.get("csv_text", "").strip()
    upload = request.files.get("csv_file")
    if upload and upload.filename:
        if csv_text:
            return redirect(url_for("salonmax_owner_business_detail", business_account_public_id=business_account_public_id,
                                    notice="Choose either a CSV file or pasted CSV data."))
        data = upload.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            return redirect(url_for("salonmax_owner_business_detail", business_account_public_id=business_account_public_id,
                                    notice="The CSV file must be 1 MB or smaller."))
        try:
            csv_text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return redirect(url_for("salonmax_owner_business_detail", business_account_public_id=business_account_public_id,
                                    notice="Save the CSV as UTF-8 and try again."))
    author_name = request.form.get("author_name", "").strip()
    if not csv_text:
        return redirect(
            url_for(
                "salonmax_owner_business_detail",
                business_account_public_id=business_account_public_id,
                notice="Paste CSV customer data before importing.",
            )
        )
    if len(csv_text.encode("utf-8")) > 1024 * 1024:
        return redirect(url_for("salonmax_owner_business_detail", business_account_public_id=business_account_public_id,
                                notice="The CSV must be 1 MB or smaller."))
    try:
        parse_result = parse_customer_import_csv(csv_text)
    except csv.Error:
        return redirect(url_for("salonmax_owner_business_detail", business_account_public_id=business_account_public_id,
                                notice="The CSV could not be read. Check quotes and field lengths."))
    rows = parse_result["rows"]
    if not rows:
        return redirect(
            url_for(
                "salonmax_owner_business_detail",
                business_account_public_id=business_account_public_id,
                notice="No valid customer rows were found. " + " ".join(parse_result["warnings"][:4]),
            )
        )
    db = get_platform_db()
    created_count = 0
    updated_count = 0
    conflict_count = 0
    if postgres():
        db.raw.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (business_account_public_id,))
    source_reference = f"import:{business_account_public_id}:{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    for row in rows:
        result = upsert_cloud_import_customer(
            db,
            business_account_public_id=business_account_public_id,
            row=row,
            source_reference=source_reference,
            author_name=author_name,
        )
        if result.get("conflict"):
            conflict_count += 1
        elif result["created"]:
            created_count += 1
        else:
            updated_count += 1
    db.commit()
    warning_bits = []
    if conflict_count:
        warning_bits.append(f"Conflicting matches skipped: {conflict_count}")
    if int(parse_result["skipped_count"] or 0) > 0:
        warning_bits.append(f"Skipped: {int(parse_result['skipped_count'])}")
    if int(parse_result["error_count"] or 0) > 0:
        warning_bits.append(f"Invalid rows skipped: {int(parse_result['error_count'])}")
    warning_suffix = f" ({', '.join(warning_bits)})" if warning_bits else ""
    save_cloud_support_note(
        business_account_public_id,
        note_type="migration_import",
        author_name=author_name,
        note_text=f"Customer import completed. Created: {created_count}. Updated: {updated_count}.{warning_suffix} Source ref: {source_reference}.",
    )
    warning_text = ""
    if parse_result["warnings"]:
        warning_text = " Warnings: " + " | ".join(parse_result["warnings"][:4])
    return redirect(
        url_for(
            "salonmax_owner_business_detail",
            business_account_public_id=business_account_public_id,
            notice=f"Customer import complete. Created: {created_count}. Updated: {updated_count}.{warning_suffix}{warning_text}",
        )
    )


@app.route("/platform/business/<business_account_public_id>/customers/<customer_public_id>")
def salonmax_customer_ledger_detail(business_account_public_id: str, customer_public_id: str):
    snapshot = salonmax_customer_ledger_snapshot(business_account_public_id, customer_public_id)
    if snapshot is None:
        return json_error("CUSTOMER_NOT_FOUND", "No cloud customer ledger record was found for that id.", status=404)
    return render_template(
        "salonmax_customer_ledger.html",
        snapshot=snapshot,
        title="Salon Max Customer Ledger",
        notice=request.args.get("notice", "").strip(),
    )


@app.post("/platform/business/<business_account_public_id>/customers/<customer_public_id>/adjust")
def salonmax_customer_ledger_adjust(business_account_public_id: str, customer_public_id: str):
    ensure_platform_sync_tables()
    db = get_platform_db()
    snapshot = salonmax_customer_ledger_snapshot(business_account_public_id, customer_public_id)
    if snapshot is None:
        return redirect(url_for("salonmax_owner_console", notice="Customer ledger not found."))

    ledger_kind = request.form.get("ledger_kind", "").strip()
    adjustment_type = request.form.get("adjustment_type", "").strip() or "manual_credit"
    author_name = request.form.get("author_name", "").strip()
    reason_category = request.form.get("reason_category", "").strip() or "correction"
    transaction_reference = request.form.get("transaction_reference", "").strip()
    reason = request.form.get("reason", "").strip()
    terminal_device_public_id = request.form.get("terminal_device_public_id", "").strip()
    occurred_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    source_reference = transaction_reference or f"manual:{reason_category}:{adjustment_type}:{author_name or 'unknown'}"

    if not reason:
        return redirect(
            url_for(
                "salonmax_customer_ledger_detail",
                business_account_public_id=business_account_public_id,
                customer_public_id=customer_public_id,
                notice="Reason is required for a ledger adjustment.",
            )
        )

    if ledger_kind == "minutes":
        raw_quantity = request.form.get("delta_minutes", "").strip()
        try:
            quantity = int(raw_quantity)
        except ValueError:
            quantity = 0
        if quantity == 0:
            return redirect(
                url_for(
                    "salonmax_customer_ledger_detail",
                    business_account_public_id=business_account_public_id,
                    customer_public_id=customer_public_id,
                    notice="Minutes adjustment must be a whole number and cannot be zero.",
                )
            )
        final_entry_type = normalise_manual_adjustment_entry_type(adjustment_type, quantity)
        summary_row = ensure_cloud_customer_minute_summary(db, business_account_public_id, customer_public_id)
        minutes_after = int(summary_row["minutes_available"] or 0) + quantity
        db.execute(
            """
            insert into cloud_customer_minute_ledger (
                business_account_public_id,
                customer_public_id,
                site_public_id,
                terminal_device_public_id,
                staff_user_public_id,
                source_event_id,
                source_reference,
                package_code,
                entry_type,
                delta_minutes,
                minutes_after,
                notes,
                created_at
            ) values (?, ?, '', ?, '', '', ?, '', ?, ?, ?, ?, ?)
            """,
            (
                business_account_public_id,
                customer_public_id,
                terminal_device_public_id,
                source_reference,
                final_entry_type,
                quantity,
                minutes_after,
                reason,
                occurred_at,
            ),
        )
        db.execute(
            """
            update cloud_customer_minute_summary
            set minutes_available = ?,
                updated_at = current_timestamp
            where business_account_public_id = ?
              and customer_public_id = ?
            """,
            (minutes_after, business_account_public_id, customer_public_id),
        )
        db.commit()
        save_cloud_support_note(
            business_account_public_id,
            terminal_device_public_id=terminal_device_public_id,
            customer_public_id=customer_public_id,
            note_type="ledger_adjustment",
            author_name=author_name,
            note_text=(
                f"Minute ledger {final_entry_type.replace('_', ' ')} for {snapshot['full_name']}: "
                f"{quantity:+d} mins. Category: {reason_category.replace('_', ' ')}. "
                f"{'Reference: ' + transaction_reference + '. ' if transaction_reference else ''}"
                f"Reason: {reason}"
            ),
        )
        return redirect(
            url_for(
                "salonmax_customer_ledger_detail",
                business_account_public_id=business_account_public_id,
                customer_public_id=customer_public_id,
                notice="Minute ledger updated.",
            )
        )

    if ledger_kind == "balance":
        raw_quantity = request.form.get("delta_amount", "").strip()
        try:
            quantity = round(float(raw_quantity), 2)
        except ValueError:
            quantity = 0
        if quantity == 0:
            return redirect(
                url_for(
                    "salonmax_customer_ledger_detail",
                    business_account_public_id=business_account_public_id,
                    customer_public_id=customer_public_id,
                    notice="Balance adjustment must be a non-zero amount.",
                )
            )
        final_entry_type = normalise_manual_adjustment_entry_type(adjustment_type, quantity)
        summary_row = ensure_cloud_customer_balance_summary(db, business_account_public_id, customer_public_id)
        balance_after = round(float(summary_row["current_balance"] or 0) + quantity, 2)
        db.execute(
            """
            insert into cloud_customer_balance_ledger (
                business_account_public_id,
                customer_public_id,
                site_public_id,
                terminal_device_public_id,
                staff_user_public_id,
                source_event_id,
                source_reference,
                entry_type,
                delta_amount,
                balance_after,
                currency_code,
                notes,
                created_at
            ) values (?, ?, '', ?, '', '', ?, ?, ?, ?, 'GBP', ?, ?)
            """,
            (
                business_account_public_id,
                customer_public_id,
                terminal_device_public_id,
                source_reference,
                final_entry_type,
                f"{quantity:.2f}",
                f"{balance_after:.2f}",
                reason,
                occurred_at,
            ),
        )
        db.execute(
            """
            update cloud_customer_balance_summary
            set current_balance = ?,
                updated_at = current_timestamp
            where business_account_public_id = ?
              and customer_public_id = ?
            """,
            (f"{balance_after:.2f}", business_account_public_id, customer_public_id),
        )
        db.commit()
        save_cloud_support_note(
            business_account_public_id,
            terminal_device_public_id=terminal_device_public_id,
            customer_public_id=customer_public_id,
            note_type="ledger_adjustment",
            author_name=author_name,
            note_text=(
                f"Money ledger {final_entry_type.replace('_', ' ')} for {snapshot['full_name']}: "
                f"GBP {quantity:+.2f}. Category: {reason_category.replace('_', ' ')}. "
                f"{'Reference: ' + transaction_reference + '. ' if transaction_reference else ''}"
                f"Reason: {reason}"
            ),
        )
        return redirect(
            url_for(
                "salonmax_customer_ledger_detail",
                business_account_public_id=business_account_public_id,
                customer_public_id=customer_public_id,
                notice="Money ledger updated.",
            )
        )

    return redirect(
        url_for(
            "salonmax_customer_ledger_detail",
            business_account_public_id=business_account_public_id,
            customer_public_id=customer_public_id,
            notice="Unknown ledger adjustment type.",
        )
    )


@app.post("/platform/businesses/create")
def salonmax_create_business():
    ensure_platform_sync_tables()
    business_name = request.form.get("business_name", "").strip()
    subscription_plan = request.form.get("subscription_plan", "").strip() or "pilot"
    subscription_status = request.form.get("subscription_status", "").strip() or "active"
    contact_name = request.form.get("contact_name", "").strip()
    contact_email = request.form.get("contact_email", "").strip()
    contact_phone = request.form.get("contact_phone", "").strip()
    billing_email = request.form.get("billing_email", "").strip()
    company_number = request.form.get("company_number", "").strip()
    address_line_1 = request.form.get("address_line_1", "").strip()
    address_line_2 = request.form.get("address_line_2", "").strip()
    city = request.form.get("city", "").strip()
    county = request.form.get("county", "").strip()
    postcode = request.form.get("postcode", "").strip()
    billing_address_line_1 = request.form.get("billing_address_line_1", "").strip()
    billing_address_line_2 = request.form.get("billing_address_line_2", "").strip()
    billing_city = request.form.get("billing_city", "").strip()
    billing_county = request.form.get("billing_county", "").strip()
    billing_postcode = request.form.get("billing_postcode", "").strip()
    vat_number = request.form.get("vat_number", "").strip()
    contract_start_date = request.form.get("contract_start_date", "").strip()
    renewal_date = request.form.get("renewal_date", "").strip()
    monthly_fee = request.form.get("monthly_fee", "").strip()
    notes = request.form.get("notes", "").strip()
    if not business_name:
        return redirect(url_for("salonmax_owner_console", notice="Business name is required."))

    business_account_public_id = make_public_id("biz", business_name)
    suffix = 2
    while platform_query_one(
        "select id from cloud_business_accounts where business_account_public_id = ?",
        (business_account_public_id,),
    ) is not None:
        business_account_public_id = f"{make_public_id('biz', business_name)}-{suffix}"
        suffix += 1

    platform_execute(
        """
        insert into cloud_business_accounts (
            business_account_public_id,
            business_name,
            product_type,
            status,
            subscription_plan,
            subscription_status,
            contact_name,
            contact_email,
            contact_phone,
            billing_email,
            company_number,
            address_line_1,
            address_line_2,
            city,
            county,
            postcode,
            billing_address_line_1,
            billing_address_line_2,
            billing_city,
            billing_county,
            billing_postcode,
            vat_number,
            contract_start_date,
            renewal_date,
            monthly_fee,
            notes
        ) values (?, ?, 'salon', 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            business_account_public_id,
            business_name,
            subscription_plan,
            subscription_status,
            contact_name,
            contact_email,
            contact_phone,
            billing_email,
            company_number,
            address_line_1,
            address_line_2,
            city,
            county,
            postcode,
            billing_address_line_1,
            billing_address_line_2,
            billing_city,
            billing_county,
            billing_postcode,
            vat_number,
            contract_start_date,
            renewal_date,
            monthly_fee,
            notes,
        ),
    )
    if APP_ROLE == "cloud":
        from backoffice import setup
        setup(business_account_public_id, business_name, contact_email)
    return redirect(
        url_for(
            "salonmax_owner_business_detail",
            business_account_public_id=business_account_public_id,
            notice="Business created.",
        )
    )


@app.post("/platform/business/<business_account_public_id>/update")
def salonmax_update_business(business_account_public_id: str):
    ensure_platform_sync_tables()
    account_row = platform_query_one(
        "select * from cloud_business_accounts where business_account_public_id = ?",
        (business_account_public_id,),
    )
    if account_row is None:
        return redirect(url_for("salonmax_owner_console", notice="Business account not found."))

    business_name = request.form.get("business_name", "").strip()
    subscription_plan = request.form.get("subscription_plan", "").strip() or "pilot"
    subscription_status = request.form.get("subscription_status", "").strip() or "active"
    status = request.form.get("status", "").strip() or "active"
    contact_name = request.form.get("contact_name", "").strip()
    contact_email = request.form.get("contact_email", "").strip()
    contact_phone = request.form.get("contact_phone", "").strip()
    billing_email = request.form.get("billing_email", "").strip()
    company_number = request.form.get("company_number", "").strip()
    address_line_1 = request.form.get("address_line_1", "").strip()
    address_line_2 = request.form.get("address_line_2", "").strip()
    city = request.form.get("city", "").strip()
    county = request.form.get("county", "").strip()
    postcode = request.form.get("postcode", "").strip()
    billing_address_line_1 = request.form.get("billing_address_line_1", "").strip()
    billing_address_line_2 = request.form.get("billing_address_line_2", "").strip()
    billing_city = request.form.get("billing_city", "").strip()
    billing_county = request.form.get("billing_county", "").strip()
    billing_postcode = request.form.get("billing_postcode", "").strip()
    vat_number = request.form.get("vat_number", "").strip()
    contract_start_date = request.form.get("contract_start_date", "").strip()
    renewal_date = request.form.get("renewal_date", "").strip()
    monthly_fee = request.form.get("monthly_fee", "").strip()
    notes = request.form.get("notes", "").strip()
    if not business_name:
        return redirect(
            url_for(
                "salonmax_owner_business_detail",
                business_account_public_id=business_account_public_id,
                notice="Business name is required.",
            )
        )

    platform_execute(
        """
        update cloud_business_accounts
        set business_name = ?,
            subscription_plan = ?,
            subscription_status = ?,
            status = ?,
            contact_name = ?,
            contact_email = ?,
            contact_phone = ?,
            billing_email = ?,
            company_number = ?,
            address_line_1 = ?,
            address_line_2 = ?,
            city = ?,
            county = ?,
            postcode = ?,
            billing_address_line_1 = ?,
            billing_address_line_2 = ?,
            billing_city = ?,
            billing_county = ?,
            billing_postcode = ?,
            vat_number = ?,
            contract_start_date = ?,
            renewal_date = ?,
            monthly_fee = ?,
            notes = ?,
            updated_at = current_timestamp
        where business_account_public_id = ?
        """,
        (
            business_name,
            subscription_plan,
            subscription_status,
            status,
            contact_name,
            contact_email,
            contact_phone,
            billing_email,
            company_number,
            address_line_1,
            address_line_2,
            city,
            county,
            postcode,
            billing_address_line_1,
            billing_address_line_2,
            billing_city,
            billing_county,
            billing_postcode,
            vat_number,
            contract_start_date,
            renewal_date,
            monthly_fee,
            notes,
            business_account_public_id,
        ),
    )
    return redirect(
        url_for(
            "salonmax_owner_business_detail",
            business_account_public_id=business_account_public_id,
            notice="Business details updated.",
        )
    )


@app.post("/platform/business/<business_account_public_id>/archive")
def salonmax_archive_business(business_account_public_id: str):
    ensure_platform_sync_tables()
    account_row = platform_query_one(
        "select * from cloud_business_accounts where business_account_public_id = ?",
        (business_account_public_id,),
    )
    if account_row is None:
        return redirect(url_for("salonmax_owner_console", notice="Business account not found."))
    if str(account_row["status"] or "").strip() == "deleted":
        return redirect(url_for("salonmax_owner_console", notice="Business already archived."))
    platform_execute(
        """
        update cloud_business_accounts
        set status = 'deleted',
            updated_at = current_timestamp
        where business_account_public_id = ?
        """,
        (business_account_public_id,),
    )
    return redirect(url_for("salonmax_owner_console", notice="Business archived from the live directory. Its data has been kept for later restore if needed."))


@app.post("/platform/business/<business_account_public_id>/restore")
def salonmax_restore_business(business_account_public_id: str):
    ensure_platform_sync_tables()
    account_row = platform_query_one(
        "select * from cloud_business_accounts where business_account_public_id = ?",
        (business_account_public_id,),
    )
    if account_row is None:
        return redirect(url_for("salonmax_owner_console", notice="Business account not found."))
    if str(account_row["status"] or "").strip() != "deleted":
        return redirect(url_for("salonmax_owner_console", notice="Business is already live."))
    platform_execute(
        """
        update cloud_business_accounts
        set status = 'active',
            updated_at = current_timestamp
        where business_account_public_id = ?
        """,
        (business_account_public_id,),
    )
    return redirect(url_for("salonmax_owner_console", notice="Archived business restored to the live directory."))


@app.post("/platform/onboard/create")
def salonmax_create_business_with_first_site():
    ensure_platform_sync_tables()
    business_name = request.form.get("business_name", "").strip()
    subscription_plan = request.form.get("subscription_plan", "").strip() or "pilot"
    subscription_status = request.form.get("subscription_status", "").strip() or "active"
    contact_name = request.form.get("contact_name", "").strip()
    contact_email = request.form.get("contact_email", "").strip()
    contact_phone = request.form.get("contact_phone", "").strip()
    billing_email = request.form.get("billing_email", "").strip()
    company_number = request.form.get("company_number", "").strip()
    address_line_1 = request.form.get("address_line_1", "").strip()
    address_line_2 = request.form.get("address_line_2", "").strip()
    city = request.form.get("city", "").strip()
    county = request.form.get("county", "").strip()
    postcode = request.form.get("postcode", "").strip()
    billing_address_line_1 = request.form.get("billing_address_line_1", "").strip()
    billing_address_line_2 = request.form.get("billing_address_line_2", "").strip()
    billing_city = request.form.get("billing_city", "").strip()
    billing_county = request.form.get("billing_county", "").strip()
    billing_postcode = request.form.get("billing_postcode", "").strip()
    vat_number = request.form.get("vat_number", "").strip()
    contract_start_date = request.form.get("contract_start_date", "").strip()
    renewal_date = request.form.get("renewal_date", "").strip()
    monthly_fee = request.form.get("monthly_fee", "").strip()
    notes = request.form.get("notes", "").strip()
    site_name = request.form.get("site_name", "").strip()
    site_code = request.form.get("site_code", "").strip().upper()
    terminal_name = request.form.get("terminal_name", "").strip()

    if not business_name or not site_name or not terminal_name:
        return redirect(url_for("salonmax_onboard_business", notice="Business, site, and first terminal are required."))

    business_account_public_id = make_public_id("biz", business_name)
    suffix = 2
    while platform_query_one(
        "select id from cloud_business_accounts where business_account_public_id = ?",
        (business_account_public_id,),
    ) is not None:
        business_account_public_id = f"{make_public_id('biz', business_name)}-{suffix}"
        suffix += 1

    if not site_code:
        site_code = secure_filename(site_name).replace("_", "").upper()[:6] or "SITE"
    site_public_id = make_public_id("site", site_code.lower())
    site_suffix = 2
    while platform_query_one(
        "select id from cloud_business_sites where site_public_id = ?",
        (site_public_id,),
    ) is not None:
        site_public_id = f"{make_public_id('site', site_code.lower())}-{site_suffix}"
        site_suffix += 1

    terminal_base = make_public_id("term", f"{site_code.lower()}-{terminal_name}")
    terminal_device_public_id = terminal_base
    terminal_suffix = 2
    while platform_query_one(
        "select id from cloud_terminal_registry where terminal_device_public_id = ?",
        (terminal_device_public_id,),
    ) is not None:
        terminal_device_public_id = f"{terminal_base}-{terminal_suffix}"
        terminal_suffix += 1

    pairing_code = make_pairing_code()

    platform_execute(
        """
        insert into cloud_business_accounts (
            business_account_public_id,
            business_name,
            status,
            subscription_plan,
            subscription_status,
            contact_name,
            contact_email,
            contact_phone,
            billing_email,
            company_number,
            address_line_1,
            address_line_2,
            city,
            county,
            postcode,
            billing_address_line_1,
            billing_address_line_2,
            billing_city,
            billing_county,
            billing_postcode,
            vat_number,
            contract_start_date,
            renewal_date,
            monthly_fee,
            notes
        ) values (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            business_account_public_id,
            business_name,
            subscription_plan,
            subscription_status,
            contact_name,
            contact_email,
            contact_phone,
            billing_email,
            company_number,
            address_line_1,
            address_line_2,
            city,
            county,
            postcode,
            billing_address_line_1,
            billing_address_line_2,
            billing_city,
            billing_county,
            billing_postcode,
            vat_number,
            contract_start_date,
            renewal_date,
            monthly_fee,
            notes,
        ),
    )
    platform_execute(
        """
        insert into cloud_business_sites (
            business_account_public_id,
            site_public_id,
            site_name,
            site_code,
            status
        ) values (?, ?, ?, ?, 'active')
        """,
        (business_account_public_id, site_public_id, site_name, site_code),
    )
    platform_execute(
        """
        insert into cloud_terminal_registry (
            business_account_public_id,
            site_public_id,
            terminal_device_public_id,
            terminal_name,
            pairing_code,
            install_mode,
            status
        ) values (?, ?, ?, ?, ?, 'fresh_install', 'ready_to_pair')
        """,
        (
            business_account_public_id,
            site_public_id,
            terminal_device_public_id,
            terminal_name,
            pairing_code,
        ),
    )
    if APP_ROLE == "cloud":
        from backoffice import setup
        setup(business_account_public_id, business_name, contact_email)
    return redirect(
        url_for(
            "salonmax_owner_business_detail",
            business_account_public_id=business_account_public_id,
            notice="Business onboarded.",
            pairing_code=pairing_code,
        )
    )


@app.post("/platform/business/<business_account_public_id>/sites/create")
def salonmax_create_business_site(business_account_public_id: str):
    ensure_platform_sync_tables()
    account_row = platform_query_one(
        "select * from cloud_business_accounts where business_account_public_id = ?",
        (business_account_public_id,),
    )
    if account_row is None:
        return redirect(url_for("salonmax_owner_console", notice="Business account not found."))

    site_name = request.form.get("site_name", "").strip()
    site_code = request.form.get("site_code", "").strip().upper()
    if not site_name:
        return redirect(url_for("salonmax_owner_business_detail", business_account_public_id=business_account_public_id, notice="Site name is required."))
    if not site_code:
        site_code = secure_filename(site_name).replace("_", "").upper()[:6] or "SITE"

    site_public_id = make_public_id("site", site_code.lower())
    suffix = 2
    while platform_query_one(
        "select id from cloud_business_sites where site_public_id = ?",
        (site_public_id,),
    ) is not None:
        site_public_id = f"{make_public_id('site', site_code.lower())}-{suffix}"
        suffix += 1

    platform_execute(
        """
        insert into cloud_business_sites (
            business_account_public_id,
            site_public_id,
            site_name,
            site_code,
            status
        ) values (?, ?, ?, ?, 'active')
        """,
        (business_account_public_id, site_public_id, site_name, site_code),
    )
    return redirect(
        url_for(
            "salonmax_owner_business_detail",
            business_account_public_id=business_account_public_id,
            notice="Site added.",
        )
    )


@app.post("/platform/business/<business_account_public_id>/sites/<site_public_id>/update")
def salonmax_update_business_site(business_account_public_id: str, site_public_id: str):
    ensure_platform_sync_tables()
    site_row = platform_query_one(
        """
        select *
        from cloud_business_sites
        where business_account_public_id = ?
          and site_public_id = ?
        """,
        (business_account_public_id, site_public_id),
    )
    if site_row is None:
        return redirect(url_for("salonmax_owner_console", notice="Site not found."))

    site_name = request.form.get("site_name", "").strip()
    site_code = request.form.get("site_code", "").strip().upper()
    if not site_name:
        return redirect(
            url_for(
                "salonmax_owner_business_detail",
                business_account_public_id=business_account_public_id,
                notice="Site name is required.",
            )
        )
    if not site_code:
        site_code = secure_filename(site_name).replace("_", "").upper()[:6] or "SITE"

    platform_execute(
        """
        update cloud_business_sites
        set site_name = ?,
            site_code = ?,
            updated_at = current_timestamp
        where business_account_public_id = ?
          and site_public_id = ?
        """,
        (site_name, site_code, business_account_public_id, site_public_id),
    )
    return redirect(
        url_for(
            "salonmax_owner_business_detail",
            business_account_public_id=business_account_public_id,
            notice="Site updated.",
        )
    )


@app.post("/platform/business/<business_account_public_id>/sites/<site_public_id>/suspend")
def salonmax_suspend_business_site(business_account_public_id: str, site_public_id: str):
    ensure_platform_sync_tables()
    site_row = platform_query_one(
        """
        select *
        from cloud_business_sites
        where business_account_public_id = ?
          and site_public_id = ?
        """,
        (business_account_public_id, site_public_id),
    )
    if site_row is None:
        return redirect(url_for("salonmax_owner_console", notice="Site not found."))

    current_status = str(site_row["status"] or "active")
    next_status = "suspended" if current_status == "active" else "active"
    platform_execute(
        """
        update cloud_business_sites
        set status = ?,
            updated_at = current_timestamp
        where business_account_public_id = ?
          and site_public_id = ?
        """,
        (next_status, business_account_public_id, site_public_id),
    )
    return redirect(
        url_for(
            "salonmax_owner_business_detail",
            business_account_public_id=business_account_public_id,
            notice="Site suspended." if next_status == "suspended" else "Site reactivated.",
        )
    )


@app.post("/platform/business/<business_account_public_id>/sites/<site_public_id>/delete")
def salonmax_delete_business_site(business_account_public_id: str, site_public_id: str):
    ensure_platform_sync_tables()
    site_row = platform_query_one(
        """
        select *
        from cloud_business_sites
        where business_account_public_id = ?
          and site_public_id = ?
        """,
        (business_account_public_id, site_public_id),
    )
    if site_row is None:
        return redirect(url_for("salonmax_owner_console", notice="Site not found."))

    platform_execute(
        """
        update cloud_business_sites
        set status = 'deleted',
            updated_at = current_timestamp
        where business_account_public_id = ?
          and site_public_id = ?
        """,
        (business_account_public_id, site_public_id),
    )
    platform_execute(
        """
        update cloud_terminal_registry
        set management_status = 'retired',
            updated_at = current_timestamp
        where business_account_public_id = ?
          and site_public_id = ?
        """,
        (business_account_public_id, site_public_id),
    )
    return redirect(
        url_for(
            "salonmax_owner_business_detail",
            business_account_public_id=business_account_public_id,
            notice="Site archived and its terminals retired.",
        )
    )


@app.post("/platform/business/<business_account_public_id>/sites/<site_public_id>/restore")
def salonmax_restore_business_site(business_account_public_id: str, site_public_id: str):
    ensure_platform_sync_tables()
    site_row = platform_query_one(
        """
        select *
        from cloud_business_sites
        where business_account_public_id = ?
          and site_public_id = ?
        """,
        (business_account_public_id, site_public_id),
    )
    if site_row is None:
        return redirect(url_for("salonmax_owner_console", notice="Site not found."))

    platform_execute(
        """
        update cloud_business_sites
        set status = 'suspended',
            updated_at = current_timestamp
        where business_account_public_id = ?
          and site_public_id = ?
        """,
        (business_account_public_id, site_public_id),
    )
    return redirect(
        url_for(
            "salonmax_owner_business_detail",
            business_account_public_id=business_account_public_id,
            notice="Archived site restored as suspended. Reactivate it when ready.",
        )
    )


@app.post("/platform/business/<business_account_public_id>/terminals/create")
def salonmax_create_terminal(business_account_public_id: str):
    ensure_platform_sync_tables()
    account_row = platform_query_one(
        "select * from cloud_business_accounts where business_account_public_id = ?",
        (business_account_public_id,),
    )
    if account_row is None:
        return redirect(url_for("salonmax_owner_console", notice="Business account not found."))

    site_public_id = request.form.get("site_public_id", "").strip()
    terminal_name = request.form.get("terminal_name", "").strip()
    if not site_public_id or not terminal_name:
        return redirect(
            url_for(
                "salonmax_owner_business_detail",
                business_account_public_id=business_account_public_id,
                notice="Choose a site and terminal name.",
            )
        )
    site_row = platform_query_one(
        "select * from cloud_business_sites where business_account_public_id = ? and site_public_id = ?",
        (business_account_public_id, site_public_id),
    )
    if site_row is None:
        return redirect(
            url_for(
                "salonmax_owner_business_detail",
                business_account_public_id=business_account_public_id,
                notice="Selected site was not found.",
            )
        )

    site_code = str(site_row["site_code"]).lower()
    terminal_base = make_public_id("term", f"{site_code}-{terminal_name}")
    terminal_device_public_id = terminal_base
    suffix = 2
    while platform_query_one(
        "select id from cloud_terminal_registry where terminal_device_public_id = ?",
        (terminal_device_public_id,),
    ) is not None:
        terminal_device_public_id = f"{terminal_base}-{suffix}"
        suffix += 1

    pairing_code = make_pairing_code()
    platform_execute(
        """
        insert into cloud_terminal_registry (
            business_account_public_id,
            site_public_id,
            terminal_device_public_id,
            terminal_name,
            pairing_code,
            install_mode,
            status
        ) values (?, ?, ?, ?, ?, 'fresh_install', 'ready_to_pair')
        """,
        (
            business_account_public_id,
            site_public_id,
            terminal_device_public_id,
            terminal_name,
            pairing_code,
        ),
    )
    return redirect(
        url_for(
            "salonmax_owner_business_detail",
            business_account_public_id=business_account_public_id,
            notice="Terminal added.",
            pairing_code=pairing_code,
        )
    )


@app.post("/platform/business/<business_account_public_id>/terminals/<terminal_device_public_id>/update")
def salonmax_update_terminal(business_account_public_id: str, terminal_device_public_id: str):
    ensure_platform_sync_tables()
    terminal_row = platform_query_one(
        """
        select *
        from cloud_terminal_registry
        where business_account_public_id = ?
          and terminal_device_public_id = ?
        """,
        (business_account_public_id, terminal_device_public_id),
    )
    if terminal_row is None:
        return redirect(url_for("salonmax_owner_console", notice="Terminal not found."))

    site_public_id = request.form.get("site_public_id", "").strip()
    terminal_name = request.form.get("terminal_name", "").strip()
    if not site_public_id or not terminal_name:
        return redirect(
            url_for(
                "salonmax_owner_business_detail",
                business_account_public_id=business_account_public_id,
                notice="Choose a site and terminal name.",
            )
        )
    site_row = platform_query_one(
        """
        select *
        from cloud_business_sites
        where business_account_public_id = ?
          and site_public_id = ?
          and status = 'active'
        """,
        (business_account_public_id, site_public_id),
    )
    if site_row is None:
        return redirect(
            url_for(
                "salonmax_owner_business_detail",
                business_account_public_id=business_account_public_id,
                notice="Selected site was not found or is not active.",
            )
        )

    platform_execute(
        """
        update cloud_terminal_registry
        set site_public_id = ?,
            terminal_name = ?,
            updated_at = current_timestamp
        where business_account_public_id = ?
          and terminal_device_public_id = ?
        """,
        (site_public_id, terminal_name, business_account_public_id, terminal_device_public_id),
    )
    return redirect(
        url_for(
            "salonmax_owner_business_detail",
            business_account_public_id=business_account_public_id,
            notice="Terminal updated.",
        )
    )


@app.post("/platform/business/<business_account_public_id>/terminals/<terminal_device_public_id>/suspend")
def salonmax_suspend_terminal(business_account_public_id: str, terminal_device_public_id: str):
    ensure_platform_sync_tables()
    terminal_row = platform_query_one(
        """
        select *
        from cloud_terminal_registry
        where business_account_public_id = ?
          and terminal_device_public_id = ?
        """,
        (business_account_public_id, terminal_device_public_id),
    )
    if terminal_row is None:
        return redirect(url_for("salonmax_owner_console", notice="Terminal not found."))

    current_status = str(terminal_row["management_status"] or "active")
    next_status = "suspended" if current_status == "active" else "active"
    platform_execute(
        """
        update cloud_terminal_registry
        set management_status = ?,
            updated_at = current_timestamp
        where business_account_public_id = ?
          and terminal_device_public_id = ?
        """,
        (next_status, business_account_public_id, terminal_device_public_id),
    )
    return redirect(
        url_for(
            "salonmax_owner_business_detail",
            business_account_public_id=business_account_public_id,
            notice="Terminal suspended." if next_status == "suspended" else "Terminal reactivated.",
        )
    )


@app.post("/platform/business/<business_account_public_id>/terminals/<terminal_device_public_id>/retire")
def salonmax_retire_terminal(business_account_public_id: str, terminal_device_public_id: str):
    ensure_platform_sync_tables()
    terminal_row = platform_query_one(
        """
        select *
        from cloud_terminal_registry
        where business_account_public_id = ?
          and terminal_device_public_id = ?
        """,
        (business_account_public_id, terminal_device_public_id),
    )
    if terminal_row is None:
        return redirect(url_for("salonmax_owner_console", notice="Terminal not found."))

    platform_execute(
        """
        update cloud_terminal_registry
        set management_status = 'retired',
            updated_at = current_timestamp
        where business_account_public_id = ?
          and terminal_device_public_id = ?
        """,
        (business_account_public_id, terminal_device_public_id),
    )
    return redirect(
        url_for(
            "salonmax_owner_business_detail",
            business_account_public_id=business_account_public_id,
            notice="Terminal retired.",
        )
    )


@app.post("/platform/terminal/<terminal_device_public_id>/replacement-code")
def salonmax_prepare_replacement_terminal(terminal_device_public_id: str):
    ensure_platform_sync_tables()
    terminal_row = platform_query_one(
        "select * from cloud_terminal_registry where terminal_device_public_id = ?",
        (terminal_device_public_id,),
    )
    if terminal_row is None:
        return redirect(url_for("salonmax_owner_console", notice="Terminal not found."))
    if str(terminal_row["management_status"] or "active") == "retired":
        return redirect(
            url_for(
                "salonmax_owner_business_detail",
                business_account_public_id=str(terminal_row["business_account_public_id"]),
                notice="Retired terminals cannot be re-paired.",
            )
        )

    pairing_code = make_pairing_code()
    platform_execute(
        """
        update cloud_terminal_registry
        set pairing_code = ?,
            install_mode = 'replacement_pi',
            status = 'ready_to_pair',
            updated_at = current_timestamp
        where terminal_device_public_id = ?
        """,
        (pairing_code, terminal_device_public_id),
    )
    return redirect(
        url_for(
            "salonmax_owner_business_detail",
            business_account_public_id=str(terminal_row["business_account_public_id"]),
            notice="Replacement Pi pairing pack generated.",
            pairing_code=pairing_code,
        )
    )


@app.post("/platform/terminal/<terminal_device_public_id>/fresh-install-code")
def salonmax_prepare_fresh_install_terminal(terminal_device_public_id: str):
    ensure_platform_sync_tables()
    terminal_row = platform_query_one(
        "select * from cloud_terminal_registry where terminal_device_public_id = ?",
        (terminal_device_public_id,),
    )
    if terminal_row is None:
        return redirect(url_for("salonmax_owner_console", notice="Terminal not found."))
    if str(terminal_row["management_status"] or "active") == "retired":
        return redirect(
            url_for(
                "salonmax_owner_business_detail",
                business_account_public_id=str(terminal_row["business_account_public_id"]),
                notice="Retired terminals cannot be re-paired.",
            )
        )

    pairing_code = make_pairing_code()
    platform_execute(
        """
        update cloud_terminal_registry
        set pairing_code = ?,
            install_mode = 'fresh_install',
            status = 'ready_to_pair',
            updated_at = current_timestamp
        where terminal_device_public_id = ?
        """,
        (pairing_code, terminal_device_public_id),
    )
    return redirect(
        url_for(
            "salonmax_owner_business_detail",
            business_account_public_id=str(terminal_row["business_account_public_id"]),
            notice="Fresh install pairing pack generated.",
            pairing_code=pairing_code,
        )
    )


@app.post("/platform/updates/set-target")
def salonmax_set_global_target_version():
    ensure_platform_sync_tables()
    target_version = request.form.get("desired_app_version", "").strip()
    business_account_public_id = request.form.get("business_account_public_id", "").strip()
    update_channel = request.form.get("app_update_channel", "stable").strip() or "stable"
    if not target_version:
        return provider_notice_redirect("salonmax_owner_updates", notice="Target version is required.")
    sql = """
        update cloud_terminal_registry
        set desired_app_version = ?,
            app_update_channel = ?,
            updated_at = current_timestamp
        where management_status != 'retired'
    """
    params = [target_version, update_channel]
    if business_account_public_id:
        sql += " and business_account_public_id = ?"
        params.append(business_account_public_id)
    platform_execute(sql, tuple(params))
    return provider_notice_redirect(
        "salonmax_owner_updates",
        business_account_public_id=business_account_public_id,
        notice=f"Target version set to {target_version} on {update_channel}.",
    )


@app.post("/platform/updates/terminal/<terminal_device_public_id>/set-target")
def salonmax_set_terminal_target_version(terminal_device_public_id: str):
    ensure_platform_sync_tables()
    terminal_row = platform_query_one(
        "select * from cloud_terminal_registry where terminal_device_public_id = ?",
        (terminal_device_public_id,),
    )
    if terminal_row is None:
        return provider_notice_redirect("salonmax_owner_updates", notice="Terminal not found.")
    target_version = request.form.get("desired_app_version", "").strip()
    update_channel = request.form.get("app_update_channel", "stable").strip() or "stable"
    business_account_public_id = str(terminal_row["business_account_public_id"] or "")
    if not target_version:
        return provider_notice_redirect(
            "salonmax_owner_updates",
            business_account_public_id=business_account_public_id,
            notice="Target version is required.",
        )
    platform_execute(
        """
        update cloud_terminal_registry
        set desired_app_version = ?,
            app_update_channel = ?,
            updated_at = current_timestamp
        where terminal_device_public_id = ?
        """,
        (target_version, update_channel, terminal_device_public_id),
    )
    return provider_notice_redirect(
        "salonmax_owner_updates",
        business_account_public_id=business_account_public_id,
        notice=f"Target version updated for {terminal_device_public_id} on {update_channel}.",
    )


@app.post("/platform/updates/terminal/<terminal_device_public_id>/use-reported")
def salonmax_use_reported_terminal_version(terminal_device_public_id: str):
    ensure_platform_sync_tables()
    terminal_row = platform_query_one(
        "select * from cloud_terminal_registry where terminal_device_public_id = ?",
        (terminal_device_public_id,),
    )
    if terminal_row is None:
        return provider_notice_redirect("salonmax_owner_updates", notice="Terminal not found.")
    reported_version = str(terminal_row["app_version_reported"] or "").strip()
    business_account_public_id = str(terminal_row["business_account_public_id"] or "")
    if not reported_version:
        return provider_notice_redirect(
            "salonmax_owner_updates",
            business_account_public_id=business_account_public_id,
            notice="This terminal has not reported an app version yet.",
        )
    platform_execute(
        """
        update cloud_terminal_registry
        set desired_app_version = ?,
            updated_at = current_timestamp
        where terminal_device_public_id = ?
        """,
        (reported_version, terminal_device_public_id),
    )
    return provider_notice_redirect(
        "salonmax_owner_updates",
        business_account_public_id=business_account_public_id,
        notice=f"Target version matched to reported version for {terminal_device_public_id}.",
    )


@app.post("/platform/updates/terminal/<terminal_device_public_id>/clear-target")
def salonmax_clear_terminal_target_version(terminal_device_public_id: str):
    ensure_platform_sync_tables()
    terminal_row = platform_query_one(
        "select * from cloud_terminal_registry where terminal_device_public_id = ?",
        (terminal_device_public_id,),
    )
    if terminal_row is None:
        return provider_notice_redirect("salonmax_owner_updates", notice="Terminal not found.")
    business_account_public_id = str(terminal_row["business_account_public_id"] or "")
    platform_execute(
        """
        update cloud_terminal_registry
        set desired_app_version = '',
            updated_at = current_timestamp
        where terminal_device_public_id = ?
        """,
        (terminal_device_public_id,),
    )
    return provider_notice_redirect(
        "salonmax_owner_updates",
        business_account_public_id=business_account_public_id,
        notice=f"Target version cleared for {terminal_device_public_id}.",
    )


@app.post("/platform/licences/<terminal_device_public_id>/extend")
def salonmax_extend_terminal_licence(terminal_device_public_id: str):
    ensure_platform_sync_tables()
    licence_row = platform_query_one(
        "select * from cloud_device_licences where terminal_device_public_id = ?",
        (terminal_device_public_id,),
    )
    if licence_row is None:
        return provider_notice_redirect("salonmax_owner_licences", notice="Licence not found.")
    terminal_row = platform_query_one(
        "select * from cloud_terminal_registry where terminal_device_public_id = ?",
        (terminal_device_public_id,),
    )
    business_row = None
    site_row = None
    if terminal_row is not None:
        business_row = platform_query_one(
            "select * from cloud_business_accounts where business_account_public_id = ?",
            (str(terminal_row["business_account_public_id"] or "").strip(),),
        )
        site_row = platform_query_one(
            "select * from cloud_business_sites where site_public_id = ?",
            (str(terminal_row["site_public_id"] or "").strip(),),
        )
    _, next_status, _ = terminal_access_state(business_row, site_row, terminal_row)
    new_expiry = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    platform_execute(
        """
        update cloud_device_licences
        set licence_status = ?,
            expires_at = ?
        where terminal_device_public_id = ?
        """,
        (next_status, new_expiry, terminal_device_public_id),
    )
    return provider_notice_redirect(
        "salonmax_owner_licences",
        business_account_public_id=request.form.get("business_account_public_id", "").strip(),
        health_filter=request.form.get("health_filter", "").strip(),
        notice=f"Licence extended for {terminal_device_public_id}.",
    )


@app.route("/business-settings")
def business_settings():
    return render_template(
        "business_settings.html",
        settings=business_settings_row(),
        notice=request.args.get("notice", "").strip(),
    )


@app.post("/business-settings/update")
def update_business_settings():
    business_name = request.form.get("business_name", "").strip() or "Your Salon"
    currency_symbol = request.form.get("currency_symbol", "").strip() or "GBP"

    try:
        default_package_validity_days = int(request.form.get("default_package_validity_days", "365").strip())
        default_prep_minutes = int(request.form.get("default_prep_minutes", "3").strip())
        default_cooldown_minutes = int(request.form.get("default_cooldown_minutes", "3").strip())
        peak_price_per_minute = float(request.form.get("peak_price_per_minute", "0.65").strip())
        happy_hour_price_per_minute = float(request.form.get("happy_hour_price_per_minute", "0.55").strip())
    except ValueError:
        return redirect(url_for("business_settings"))

    happy_hour_1_start = request.form.get("happy_hour_1_start", "10:00").strip() or "10:00"
    happy_hour_1_end = request.form.get("happy_hour_1_end", "11:00").strip() or "11:00"
    happy_hour_2_start = request.form.get("happy_hour_2_start", "20:00").strip() or "20:00"
    happy_hour_2_end = request.form.get("happy_hour_2_end", "21:00").strip() or "21:00"
    management_report_emails = request.form.get("management_report_emails", "").strip()
    report_from_email = request.form.get("report_from_email", "").strip()
    smtp_host = request.form.get("smtp_host", "").strip()
    smtp_username = request.form.get("smtp_username", "").strip()
    smtp_password = request.form.get("smtp_password", "").strip()
    email_reports_enabled = parse_checkbox("email_reports_enabled")
    smtp_use_tls = parse_checkbox("smtp_use_tls")
    auto_email_shift_reports = parse_checkbox("auto_email_shift_reports")
    auto_email_daily_reports = parse_checkbox("auto_email_daily_reports")

    try:
        smtp_port = int(request.form.get("smtp_port", "587").strip() or "587")
    except ValueError:
        smtp_port = 587

    ensure_business_settings_table()
    execute(
        """
        update business_settings
        set business_name = ?,
            currency_symbol = ?,
            default_package_validity_days = ?,
            default_prep_minutes = ?,
            default_cooldown_minutes = ?,
            peak_price_per_minute = ?,
            happy_hour_price_per_minute = ?,
            happy_hour_1_start = ?,
            happy_hour_1_end = ?,
            happy_hour_2_start = ?,
            happy_hour_2_end = ?,
            management_report_emails = ?,
            report_from_email = ?,
            smtp_host = ?,
            smtp_port = ?,
            smtp_username = ?,
            smtp_password = ?,
            smtp_use_tls = ?,
            email_reports_enabled = ?,
            auto_email_shift_reports = ?,
            auto_email_daily_reports = ?
        where id = 1
        """,
        (
            business_name,
            currency_symbol,
            default_package_validity_days,
            default_prep_minutes,
            default_cooldown_minutes,
            peak_price_per_minute,
            happy_hour_price_per_minute,
            happy_hour_1_start,
            happy_hour_1_end,
            happy_hour_2_start,
            happy_hour_2_end,
            management_report_emails,
            report_from_email,
            smtp_host,
            smtp_port,
            smtp_username,
            smtp_password,
            smtp_use_tls,
            email_reports_enabled,
            auto_email_shift_reports,
            auto_email_daily_reports,
        ),
    )
    return redirect(url_for("business_settings", notice="Business settings saved"))


@app.route("/sunbed-settings")
def sunbed_settings():
    catalogue_rows, catalogue_by_manufacturer = sunbed_catalogue_rows()
    return render_template(
        "sunbed_settings.html",
        sunbeds=sunbed_rows(),
        sunbed_catalogue_rows=catalogue_rows,
        sunbed_catalogue_by_manufacturer=catalogue_by_manufacturer,
    )


@app.post("/sunbed-settings/<int:sunbed_id>/update")
def update_sunbed_settings(sunbed_id):
    bed_row = query_one("select * from sunbeds where id = ?", (sunbed_id,))
    if bed_row is None:
        return redirect(url_for("sunbed_settings"))

    catalogue_rows, _catalogue_by_manufacturer = sunbed_catalogue_rows()
    valid_catalogue = {
        (str(row["manufacturer"]).strip(), str(row["model"]).strip()): row
        for row in catalogue_rows
    }
    bed_number = int(bed_row["room_number"] or 1)
    custom_name = request.form.get("custom_name", "").strip()
    manufacturer = request.form.get("manufacturer", "").strip()
    model = request.form.get("model", "").strip()
    room_name = default_sunbed_label(bed_number)
    bed_name = custom_name or room_name
    display_name = custom_name or room_name
    bed_type = bed_row["bed_type"] or "lay_down"
    customer_display_image_path = request.form.get("existing_customer_display_image_path", "").strip()
    default_catalogue_image_file = str(bed_row["default_catalogue_image_file"] or "").strip()
    is_active = int(bed_row["is_active"] or 1)

    selected_catalogue_row = valid_catalogue.get((manufacturer, model))
    if selected_catalogue_row is None:
        manufacturer = ""
        model = ""
        default_catalogue_image_file = ""
    else:
        default_catalogue_image_file = str(selected_catalogue_row["default_image_file"] or "").strip()
        if not customer_display_image_path:
            catalogue_image_path = default_catalogue_image_path(default_catalogue_image_file)
            if catalogue_image_path:
                customer_display_image_path = catalogue_image_path

    image_file = request.files.get("customer_display_image_file")
    if image_file and image_file.filename:
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        filename = secure_filename(image_file.filename)
        timestamped = f"sunbed_{sunbed_id}_{int(datetime.now().timestamp())}_{filename}"
        destination = UPLOADS_DIR / timestamped
        image_file.save(destination)
        customer_display_image_path = f"/static/uploads/{timestamped}"

    db = get_db()
    db.execute(
        """
        update sunbeds
        set room_name = ?,
            bed_name = ?,
            display_name = ?,
            manufacturer = ?,
            model = ?,
            default_catalogue_image_file = ?,
            bed_type = ?,
            customer_display_image_path = ?,
            is_active = ?,
            updated_at = current_timestamp
        where room_number = ?
        """,
        (
            room_name,
            bed_name,
            display_name,
            manufacturer,
            model,
            default_catalogue_image_file,
            bed_type,
            customer_display_image_path,
            is_active,
            bed_number,
        ),
    )
    db.commit()
    return redirect(url_for("sunbed_settings"))


@app.post("/sunbed-settings/<int:sunbed_id>/reset-retube")
def reset_sunbed_retube(sunbed_id):
    execute(
        """
        update sunbeds
        set retube_minutes_used = 0,
            last_retube_reset_at = current_timestamp,
            updated_at = current_timestamp
        where id = ?
        """,
        (sunbed_id,),
    )
    return redirect(url_for("sunbed_settings"))


@app.route("/customers")
def customers():
    search = request.args.get("q", "").strip()

    if search:
        like = f"%{search}%"
        rows = query_all(
            """
            select *
            from customers
            where customer_number like ?
               or account_number like ?
               or first_name like ?
               or last_name like ?
            order by last_name, first_name
            """,
            (like, like, like, like),
        )
    else:
        rows = query_all("select * from customers order by last_name, first_name limit 50")

    return render_template(
        "customers.html",
        customers=rows,
        search=search,
        notice=request.args.get("notice", "").strip(),
    )


@app.post("/customers/create")
def create_customer():
    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    account_number = request.form.get("account_number", "").strip()
    phone = request.form.get("phone", "").strip()
    email = request.form.get("email", "").strip()

    if not first_name or not last_name:
        return redirect(url_for("customers"))

    new_customer_number = next_customer_number()

    execute(
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
        ) values (?, ?, ?, ?, ?, ?, 0, 0, 1)
        """,
        (new_customer_number, account_number, first_name, last_name, phone, email),
    )

    customer = query_one(
        "select id from customers where customer_number = ?",
        (new_customer_number,),
    )
    return redirect(url_for("customer_detail", customer_id=customer["id"]))


@app.post("/customers/import-ivy")
def import_ivy_customers():
    raw_text = request.form.get("ivy_export_text", "")
    if not raw_text.strip():
        return redirect(url_for("customers", notice="No Ivy data was pasted."))

    inserted, updated, skipped = import_ivy_customer_rows(raw_text)
    notice = f"Ivy import complete. Inserted: {inserted}, updated: {updated}, skipped: {skipped}."
    return redirect(url_for("customers", notice=notice))


def customer_transactions(customer_id):
    return query_all(
        """
        select transaction_number, transaction_type, total_amount, payment_method, created_at, notes
        from transactions
        where customer_id = ?
        order by id desc
        limit 15
        """,
        (customer_id,),
    )


@app.route("/customers/<int:customer_id>")
def customer_detail(customer_id):
    customer = query_one("select * from customers where id = ?", (customer_id,))
    if customer is None:
        return redirect(url_for("customers"))

    return render_template(
        "customer_detail.html",
        customer=customer,
        transactions=customer_transactions(customer_id),
        package_products=query_all(
            "select * from package_products where is_active = 1 order by minutes_included"
        ),
        sites=query_all("select * from sites order by name"),
        staff_rows=query_all("select * from staff_users where is_active = 1 order by name"),
        terminals=query_all("select * from terminals where is_active = 1 order by name"),
    )


@app.post("/customers/<int:customer_id>/topup-minutes")
def customer_topup_minutes(customer_id):
    minutes_text = request.form.get("minutes", "0").strip()
    customer = query_one("select * from customers where id = ?", (customer_id,))
    if customer is None:
        return redirect(url_for("customers"))

    try:
        minutes = int(minutes_text)
    except ValueError:
        return redirect(url_for("customer_detail", customer_id=customer_id))

    if minutes <= 0:
        return redirect(url_for("customer_detail", customer_id=customer_id))

    execute(
        "update customers set package_minutes = package_minutes + ? where id = ?",
        (minutes, customer_id),
    )

    transaction_id = execute(
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
        ) values (?, ?, null, null, null, ?, 0, 'management', 'completed', ?)
        """,
        (
            next_transaction_number(),
            customer_id,
            "minutes_topup",
            f"Back office minutes top-up: {minutes} mins",
        ),
    )

    execute(
        """
        insert into transaction_lines (
            transaction_id,
            line_type,
            description,
            quantity,
            unit_price,
            line_total,
            minutes
        ) values (?, ?, ?, 1, 0, 0, ?)
        """,
        (transaction_id, "minutes_topup", "Back Office Minutes Top-Up", minutes),
    )

    return redirect(url_for("customer_detail", customer_id=customer_id))


@app.route("/staff")
def staff():
    return render_template("staff.html", staff_rows=query_all("select * from staff_users order by name"))


@app.post("/staff/create")
def create_staff():
    name = request.form.get("name", "").strip()
    pin_code = request.form.get("pin_code", "").strip()
    role = request.form.get("role", "staff").strip() or "staff"

    if not name or not pin_code:
        return redirect(url_for("staff"))

    execute(
        "insert into staff_users (name, pin_code, role, is_active) values (?, ?, ?, 1)",
        (name, pin_code, role),
    )
    return redirect(url_for("staff"))


@app.route("/pricing")
def pricing():
    rules = query_all(
        """
        select pricing_rules.id, sites.name as site_name, devices.device_name, pricing_rules.price_per_minute
        from pricing_rules
        left join sites on sites.id = pricing_rules.site_id
        left join devices on devices.id = pricing_rules.device_id
        where pricing_rules.is_active = 1
        order by sites.name, devices.device_name
        """
    )
    return render_template("pricing.html", rules=rules)


@app.post("/pricing/update")
def update_pricing():
    rule_id = request.form.get("rule_id", "").strip()
    price = request.form.get("price_per_minute", "").strip()

    try:
        price_value = float(price)
    except ValueError:
        return redirect(url_for("pricing"))

    execute(
        "update pricing_rules set price_per_minute = ? where id = ?",
        (price_value, rule_id),
    )
    return redirect(url_for("pricing"))


@app.route("/packages")
def packages():
    rows = query_all(
        """
        select *
        from package_products
        order by minutes_included, name
        """
    )
    return render_template("packages.html", package_rows=rows)


@app.post("/packages/create")
def create_package():
    name = request.form.get("name", "").strip()
    code = request.form.get("code", "").strip()
    minutes_text = request.form.get("minutes_included", "").strip()
    price_text = request.form.get("price", "").strip()
    validity_days_text = request.form.get("validity_days", "").strip()

    try:
        minutes_included = int(minutes_text)
        price = float(price_text)
        validity_days = int(validity_days_text)
    except ValueError:
        return redirect(url_for("packages"))

    if not name or not code:
        return redirect(url_for("packages"))

    execute(
        """
        insert into package_products (
            name,
            code,
            minutes_included,
            price,
            validity_days,
            is_active
        ) values (?, ?, ?, ?, ?, 1)
        """,
        (name, code, minutes_included, price, validity_days),
    )
    return redirect(url_for("packages"))


@app.route("/store")
def store():
    search = request.args.get("q", "").strip()
    return render_template(
        "store.html",
        group_rows=product_group_rows(),
        product_rows=retail_product_rows_filtered(search),
        stock_adjustment_rows=stock_adjustment_rows(),
        search=search,
    )


@app.post("/store/groups/create")
def create_product_group():
    name = request.form.get("name", "").strip()
    sort_order_text = request.form.get("sort_order", "0").strip()

    try:
        sort_order = int(sort_order_text or "0")
    except ValueError:
        return redirect(url_for("store"))

    if not name:
        return redirect(url_for("store"))

    execute(
        "insert into product_groups (name, sort_order, is_active) values (?, ?, 1)",
        (name, sort_order),
    )
    return redirect(url_for("store"))


@app.post("/store/products/create")
def create_retail_product():
    group_id = request.form.get("group_id", "").strip() or None
    name = request.form.get("name", "").strip()
    sku = request.form.get("sku", "").strip()
    size_label = request.form.get("size_label", "").strip()
    unit_label = request.form.get("unit_label", "").strip()
    price_text = request.form.get("price", "").strip()
    stock_quantity_text = request.form.get("stock_quantity", "0").strip()
    commission_rate_text = request.form.get("commission_rate", "0").strip()

    try:
        price = float(price_text)
        stock_quantity = int(stock_quantity_text or "0")
        commission_rate = float(commission_rate_text or "0")
    except ValueError:
        return redirect(url_for("store"))

    if not name or not sku:
        return redirect(url_for("store"))

    execute(
        """
        insert into retail_products (
            group_id,
            name,
            sku,
            size_label,
            unit_label,
            price,
            stock_quantity,
            commission_rate,
            is_active
        ) values (?, ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (group_id, name, sku, size_label, unit_label, price, stock_quantity, commission_rate),
    )
    return redirect(url_for("store"))


@app.post("/store/products/<int:product_id>/update")
def update_retail_product(product_id):
    group_id = request.form.get("group_id", "").strip() or None
    name = request.form.get("name", "").strip()
    sku = request.form.get("sku", "").strip()
    size_label = request.form.get("size_label", "").strip()
    unit_label = request.form.get("unit_label", "").strip()
    price_text = request.form.get("price", "").strip()
    stock_quantity_text = request.form.get("stock_quantity", "0").strip()
    commission_rate_text = request.form.get("commission_rate", "0").strip()

    try:
        price = float(price_text)
        stock_quantity = int(stock_quantity_text or "0")
        commission_rate = float(commission_rate_text or "0")
    except ValueError:
        return redirect(url_for("store"))

    if not name or not sku:
        return redirect(url_for("store"))

    execute(
        """
        update retail_products
        set group_id = ?,
            name = ?,
            sku = ?,
            size_label = ?,
            unit_label = ?,
            price = ?,
            stock_quantity = ?,
            commission_rate = ?
        where id = ?
        """,
        (
            group_id,
            name,
            sku,
            size_label,
            unit_label,
            price,
            stock_quantity,
            commission_rate,
            product_id,
        ),
    )
    return redirect(url_for("store"))


@app.post("/store/products/<int:product_id>/adjust-stock")
def adjust_retail_product_stock(product_id):
    quantity_text = request.form.get("change_quantity", "0").strip()
    reason = request.form.get("reason", "").strip() or "Manual stock adjustment"
    product = query_one("select * from retail_products where id = ?", (product_id,))

    if product is None:
        return redirect(url_for("store"))

    try:
        change_quantity = int(quantity_text)
    except ValueError:
        return redirect(url_for("store"))

    if change_quantity == 0:
        return redirect(url_for("store"))

    new_quantity = int(product["stock_quantity"]) + change_quantity
    if new_quantity < 0:
        new_quantity = 0
        change_quantity = -int(product["stock_quantity"])

    execute(
        "update retail_products set stock_quantity = ? where id = ?",
        (new_quantity, product_id),
    )
    execute(
        "insert into stock_adjustments (product_id, change_quantity, reason) values (?, ?, ?)",
        (product_id, change_quantity, reason),
    )
    return redirect(url_for("store"))


@app.route("/transactions")
def transactions():
    rows = query_all(
        """
        select
            transactions.*,
            customers.first_name,
            customers.last_name,
            sites.name as site_name,
            staff_users.name as staff_name
        from transactions
        left join customers on customers.id = transactions.customer_id
        left join sites on sites.id = transactions.site_id
        left join staff_users on staff_users.id = transactions.staff_user_id
        order by transactions.id desc
        limit 100
        """
    )
    return render_template("transactions.html", transaction_rows=rows)


@app.route("/till-sessions")
def till_sessions():
    rows = query_all(
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
        order by till_sessions.id desc
        """
    )
    return render_template(
        "till_sessions.html",
        till_sessions=rows,
        sites=query_all("select * from sites where is_active = 1 order by name"),
        terminals=query_all("select * from terminals where is_active = 1 order by name"),
        staff_rows=query_all("select * from staff_users where is_active = 1 order by name"),
        notice=request.args.get("notice", "").strip(),
    )


@app.post("/till-sessions/open")
def open_till_session():
    site_id = request.form.get("site_id", "").strip()
    terminal_id = request.form.get("terminal_id", "").strip()
    opened_by_user_id = request.form.get("opened_by_user_id", "").strip()
    opening_float_text = request.form.get("opening_float", "0").strip()

    try:
        opening_float = float(opening_float_text)
    except ValueError:
        return redirect(url_for("till_sessions"))

    execute(
        """
        insert into till_sessions (
            site_id,
            terminal_id,
            opened_by_user_id,
            opening_float,
            expected_cash,
            counted_cash,
            variance,
            status,
            closing_notes
        ) values (?, ?, ?, ?, ?, 0, 0, 'open', '')
        """,
        (site_id, terminal_id, opened_by_user_id, opening_float, opening_float),
    )
    return redirect(url_for("till_sessions"))


@app.post("/till-sessions/<int:session_id>/close")
def close_till_session(session_id):
    counted_cash_text = request.form.get("counted_cash", "0").strip()
    closed_by_user_id = request.form.get("closed_by_user_id", "").strip()
    closing_notes = request.form.get("closing_notes", "").strip()

    session = query_one("select * from till_sessions where id = ?", (session_id,))
    if session is None or session["status"] != "open":
        return redirect(url_for("till_sessions"))

    try:
        counted_cash = float(counted_cash_text)
    except ValueError:
        return redirect(url_for("till_sessions"))

    variance = counted_cash - float(session["expected_cash"])
    closed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    execute(
        """
        update till_sessions
        set counted_cash = ?,
            variance = ?,
            closed_by_user_id = ?,
            closed_at = ?,
            status = 'closed',
            closing_notes = ?
        where id = ?
        """,
        (counted_cash, variance, closed_by_user_id, closed_at, closing_notes, session_id),
    )
    settings = email_report_settings()
    notice = "Till closed"
    if int(settings.get("email_reports_enabled") or 0) and int(settings.get("auto_email_shift_reports") or 0):
        sent, message = send_shift_summary_email_for_session(session_id)
        notice = message if sent else f"Till closed. Shift email not sent: {message}"
    return redirect(url_for("till_sessions", notice=notice))


@app.post("/reports/email-selected-day")
def email_selected_day_report():
    selected_day = parse_date_or_default(request.form.get("selected_day", ""), datetime.now().date())
    sent, message = send_daily_summary_email_for_date(selected_day)
    notice = message if sent else f"Daily email not sent: {message}"
    return redirect(
        url_for(
            "reports",
            selected_day=selected_day.isoformat(),
            compare_a=request.form.get("compare_a", "").strip(),
            compare_b=request.form.get("compare_b", "").strip(),
            notice=notice,
        )
    )


@app.route("/reports")
def reports():
    today = datetime.now().date()
    selected_day = parse_date_or_default(request.args.get("selected_day", ""), today)
    compare_a = parse_date_or_default(request.args.get("compare_a", ""), today)
    compare_b = parse_date_or_default(request.args.get("compare_b", ""), today - timedelta(days=7))
    week_start = today - timedelta(days=today.weekday())
    today_start = f"{today.isoformat()} 00:00:00"
    today_end = f"{today.isoformat()} 23:59:59"
    week_start_text = f"{week_start.isoformat()} 00:00:00"
    week_end_text = f"{today.isoformat()} 23:59:59"
    selected_day_start = f"{selected_day.isoformat()} 00:00:00"
    selected_day_end = f"{selected_day.isoformat()} 23:59:59"
    compare_a_start = f"{compare_a.isoformat()} 00:00:00"
    compare_a_end = f"{compare_a.isoformat()} 23:59:59"
    compare_b_start = f"{compare_b.isoformat()} 00:00:00"
    compare_b_end = f"{compare_b.isoformat()} 23:59:59"

    site_rows = query_all(
        """
        select
            sites.name,
            count(transactions.id) as transaction_count,
            coalesce(sum(transactions.total_amount), 0) as sales_total,
            coalesce(sum(case when transactions.transaction_type = 'account_topup' then transactions.total_amount else 0 end), 0) as topup_total,
            coalesce(sum(case when transactions.transaction_type = 'package_sale' then transactions.total_amount else 0 end), 0) as package_sales_total,
            coalesce(sum(case when transactions.transaction_type = 'retail_sale' then transactions.total_amount else 0 end), 0) as retail_sales_total
        from sites
        left join transactions on transactions.site_id = sites.id and transactions.status = 'completed'
        group by sites.id
        order by sites.name
        """
    )
    return render_template(
        "reports.html",
        site_rows=site_rows,
        today_totals=totals_between(today_start, today_end),
        week_totals=totals_between(week_start_text, week_end_text),
        best_sellers_today=best_seller_rows(today_start, today_end),
        best_sellers_week=best_seller_rows(week_start_text, week_end_text),
        selected_day=selected_day.isoformat(),
        selected_day_totals=totals_between(selected_day_start, selected_day_end),
        selected_day_transactions=transactions_for_day(selected_day),
        compare_a=compare_a.isoformat(),
        compare_b=compare_b.isoformat(),
        compare_a_totals=totals_between(compare_a_start, compare_a_end),
        compare_b_totals=totals_between(compare_b_start, compare_b_end),
        notice=request.args.get("notice", "").strip(),
    )


if __name__ == "__main__":
    run_app()
