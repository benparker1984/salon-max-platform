"""Keep commissioning metadata alongside the recovered provider registry.

The provider registry is authoritative for identity, lifecycle and runtime health.
The retained commissioning tables own their checklist and remote setup fields.
"""
import json


def upsert(modern, table, key, value, fields):
    if modern.query_one(f'select id from {table} where {key}=?', (value,)):
        modern.execute(f'update {table} set ' + ','.join(f'{k}=?' for k in fields) + f' where {key}=?', (*fields.values(), value))
    else:
        fields = {key:value, **fields}
        modern.execute(f'insert into {table} ('+','.join(fields)+') values ('+','.join('?' for _ in fields)+')', tuple(fields.values()))


def to_commissioning(provider, modern):
    for row in provider.platform_query_all('select * from cloud_business_accounts'):
        upsert(modern, 'businesses', 'public_id', row['business_account_public_id'], {
            'name':row['business_name'], 'status':row['status'], 'subscription_status':row['subscription_status'],
            'plan':row['subscription_plan'], 'contact_name':row['contact_name'], 'contact_email':row['contact_email'],
            'contact_phone':row['contact_phone'], 'address_line_1':row['address_line_1'], 'address_line_2':row['address_line_2'],
            'town':row['city'], 'postcode':row['postcode'], 'notes':row['notes'],
            'archived_at':row['updated_at'] if row['status']=='deleted' else None,
            'created_at':row['created_at'], 'updated_at':row['updated_at']})
    for row in provider.platform_query_all('select * from cloud_business_sites'):
        upsert(modern, 'sites', 'recovered_public_id', row['site_public_id'], {
            'business_public_id':row['business_account_public_id'], 'name':row['site_name'], 'code':row['site_code'],
            'status':row['status'], 'archived_at':row['updated_at'] if row['status']=='deleted' else None,
            'created_at':row['created_at'], 'updated_at':row['updated_at']})
    for row in provider.platform_query_all('select * from cloud_terminal_registry'):
        site = modern.query_one('select id from sites where recovered_public_id=?', (row['site_public_id'],))
        upsert(modern, 'terminals', 'device_public_id', row['terminal_device_public_id'], {
            'business_public_id':row['business_account_public_id'], 'site_id':site['id'] if site else None,
            'terminal_name':row['terminal_name'], 'status':row['status'],
            'app_version':row['app_version_reported'], 'target_app_version':row['desired_app_version'],
            'last_seen_at':row['last_seen_at'], 'retired_at':row['updated_at'] if row['management_status']=='retired' else None,
            'created_at':row['created_at'], 'updated_at':row['updated_at']})


def from_commissioning(provider, modern):
    for row in modern.query_all('select * from businesses'):
        provider.ensure_cloud_business_account(row['public_id'], row['name'])
        provider.platform_execute('update cloud_business_accounts set business_name=?, status=?, subscription_status=?, subscription_plan=?, contact_name=?, contact_email=?, contact_phone=?, address_line_1=?, address_line_2=?, city=?, postcode=?, notes=?, updated_at=? where business_account_public_id=?',
            (row['name'], 'deleted' if row['archived_at'] else row['status'], row['subscription_status'], row['plan'], row['contact_name'], row['contact_email'], row['contact_phone'], row['address_line_1'], row['address_line_2'], row['town'], row['postcode'], row['notes'], row['updated_at'], row['public_id']))
    for row in modern.query_all('select * from sites'):
        public_id = row['recovered_public_id'] or f"site_online_{row['id']}"
        if not row['recovered_public_id']:
            modern.execute('update sites set recovered_public_id=? where id=?', (public_id, row['id']))
        provider.ensure_cloud_site_record(row['business_public_id'], public_id, row['name'], row['code'])
        provider.platform_execute('update cloud_business_sites set site_name=?, site_code=?, status=?, updated_at=? where site_public_id=?',
            (row['name'], row['code'], 'deleted' if row['archived_at'] else row['status'], row['updated_at'], public_id))
    for row in modern.query_all('select terminals.*, sites.recovered_public_id from terminals left join sites on sites.id=terminals.site_id'):
        if not row['device_public_id'] or not row['recovered_public_id']:
            continue
        existing = provider.platform_query_one('select id from cloud_terminal_registry where terminal_device_public_id=?', (row['device_public_id'],))
        if not existing:
            provider.ensure_cloud_terminal_record(row['business_public_id'], row['recovered_public_id'], row['device_public_id'], row['terminal_name'])
        provider.platform_execute('update cloud_terminal_registry set terminal_name=?, site_public_id=?, status=?, management_status=?, desired_app_version=?, updated_at=? where terminal_device_public_id=?',
            (row['terminal_name'], row['recovered_public_id'], row['status'], 'retired' if row['retired_at'] else ('suspended' if row['status']=='suspended' else 'active'), row['target_app_version'], row['updated_at'], row['device_public_id']))
