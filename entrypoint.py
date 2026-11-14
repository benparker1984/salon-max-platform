"""Unified hosted Salon Max provider, recovery, commissioning and Pi API."""
import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from flask import g, jsonify, redirect, request, session, url_for, Response, abort
import provider
import commissioning
import bridge
import recovery
import storage
import salon_web
import backoffice
import checkout
import marketing
import customer_profiles
from commercial import bp as commercial_bp

app = provider.app
app.register_blueprint(commercial_bp)
app.register_blueprint(salon_web.bp)
app.register_blueprint(backoffice.bp)
app.add_template_global(backoffice.office_url)
app.register_blueprint(checkout.bp)
app.register_blueprint(marketing.bp)
app.register_blueprint(customer_profiles.bp)
app.config.update(MAX_CONTENT_LENGTH=10 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=storage.postgres())

# Keep every current commissioning action. Its templates have their own namespace.
for rule in commissioning.app.url_map.iter_rules():
    if rule.endpoint == 'static' or rule.rule.startswith('/v1/') or rule.rule == '/healthz':
        continue
    app.add_url_rule('/commissioning' + rule.rule, rule.endpoint,
                     commissioning.app.view_functions[rule.endpoint], methods=rule.methods)
app.teardown_appcontext(commissioning.close_db)


def initialise():
    lock = storage.connect(provider.PLATFORM_DATABASE_PATH)
    try:
        if storage.postgres():
            lock.raw.execute('select pg_advisory_lock(734210011)')
            lock.commit()
        with app.app_context():
            provider.init_db()
            recovery.restore(provider)
            provider.restore_synced_topups()
            salon_web.init_schema()
            backoffice.init_schema()
            checkout.init_schema()
            marketing.init_schema()
            provider.platform_execute("""create table if not exists terminal_bed_inventory (
                business_id text not null, site_id text not null, terminal_id text not null,
                bed_number integer not null, primary key (business_id,site_id,terminal_id,bed_number)
            )""")
            app.config['PLATFORM_SCHEMA_READY'] = True
            commissioning.init_db()
            commissioning.ensure_column('sites', 'recovered_public_id', "text not null default ''")
            provider.platform_execute('create table if not exists platform_media (media_id text primary key, business_account_public_id text not null, content_type text not null, content_base64 text not null, filename text not null, created_at text not null)')
            provider.platform_execute('create table if not exists platform_schema_versions (version text primary key, applied_at text not null)')
            if not provider.platform_query_one('select version from platform_schema_versions where version=?', ('online-registry-bridge-v1',)):
                bridge.from_commissioning(provider, commissioning)
                provider.platform_execute('insert into platform_schema_versions values (?, ?)', ('online-registry-bridge-v1', provider.now_utc_text()))
            bridge.to_commissioning(provider, commissioning)
            backoffice.backfill()
    finally:
        lock.close()


initialise()


@app.before_request
def hosted_boundaries_and_device_auth():
    path = request.path
    if path.startswith('/commissioning') and not (session.get('platform_admin_authenticated') or session.get('salonmax_platform_authed')):
        return redirect(url_for('salonmax_platform_login', next=request.full_path.rstrip('?')))
    if provider.APP_ROLE == 'cloud' and path != '/' and not path.startswith(('/platform', '/salonmax-platform', '/v1/', '/static/', '/commissioning', '/healthz', '/salon/', '/salonmax-')):
        abort(404)
    if path.startswith('/commissioning') and request.method == 'GET':
        bridge.to_commissioning(provider, commissioning)
    if path.startswith('/v1/') and path not in {'/v1/devices/pair', '/v1/pairing/claim'}:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return provider.json_error('BAD_REQUEST', 'Request body must be a JSON object.', 400)
        device = request.headers.get('X-SalonMax-Device-Id', '').strip()
        body_device = str(payload.get('terminal_device_id') or '').strip()
        path_device = (request.view_args or {}).get('terminal_device_id', '')
        expected_device = path_device or body_device or device
        if device and device != expected_device:
            return provider.json_error('DEVICE_MISMATCH', 'Device identity does not match the request.', 403)
        terminal = provider.platform_query_one('select * from cloud_terminal_registry where terminal_device_public_id=?', (expected_device,))
        business = request.headers.get('X-SalonMax-Business-Id', '').strip()
        token = provider.bearer_token_value()
        licence = provider.platform_query_one('select * from cloud_device_licences where terminal_device_public_id=?', (expected_device,))
        if not terminal or not licence or (business and business != terminal['business_account_public_id']) or not token or not hmac.compare_digest(token, licence['signed_token']):
            return provider.json_error('DEVICE_NOT_AUTHORISED', 'A registered device and its current licence token are required.', 403)
        account = provider.platform_query_one('select * from cloud_business_accounts where business_account_public_id=?', (terminal['business_account_public_id'],))
        site = provider.platform_query_one('select * from cloud_business_sites where site_public_id=?', (terminal['site_public_id'],))
        allowed, _, message = provider.terminal_access_state(account, site, terminal)
        if not allowed:
            return provider.json_error('BUSINESS_SUSPENDED', message, 403)
        if not business:
            return provider.json_error('IDENTITY_REQUIRED', 'Business identity is required.', 400)
        if path == '/v1/sync/events/push':
            for event in payload.get('events', []):
                if isinstance(event, dict) and event.get('business_account_id', business) != business:
                    return provider.json_error('BUSINESS_MISMATCH', 'Events must belong to the authenticated business.', 403)
        g.device_terminal = terminal


@app.after_request
def sync_commissioning_edits(response):
    if request.path.startswith('/commissioning') and request.method == 'POST' and response.status_code < 400:
        bridge.from_commissioning(provider, commissioning)
        if g.get('new_salon_backoffice'):
            backoffice.setup(*g.new_salon_backoffice)
    return response


# The primary /platform is the full recovered provider. Root no longer opens salon trading pages.
app.view_functions['dashboard'] = lambda: redirect(url_for('salonmax_owner_console'))


@app.get('/healthz')
def healthz():
    row = provider.platform_query_one('select count(*) as count from platform_recovery_runs')
    return jsonify(ok=True, product='salon-max-platform', database='postgres' if storage.postgres() else 'sqlite',
                   release='salon-web-v16', recovery_applied=bool(row['count']))


@app.get('/platform/stats')
@app.get('/commissioning/platform/stats')
def redirect_stats():
    return redirect(url_for('estate_health'), code=301)


@app.get('/platform/estate-health')
def estate_health():
    bridge.to_commissioning(provider, commissioning)
    return commissioning.platform_analytics()


@app.get('/platform/export.json')
def platform_export():
    bridge.to_commissioning(provider, commissioning)
    names = ['cloud_business_accounts', 'cloud_business_sites', 'cloud_terminal_registry', 'cloud_device_licences',
             'cloud_sync_events', 'cloud_customers', 'cloud_customer_balance_ledger', 'cloud_customer_minute_ledger',
             'cloud_customer_balance_summary', 'cloud_customer_minute_summary', 'cloud_support_notes',
             'platform_media', 'platform_recovery_runs']
    data = {t:[dict(r) for r in provider.platform_query_all(f'select * from {t}')] for t in names}
    return jsonify(ok=True, product='salon-max-platform', database='postgres' if storage.postgres() else 'sqlite',
                   exported_at=provider.now_utc_text(),
                   businesses=[dict(r) for r in commissioning.query_all('select * from businesses order by id')],
                   sites=[dict(r) for r in commissioning.query_all('select * from sites order by id')],
                   terminals=[dict(r) for r in commissioning.query_all('select * from terminals order by id')],
                   provider=data)


@app.post('/v1/pairing/claim')
def pairing_claim():
    return provider.device_pair()


def setup_profile(terminal):
    bridge.to_commissioning(provider, commissioning)
    business = commissioning.query_one('select * from businesses where public_id=?', (terminal['business_account_public_id'],))
    site = commissioning.query_one('select * from sites where recovered_public_id=?', (terminal['site_public_id'],))
    profile = commissioning.business_setup_profile(business, site)
    for name in ('pricing_setup', 'staff_setup', 'sunbed_setup', 'opening_hours_setup'):
        try:
            profile[name + '_data'] = json.loads(profile[name])
        except (ValueError, TypeError):
            pass
    return profile


@app.route('/v1/estate-inventory', methods=['GET', 'POST'])
def estate_inventory():
    terminal = g.device_terminal
    business, site, device = (terminal[k] for k in
                              ('business_account_public_id', 'site_public_id', 'terminal_device_public_id'))
    db = provider.get_platform_db()
    if request.method == 'POST':
        body = request.get_json(silent=True) or {}
        beds = body.get('beds')
        if not isinstance(beds, list) or len(beds) > 100 or any(type(n) is not int or not 1 <= n <= 999 for n in beds):
            abort(400)
        if storage.postgres():
            db.raw.execute('select pg_advisory_xact_lock(hashtextextended(%s, 0))', (business,))
        db.execute('delete from terminal_bed_inventory where business_id=? and terminal_id=?', (business, device))
        for number in set(beds):
            db.execute('insert into terminal_bed_inventory values (?,?,?,?)', (business, site, device, number))
        db.commit()
    sites = db.execute("""
        select site_public_id,site_name,site_code,status from cloud_business_sites
        where business_account_public_id=? and status='active' order by site_name
    """, (business,)).fetchall()
    terminals = db.execute("""
        select t.terminal_name,t.terminal_device_public_id,t.site_public_id from cloud_terminal_registry t
        join cloud_business_sites s on s.business_account_public_id=t.business_account_public_id
                                   and s.site_public_id=t.site_public_id
        where t.business_account_public_id=? and t.management_status<>'retired' and s.status='active'
        order by t.terminal_name
    """, (business,)).fetchall()
    return jsonify(ok=True, sites=[dict(row) for row in sites], terminals=[dict(row) for row in terminals],
                   bed_count=provider.estate_bed_count(business))


@app.get('/v1/devices/<terminal_device_id>/setup-profile')
def device_setup_profile(terminal_device_id):
    return jsonify(ok=True, data=setup_profile(g.device_terminal))


original_config = provider.device_config


def device_config(terminal_device_id):
    result = original_config(terminal_device_id)
    if result.status_code == 200:
        payload = result.get_json()
        profile = setup_profile(g.device_terminal)
        # An uncommissioned hosted database is not a source of salon settings.
        # Leave existing Pi names, pricing, staff and hardware configuration alone.
        if profile['status'] not in {'ready_to_apply','applied'}:
            payload['data'] = {'terminal_device_id':terminal_device_id,
                               'business_account_public_id':g.device_terminal['business_account_public_id'],
                               'licence_status':'active','remote_setup_status':profile['status']}
        else:
            payload['data']['setup_profile'] = profile
        return jsonify(payload)
    return result


app.view_functions['device_config'] = device_config


def sync_events_push():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get('events'), list) or not payload['events']:
        return provider.json_error('BAD_REQUEST', 'events must be a non-empty list.', 400)
    if len(payload['events']) > 500:
        return provider.json_error('BAD_REQUEST', 'Send at most 500 events per batch.', 400)
    terminal = g.device_terminal
    business = terminal['business_account_public_id']
    device = terminal['terminal_device_public_id']
    db = provider.get_platform_db()
    accepted, rejected = [], []
    try:
        if storage.postgres():
            # Serialize projections per business: retries and parallel devices cannot
            # double-credit a sale or lose an update to a shared customer balance.
            db.raw.execute('select pg_advisory_xact_lock(hashtextextended(%s, 0))', (business,))
        for event in payload['events']:
            if not isinstance(event, dict):
                rejected.append({'local_event_id':'', 'reason':'Event item must be an object.'})
                continue
            event_id = str(event.get('local_event_id') or '').strip()
            event_type = str(event.get('event_type') or '').strip()
            if not event_id or not event_type:
                rejected.append({'local_event_id':event_id,'reason':'local_event_id and event_type are required.'})
                continue
            if event.get('business_account_id', business) != business:
                return provider.json_error('BUSINESS_MISMATCH', 'Event belongs to another business.', 403)
            existing = db.execute('select id from cloud_sync_events where business_account_public_id=? and terminal_device_public_id=? and local_event_id=?', (business,device,event_id)).fetchone()
            if not existing:
                event_payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
                occurred_at = str(event.get('created_at') or provider.now_utc_text())
                if not provider.parse_utc_text(occurred_at):
                    rejected.append({'local_event_id':event_id,'reason':'created_at must be a UTC timestamp.'})
                    continue
                db.execute('insert into cloud_sync_events (business_account_public_id,terminal_device_public_id,local_event_id,event_type,occurred_at,payload_json) values (?,?,?,?,?,?)',
                           (business,device,event_id,event_type,occurred_at,json.dumps(event_payload, separators=(',',':'),sort_keys=True)))
                provider.project_sync_event_to_ledgers(db,business_account_public_id=business,terminal_device_public_id=device,local_event_id=event_id,event_type=event_type,occurred_at=occurred_at,payload=event_payload)
            accepted.append({'local_event_id':event_id,'cloud_event_id':event_id})
        db.commit()
    except (ValueError,TypeError,OverflowError):
        db.rollback()
        return provider.json_error('BAD_REQUEST','Event contains invalid transaction values.',400)
    except Exception:
        db.rollback()
        raise
    provider.mark_cloud_terminal_seen(device, provider.now_utc_text(), terminal['app_version_reported'])
    if isinstance(payload.get('sync_health'), dict):
        provider.update_cloud_terminal_sync_health(device,payload['sync_health'])
    return jsonify(ok=True,data={'accepted':accepted,'rejected':rejected},meta={'terminal_device_id':device,'business_account_id':business,'accepted_count':len(accepted),'rejected_count':len(rejected)})


app.view_functions['sync_events_push'] = sync_events_push


def licence_check_in():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not payload.get('terminal_device_id'):
        return provider.json_error('BAD_REQUEST','terminal_device_id is required.',400)
    terminal = g.device_terminal
    device = terminal['terminal_device_public_id']
    lease = provider.platform_query_one('select * from cloud_device_licences where terminal_device_public_id=?',(device,))
    now = datetime.now(timezone.utc)
    issued_at = provider.now_utc_text()
    expires_at = (now + timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%SZ')
    provider.mark_cloud_terminal_seen(device,issued_at,str(payload.get('app_version') or terminal['app_version_reported']))
    if isinstance(payload.get('sync_health'),dict):
        provider.update_cloud_terminal_sync_health(device,payload['sync_health'])
    provider.upsert_cloud_device_licence(business_account_public_id=terminal['business_account_public_id'], terminal_device_public_id=device,
        licence_status='active',signed_token=lease['signed_token'],issued_at=issued_at,expires_at=expires_at,last_check_in_at=issued_at)
    return jsonify(ok=True,data={'licence_status':'active','signed_token':lease['signed_token'],'issued_at':issued_at,'expires_at':expires_at,
        'grace_ends_at':(now+timedelta(days=37)).strftime('%Y-%m-%dT%H:%M:%SZ'),'access_message':''})


app.view_functions['licence_check_in'] = licence_check_in


original_pair = provider.device_pair


def secure_device_pair():
    body = request.get_json(silent=True)
    if isinstance(body,dict) and not body.get('pairing_code') and body.get('code'):
        body['pairing_code'] = body['code']
    result = original_pair()
    # The newer commissioning flow issues a business pairing code. Retain that
    # contract alongside the recovered per-terminal replacement/install codes.
    if result.status_code == 404 and isinstance(body,dict):
        code = str(body.get('pairing_code') or '').strip().upper()
        business = commissioning.query_one('select * from businesses where pairing_code=? and archived_at is null',(code,)) if code else None
        if business:
            account = provider.platform_query_one('select * from cloud_business_accounts where business_account_public_id=?',(business['public_id'],))
            allowed, _, message = provider.business_access_state(account)
            if not allowed:
                return provider.json_error('PAIRING_SUSPENDED',message,403)
            requested_device = str(body.get('terminal_device_public_id') or body.get('terminal_device_id') or body.get('device_id') or '')
            existing = provider.platform_query_one('select * from cloud_terminal_registry where terminal_device_public_id=?',(requested_device,)) if requested_device else None
            if existing and (existing['business_account_public_id']!=business['public_id'] or existing['management_status'] in {'retired','suspended'}):
                return provider.json_error('DEVICE_NOT_AUTHORISED','This device cannot be reassigned with that code.',403)
            result = commissioning.api_pairing_claim()
            if isinstance(result,tuple):
                return result
            if result.status_code != 200:
                return result
            bridge.from_commissioning(provider,commissioning)
            data = result.get_json()['data']
            device = data['terminal_device_public_id']
            terminal = provider.platform_query_one('select * from cloud_terminal_registry where terminal_device_public_id=?',(device,))
            data['site_public_id'] = terminal['site_public_id']
            provider.upsert_cloud_device_licence(business_account_public_id=business['public_id'],terminal_device_public_id=device,
                licence_status=data['licence_status'],signed_token=data['signed_token'],issued_at=data['issued_at'],expires_at=data['expires_at'],last_check_in_at=data['issued_at'])
            result = jsonify(ok=True,data=data)
    if result.status_code != 200:
        return result
    payload = result.get_json()
    data = payload['data']
    token = 'sm-lease:' + secrets.token_urlsafe(48)
    provider.platform_execute('update cloud_device_licences set signed_token=? where terminal_device_public_id=?', (token,data['terminal_device_public_id']))
    provider.platform_execute("update cloud_terminal_registry set pairing_code='' where terminal_device_public_id=?", (data['terminal_device_public_id'],))
    data['signed_token'] = token
    return jsonify(payload)


provider.device_pair = secure_device_pair
app.view_functions['device_pair'] = secure_device_pair


@app.post('/platform/business/<business_id>/media')
def upload_media(business_id):
    if not provider.platform_query_one('select id from cloud_business_accounts where business_account_public_id=?', (business_id,)):
        abort(404)
    file = request.files.get('image')
    if not file:
        abort(400)
    content = file.read(5 * 1024 * 1024 + 1)
    if len(content) > 5 * 1024 * 1024:
        abort(413)
    # Accept only raster image formats; serve with a fixed MIME and nosniff.
    mime = 'image/png' if content.startswith(b'\x89PNG\r\n\x1a\n') else ('image/jpeg' if content.startswith(b'\xff\xd8\xff') else ('image/webp' if content[:4]==b'RIFF' and content[8:12]==b'WEBP' else ''))
    if not mime:
        abort(415)
    media_id = hashlib.sha256(business_id.encode()+content).hexdigest()
    provider.platform_execute('insert into platform_media values (?, ?, ?, ?, ?, ?) on conflict do nothing',
        (media_id, business_id, mime, base64.b64encode(content).decode(), provider.secure_filename(file.filename or 'image'), provider.now_utc_text()))
    return jsonify(ok=True, url=url_for('platform_media', media_id=media_id, _external=True))


@app.get('/platform/media/<media_id>')
def platform_media(media_id):
    row = provider.platform_query_one('select * from platform_media where media_id=?', (media_id,))
    if not row:
        abort(404)
    return Response(base64.b64decode(row['content_base64']), content_type=row['content_type'], headers={'X-Content-Type-Options':'nosniff', 'Cache-Control':'private, max-age=3600'})
