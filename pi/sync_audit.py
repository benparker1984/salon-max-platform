"""Read-only comparison of a paired till with its authenticated cloud feeds.

Run on the Pi. Outputs account numbers and mismatched field names, never secrets
or customer contact details. A nonzero exit means reconciliation needs attention.
"""
import json
import sqlite3
import sys
from pathlib import Path
from urllib.request import Request, urlopen


def audit(path):
    with sqlite3.connect(path, timeout=10) as db:
        db.row_factory = sqlite3.Row
        identity = db.execute("select * from local_device_identity order by id desc limit 1").fetchone()
        lease = db.execute("select signed_token from local_licence_lease order by id desc limit 1").fetchone()
        base = db.execute("select sync_api_base_url from business_settings where id=1").fetchone()[0].rstrip('/')
        if not base.startswith('https://'):
            raise RuntimeError('Audit requires an HTTPS cloud API')
        headers = {'Authorization': 'Bearer ' + lease[0],
                   'X-SalonMax-Business-Id': identity['business_account_public_id'],
                   'X-SalonMax-Device-Id': identity['terminal_device_public_id'],
                   'Cache-Control': 'no-cache'}

        def get(route):
            with urlopen(Request(base + route, headers=headers), timeout=15) as response:
                return json.load(response)

        profiles = get('/customers/profiles')['profiles']
        directory = get('/customers/directory')['data']['customers']
        courses = get('/courses')
        estate = get('/estate-inventory')
        local = {str(r['customer_number']): dict(r) for r in db.execute('select * from customers')}
        differences = []
        fields = ('first_name', 'last_name', 'birthday', 'email', 'phone', 'postal_address')
        for remote in profiles:
            number = str(remote['customer_number'])
            row = local.get(number)
            if row is None:
                differences.append({'account': number, 'fields': ['missing_on_till']})
                continue
            mismatch = [f for f in fields if (row.get(f) or '') != (remote.get(f) or '')]
            if bool(row['is_active']) != (remote['status'] == 'active'):
                mismatch.append('status')
            if mismatch:
                differences.append({'account': number, 'fields': mismatch})
        cloud_numbers = {str(r['customer_number']) for r in profiles}
        for number in local.keys() - cloud_numbers:
            differences.append({'account': number, 'fields': ['missing_in_cloud']})
        for remote in directory:
            number = str(remote['customer_number'])
            row = local.get(number)
            if row:
                mismatch = []
                if int(row['package_minutes']) != int(remote['minutes_available']):
                    mismatch.append('package_minutes')
                if round(float(row['account_balance']), 2) != round(float(remote['current_balance']), 2):
                    mismatch.append('account_balance')
                if mismatch:
                    differences.append({'account': number, 'fields': mismatch})
        from course_sync import catalogue
        remote_courses = courses.get('courses', courses.get('data', []))
        if isinstance(remote_courses, dict):
            remote_courses = remote_courses.get('courses', [])
        course_differences = []
        local_courses = catalogue(db)
        remote_by_code = {r['code']: {f: r[f] for f in ('title', 'minutes', 'amount', 'validity_days', 'active')} for r in remote_courses}
        for code in local_courses.keys() | remote_by_code.keys():
            if local_courses.get(code) != remote_by_code.get(code):
                course_differences.append(code)
        queues = {}
        for table, condition in (('local_sync_outbox', "status!='acknowledged'"),
                                 ('local_profile_outbox', '1=1')):
            queues[table] = db.execute(f'select count(*) from {table} where {condition}').fetchone()[0]
        result = {
            'database_integrity': db.execute('pragma quick_check').fetchone()[0],
            'foreign_key_violations': len(db.execute('pragma foreign_key_check').fetchall()),
            'customers': {'active': sum(bool(r['is_active']) for r in local.values()),
                          'archived': sum(not bool(r['is_active']) for r in local.values()),
                          'cloud_profiles': len(profiles), 'cloud_active': len(directory)},
            'customer_differences': differences, 'course_differences': course_differences,
            'pending_queues': queues,
            'estate': {'cloud_sites': len(estate['sites']), 'cloud_terminals': len(estate['terminals']),
                       'cloud_beds': estate['bed_count']},
            'duplicate_open_sessions': [dict(r) for r in db.execute(
                "select terminal_id,count(*) as sessions from till_sessions where status='open' group by terminal_id having count(*)>1")],
            'not_verified': ['archived customer financial balances',
                             'multiple tills and sites exchanging sales',
                             'staff/PINs and web login roles',
                             'pricing, stock and business settings across independent databases',
                             'concurrent edits and offline recovery across terminals'],
        }
        print(json.dumps(result, indent=2))
        return int(bool(differences or course_differences or any(queues.values()) or result['duplicate_open_sessions'] or result['foreign_key_violations'] or result['database_integrity'] != 'ok'))


if __name__ == '__main__':
    sys.exit(audit(Path.home() / 'till-build/till_backoffice.db'))
