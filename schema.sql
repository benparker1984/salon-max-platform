create table if not exists sites (
    id integer primary key autoincrement,
    name text not null,
    code text not null,
    is_active integer not null default 1
);

create table if not exists devices (
    id integer primary key autoincrement,
    site_id integer not null,
    device_number integer not null,
    device_name text not null,
    is_active integer not null default 1,
    foreign key (site_id) references sites(id)
);

create table if not exists terminals (
    id integer primary key autoincrement,
    site_id integer not null,
    name text not null,
    is_active integer not null default 1,
    foreign key (site_id) references sites(id)
);

create table if not exists staff_users (
    id integer primary key autoincrement,
    name text not null,
    pin_code text not null,
    role text not null,
    is_active integer not null default 1
);

create table if not exists customers (
    id integer primary key autoincrement,
    customer_number text not null unique,
    account_number text,
    first_name text not null,
    last_name text not null,
    phone text,
    email text,
    account_balance real not null default 0,
    package_minutes integer not null default 0,
    is_active integer not null default 1
);

create table if not exists pricing_rules (
    id integer primary key autoincrement,
    site_id integer,
    device_id integer,
    price_per_minute real not null,
    is_active integer not null default 1,
    foreign key (site_id) references sites(id),
    foreign key (device_id) references devices(id)
);

create table if not exists package_products (
    id integer primary key autoincrement,
    name text not null,
    code text not null unique,
    minutes_included integer not null,
    price real not null,
    validity_days integer not null,
    is_active integer not null default 1
);

create table if not exists product_groups (
    id integer primary key autoincrement,
    name text not null unique,
    sort_order integer not null default 0,
    is_active integer not null default 1
);

create table if not exists retail_products (
    id integer primary key autoincrement,
    group_id integer,
    name text not null,
    sku text not null unique,
    size_label text not null default '',
    unit_label text not null default '',
    price real not null,
    stock_quantity integer not null default 0,
    commission_rate real not null default 0,
    is_active integer not null default 1,
    foreign key (group_id) references product_groups(id)
);

create table if not exists stock_adjustments (
    id integer primary key autoincrement,
    product_id integer not null,
    change_quantity integer not null,
    reason text not null default '',
    created_at text not null default current_timestamp,
    foreign key (product_id) references retail_products(id)
);

create table if not exists transactions (
    id integer primary key autoincrement,
    transaction_number text not null unique,
    customer_id integer,
    site_id integer,
    terminal_id integer,
    staff_user_id integer,
    transaction_type text not null,
    total_amount real not null default 0,
    payment_method text not null,
    status text not null default 'completed',
    notes text not null default '',
    created_at text not null default current_timestamp,
    foreign key (customer_id) references customers(id),
    foreign key (site_id) references sites(id),
    foreign key (terminal_id) references terminals(id),
    foreign key (staff_user_id) references staff_users(id)
);

create table if not exists transaction_lines (
    id integer primary key autoincrement,
    transaction_id integer not null,
    line_type text not null,
    description text not null,
    quantity integer not null default 1,
    unit_price real not null,
    line_total real not null,
    minutes integer not null default 0,
    foreign key (transaction_id) references transactions(id)
);

create table if not exists till_sessions (
    id integer primary key autoincrement,
    site_id integer not null,
    terminal_id integer not null,
    opened_by_user_id integer not null,
    closed_by_user_id integer,
    opened_at text not null default current_timestamp,
    closed_at text,
    opening_float real not null default 0,
    expected_cash real not null default 0,
    counted_cash real not null default 0,
    variance real not null default 0,
    status text not null default 'open',
    closing_notes text not null default '',
    foreign key (site_id) references sites(id),
    foreign key (terminal_id) references terminals(id),
    foreign key (opened_by_user_id) references staff_users(id),
    foreign key (closed_by_user_id) references staff_users(id)
);

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
