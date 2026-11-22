"""Stable business-data fingerprint; excludes periodically changing sync cursors."""
import hashlib
import json

TABLES = ('customers', 'transactions', 'transaction_lines', 'package_products',
          'retail_products', 'staff_users', 'sunbeds', 'pricing_rules',
          'business_settings', 'sites', 'terminals', 'devices', 'till_sessions')


def business_fingerprint(db):
    data = {}
    for table in TABLES:
        if not db.execute('select 1 from sqlite_master where type=? and name=?', ('table', table)).fetchone():
            continue
        cursor = db.execute('select * from "' + table + '"')
        rows = [list(row) for row in cursor.fetchall()]
        data[table] = {'columns': [col[0] for col in cursor.description],
                       'rows': sorted(rows, key=lambda row: json.dumps(row, default=lambda v:v.hex()))}
    encoded = json.dumps(data, sort_keys=True, default=lambda v:v.hex()).encode()
    return hashlib.sha256(encoded).hexdigest()
