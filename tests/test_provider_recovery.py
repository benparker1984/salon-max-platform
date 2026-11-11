"""Integration checks. Run only against a disposable PostgreSQL database."""
import io
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
import pytest


@pytest.fixture(scope='module')
def platform():
    url = os.environ.get('SALONMAX_DATABASE_URL') or os.environ.get('DATABASE_URL','')
    if urlparse(url).hostname not in {'127.0.0.1','localhost'}:
        pytest.skip('Integration tests require an explicitly configured local PostgreSQL instance')
    from app import app
    import provider
    app.config.update(TESTING=True)
    yield app,provider


@pytest.fixture
def tenant(platform):
    app,p = platform
    suffix=uuid.uuid4().hex[:12]
    business,site,device='biz_check_'+suffix,'site_check_'+suffix,'term_check_'+suffix
    token='sm-lease:check-'+uuid.uuid4().hex
    with app.app_context():
        p.ensure_cloud_business_account(business,'Recovery verification')
        p.ensure_cloud_site_record(business,site,'Verification','VERIFY')
        p.ensure_cloud_terminal_record(business,site,device,'Verification Till')
        p.upsert_cloud_device_licence(business_account_public_id=business,terminal_device_public_id=device,licence_status='active',signed_token=token,issued_at=p.now_utc_text(),expires_at='2099-01-01T00:00:00Z',last_check_in_at=p.now_utc_text())
    headers={'Authorization':'Bearer '+token,'X-SalonMax-Business-Id':business,'X-SalonMax-Device-Id':device}
    yield business,site,device,headers
    with app.app_context():
        for table in ['cloud_customer_balance_ledger','cloud_customer_minute_ledger','cloud_customer_balance_summary','cloud_customer_minute_summary','cloud_customers','cloud_support_notes','cloud_sync_events','cloud_device_licences','cloud_terminal_registry','cloud_business_sites','platform_media']:
            p.platform_execute(f'delete from {table} where business_account_public_id=?',(business,))
        p.platform_execute('delete from cloud_business_accounts where business_account_public_id=?',(business,))
        import commissioning as m
        m.execute('delete from terminals where business_public_id=?',(business,))
        m.execute('delete from sites where business_public_id=?',(business,))
        m.execute('delete from businesses where public_id=?',(business,))


def owner_client(app):
    c=app.test_client()
    with c.session_transaction() as session:
        session['platform_admin_authenticated']=True
        session['salonmax_platform_authed']=True
    return c


def sale(device,event_id='sale-1',customer_id=123):
    return {'terminal_device_id':device,'events':[{'local_event_id':event_id,'event_type':'transaction.completed','created_at':'2026-11-10T12:00:00Z',
             'payload':{'customer_id':customer_id,'customer_first_name':'Verification','transaction_type':'package_sale','minutes_included':30,'transaction_number':event_id}}]}


def test_restoration_is_repeatable_without_overwriting(platform):
    app,p=platform
    import recovery
    with app.app_context():
        before={r['customer_public_id']:dict(r) for r in p.platform_query_all('select * from cloud_customer_minute_summary')}
        recovery.restore(p)
        assert before=={r['customer_public_id']:dict(r) for r in p.platform_query_all('select * from cloud_customer_minute_summary')}


def test_authenticated_provider_pages_and_export(platform):
    app,p=platform
    c=owner_client(app)
    for route in ['/platform','/platform/queries','/platform/analytics','/platform/customer-insights','/platform/licences','/platform/diagnostics','/platform/updates','/platform/estate-health','/commissioning/platform']:
        assert c.get(route).status_code==200,route
    for route in ['/platform/stats', '/commissioning/platform/stats']:
        retired = c.get(route)
        assert retired.status_code == 301
        assert retired.location == '/platform/estate-health'
    for route in ['/kado', '/gym/test/join', '/platform/gyms', '/platform/business/test/gym-access']:
        assert c.get(route).status_code == 404
    assert c.post('/stripe/webhook').status_code == 404
    response=c.get('/platform/export.json')
    assert response.status_code==200
    assert response.json['database']=='postgres'
    assert 'cloud_customer_minute_ledger' in response.json['provider']
    assert app.test_client().get('/platform/export.json').status_code==302


def test_sync_retry_and_parallel_retry_credit_once(platform,tenant):
    app,p=platform
    business,site,device,headers=tenant
    def push():
        return app.test_client().post('/v1/sync/events/push',json=sale(device),headers=headers)
    with ThreadPoolExecutor(max_workers=3) as executor:
        results=list(executor.map(lambda _:push(),range(3)))
    assert all(r.status_code==200 for r in results)
    with app.app_context():
        assert len(p.platform_query_all('select * from cloud_sync_events where business_account_public_id=?',(business,)))==1
        assert p.platform_query_one('select minutes_available from cloud_customer_minute_summary where business_account_public_id=?',(business,))['minutes_available']==30
        assert len(p.platform_query_all('select * from cloud_customer_minute_ledger where business_account_public_id=?',(business,)))==1


def test_sync_batch_rolls_back_on_projection_error(platform,tenant):
    app,p=platform
    business,site,device,headers=tenant
    payload=sale(device)
    bad=sale(device,'bad-2',456)['events'][0]
    bad['payload']['minutes_included']='invalid'
    payload['events'].append(bad)
    response=app.test_client().post('/v1/sync/events/push',json=payload,headers=headers)
    assert response.status_code==400
    with app.app_context():
        assert not p.platform_query_one('select id from cloud_sync_events where business_account_public_id=?',(business,))
        assert not p.platform_query_one('select id from cloud_customer_minute_ledger where business_account_public_id=?',(business,))


def test_device_auth_and_tenant_boundaries(platform,tenant):
    app,p=platform
    business,site,device,headers=tenant
    c=app.test_client()
    assert c.post('/v1/sync/events/push',json=sale(device)).status_code==403
    assert c.post('/v1/sync/events/push',json=[],headers=headers).status_code==400
    bad_headers={**headers,'Authorization':'Bearer sm-lease:forged'}
    assert c.get('/v1/customers/directory',headers=bad_headers).status_code==403
    payload=sale(device)
    payload['events'][0]['business_account_id']='biz_another_business'
    assert c.post('/v1/sync/events/push',json=payload,headers=headers).status_code==403
    with app.app_context():
        p.platform_execute("update cloud_business_accounts set status='suspended' where business_account_public_id=?",(business,))
    for route in ['/v1/customers/corrections','/v1/customers/directory',f'/v1/devices/{device}/config']:
        assert c.get(route,headers=headers).status_code==403


def test_checkin_preserves_name_and_concurrent_token(platform,tenant):
    app,p=platform
    business,site,device,headers=tenant
    c=app.test_client()
    response=c.post('/v1/licence/check-in',json={'terminal_device_id':device,'app_version':'verification'},headers=headers)
    assert response.status_code==200
    assert 'Bearer '+response.json['data']['signed_token']==headers['Authorization']
    assert c.get('/v1/customers/directory',headers=headers).status_code==200
    assert c.get('/v1/customers/corrections',headers=headers).status_code==200
    assert c.get(f'/v1/devices/{device}/config',headers=headers).status_code==200
    with app.app_context():
        assert p.platform_query_one('select business_name from cloud_business_accounts where business_account_public_id=?',(business,))['business_name']=='Recovery verification'


def test_customer_import_adjustment_and_directory(platform,tenant):
    app,p=platform
    business,site,device,headers=tenant
    c=owner_client(app)
    response=c.post(f'/platform/business/{business}/customers/import',data={'csv_text':'customer_number,first_name,last_name,balance,minutes\n9001,Check,Recovery,5.00,15','author_name':'Verification'})
    assert response.status_code==302
    with app.app_context():
        customer=p.platform_query_one('select * from cloud_customers where business_account_public_id=?',(business,))
        assert customer
    customer_id=customer['customer_public_id']
    assert c.get(f'/platform/business/{business}/customers/{customer_id}').status_code==200
    assert c.post(f'/platform/business/{business}/customers/{customer_id}/adjust',data={'ledger_kind':'minutes','delta_minutes':'5','reason':'Verification correction','author_name':'Verification'}).status_code==302
    directory=app.test_client().get('/v1/customers/directory',headers=headers)
    corrections=app.test_client().get('/v1/customers/corrections',headers=headers)
    assert directory.status_code==200 and corrections.status_code==200


def test_media_is_authenticated_and_exported(platform,tenant):
    app,p=platform
    business,site,device,headers=tenant
    c=owner_client(app)
    response=c.post(f'/platform/business/{business}/media',data={'image':(io.BytesIO(b'\x89PNG\r\n\x1a\nverification'),'logo.png')})
    assert response.status_code==200
    media_path=urlparse(response.json['url']).path
    assert c.get(media_path).status_code==200
    assert app.test_client().get(media_path).status_code==302
    assert c.get('/platform/export.json').json['provider']['platform_media']


def test_pairing_issues_unpredictable_single_use_token(platform,tenant):
    app,p=platform
    business,site,device,headers=tenant
    with app.app_context():
        code=p.platform_query_one('select pairing_code from cloud_terminal_registry where terminal_device_public_id=?',(device,))['pairing_code']
    c=app.test_client()
    response=c.post('/v1/devices/pair',json={'pairing_code':code,'device_serial':'verification'})
    assert response.status_code==200
    assert len(response.json['data']['signed_token'])>60
    assert response.json['data']['signed_token']!=headers['Authorization'][7:]
    assert c.post('/v1/devices/pair',json={'pairing_code':code}).status_code==404


def test_newer_commissioning_profile_is_retained(platform,tenant):
    app,p=platform
    business,site,device,headers=tenant
    c=owner_client(app)
    assert c.get(f'/commissioning/platform/business/{business}').status_code==200
    response=c.post(f'/commissioning/platform/business/{business}/remote-setup',data={
        'remote_setup_status':'ready_to_apply','salon_display_name':'Commissioned Salon','backoffice_display_name':'Commissioned Office',
        'opening_hours_setup':'Monday 09:00-18:00','pricing_setup':'Peak 0.65','staff_setup':'','sunbed_setup':'Bed 1'})
    assert response.status_code==302
    profile=app.test_client().get(f'/v1/devices/{device}/setup-profile',headers=headers)
    assert profile.status_code==200
    assert profile.json['data']['salon_display_name']=='Commissioned Salon'
    config=app.test_client().get(f'/v1/devices/{device}/config',headers=headers)
    assert config.json['data']['setup_profile']['opening_hours_setup']=='Monday 09:00-18:00'


def test_history_does_not_overwrite_newer_device_metadata(platform,tenant):
    app,p=platform
    business,site,device,headers=tenant
    with app.app_context():
        p.platform_execute("update cloud_terminal_registry set terminal_name='Newer Till', install_mode='replacement', app_version_reported='newer-2.0', last_seen_at='2026-11-10T21:00:00Z' where terminal_device_public_id=?",(device,))
        p.platform_execute("update cloud_device_licences set last_check_in_at='2026-11-10T20:00:00Z' where terminal_device_public_id=?",(device,))
        p.platform_execute('insert into cloud_sync_events (business_account_public_id,terminal_device_public_id,local_event_id,event_type,occurred_at,payload_json,received_at) values (?,?,?,?,?,?,?)',
            (business,device,'historical','transaction.completed','2026-05-01T10:00:00Z','{}','2026-11-10T20:00:00Z'))
        p.reconcile_runtime_terminals_for_business(business)
        row=p.platform_query_one('select * from cloud_terminal_registry where terminal_device_public_id=?',(device,))
        assert row['terminal_name']=='Newer Till'
        assert row['install_mode']=='replacement'
        assert row['app_version_reported']=='newer-2.0'
        assert row['last_seen_at']=='2026-11-10T21:00:00Z'


def test_newer_business_pairing_code_still_works(platform,tenant):
    app,p=platform
    business,site,device,headers=tenant
    c=owner_client(app)
    assert c.get(f'/commissioning/platform/business/{business}').status_code==200
    with app.app_context():
        import commissioning as m
        m.execute('update businesses set pairing_code=? where public_id=?',('VERIFYNEWCODE',business))
    response=app.test_client().post('/v1/pairing/claim',json={'pairing_code':'VERIFYNEWCODE','device_serial':'verify-new-pair','terminal_name':'Second Till'})
    assert response.status_code==200
    data=response.json['data']
    assert data['business_account_public_id']==business
    assert data['site_public_id']==site
    new_headers={'Authorization':'Bearer '+data['signed_token'],'X-SalonMax-Business-Id':business,'X-SalonMax-Device-Id':data['terminal_device_public_id']}
    assert app.test_client().get('/v1/customers/directory',headers=new_headers).status_code==200


def test_salon_login_invite_and_tenant_isolation(platform, tenant):
    import re
    app, p = platform
    business, *_ = tenant
    other = "biz_other_" + uuid.uuid4().hex[:12]
    with app.app_context():
        p.ensure_cloud_business_account(other, "Other salon")
    admin = owner_client(app)
    path = f"/platform/business/{business}/web-access"
    assert admin.get(path).status_code == 200
    with admin.session_transaction() as state:
        csrf = state["salon_csrf"]
    assert admin.post(path, data={"role": "owner", "login": "owner"}).status_code == 400
    result = admin.post(path, data={"role": "owner", "login": "owner", "csrf": csrf})
    link = re.search(rb'/salon/[^/]+/activate/[A-Za-z0-9_-]+', result.data)[0].decode()
    browser = app.test_client()
    assert browser.get(link).status_code == 200
    with browser.session_transaction() as state:
        csrf = state["salon_csrf"]
    password = "unique fixture password"
    assert browser.post(link, data={"csrf": csrf, "password": password}).status_code == 303
    assert browser.get(link).status_code == 404
    assert browser.post(f"/salon/{business}/login", data={
        "csrf": csrf, "login": "owner", "role": "owner", "password": password
    }).status_code == 303
    assert browser.get(f"/salon/{business}/offers").status_code == 200
    assert browser.get(f"/salon/{other}/offers").status_code == 403
    assert browser.get(f"/salon/{other}/access").status_code == 403
    assert browser.get("/platform").status_code == 302
    with app.app_context():
        p.platform_execute("update web_accounts set disabled=1 where tenant=?", (business,))
    assert browser.get(f"/salon/{business}/offers").status_code == 403
    assert browser.get("/salon/").status_code == 200
    assert browser.get("/salon/service-worker.js").status_code == 200
    assert browser.get("/static/salon.webmanifest").json["scope"] == "/salon/"


def test_signed_payment_and_consent_withdrawal(platform, tenant, monkeypatch):
    import hashlib
    import hmac
    import json
    import time
    import marketing

    app, p = platform
    business, *_ = tenant
    order = uuid.uuid4().hex
    staff = uuid.uuid4().hex
    customer = uuid.uuid4().hex
    token = uuid.uuid4().hex
    monkeypatch.setenv("SALONMAX_STRIPE_WEBHOOK_SECRET", "test-secret")
    with app.app_context():
        p.platform_execute("""
            insert into cloud_customers (business_account_public_id,customer_public_id,email,status)
            values (?,?,?,'active')
        """, (business, customer, "fixture@example.invalid"))
        p.platform_execute("""
            insert into web_orders (order_id,tenant,customer_id,stripe_account,offer_id,minutes,amount)
            values (?,?,?,'acct_test','fixture',30,1000)
        """, (order, business, customer))
        p.platform_execute("""
            insert into web_accounts (account_id,tenant,login,role) values (?,?,'staff','staff')
        """, (staff, business))
        p.platform_execute("insert into web_marketing_consent values (?,?, 'email', 'fixture@example.invalid',1,?)",
                           (business, customer, p.now_utc_text()))
        p.platform_execute("""
            insert into web_marketing_outbox values (?,?,?,'email','fixture@example.invalid','Test','Test',?,'queued',?,null)
        """, (uuid.uuid4().hex, business, customer, hashlib.sha256(token.encode()).hexdigest(), p.now_utc_text()))
    browser = app.test_client()
    with browser.session_transaction() as state:
        state["salon_account"] = staff
    assert browser.get(f"/salon/{business}/access").status_code == 200
    assert browser.get(f"/salon/{business}/offers").status_code == 403
    assert browser.get(f"/salon/{business}/marketing").status_code == 403
    event = {"account": "acct_test", "type": "checkout.session.completed", "data": {"object": {
        "amount_total": 1000, "currency": "gbp", "payment_status": "paid", "metadata": {"order_id": order}
    }}}
    raw = json.dumps(event).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(b"test-secret", timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    url = "/salon/payments/webhook"
    assert browser.post(url, data=raw, content_type="application/json").status_code == 400
    for _ in range(2):
        assert browser.post(url, data=raw, content_type="application/json",
                            headers={"Stripe-Signature": f"t={timestamp},v1={signature}"}).status_code == 204
    unsubscribe = f"/salon/unsubscribe/{token}"
    assert browser.get(unsubscribe).status_code == 200
    with browser.session_transaction() as state:
        csrf = state["salon_csrf"]
    assert browser.post(unsubscribe, data={"csrf": csrf}).status_code == 200
    deliveries = []
    monkeypatch.setattr(marketing, "deliver", deliveries.append)
    with app.app_context():
        marketing.dispatch(25)
        assert deliveries == []
        assert p.platform_query_one("select status from web_marketing_outbox where tenant=?", (business,))["status"] == "suppressed"
        assert p.platform_query_one("select count(*) as count from cloud_customer_minute_ledger where source_reference=?", (order,))["count"] == 1
        assert p.platform_query_one("select minutes_available from cloud_customer_minute_summary where customer_public_id=?", (customer,))["minutes_available"] == 30
