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
        p.platform_execute('delete from web_pi_backoffices where tenant=?',(business,))
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
    result = admin.post(path, data={"role": "owner", "login": "owner@example.invalid", "csrf": csrf})
    link = re.search(rb'/salon/[^/]+/activate/[A-Za-z0-9_-]+', result.data)[0].decode()
    browser = app.test_client()
    assert browser.get(link).status_code == 200
    with browser.session_transaction() as state:
        csrf = state["salon_csrf"]
    password = "unique fixture password"
    assert browser.post(link, data={"csrf": csrf, "password": password}).status_code == 303
    assert browser.get(link).status_code == 404
    assert browser.post(f"/salon/{business}/login", data={
        "csrf": csrf, "login": "owner@example.invalid", "role": "owner", "password": password
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


def test_customer_portals_keep_same_email_separate(platform, tenant):
    from werkzeug.security import generate_password_hash

    app, p = platform
    first, *_ = tenant
    second = "biz_other_" + uuid.uuid4().hex[:12]
    email = "same@example.invalid"
    with app.app_context():
        p.ensure_cloud_business_account(second, "Other salon")
        for business, balance, password in [(first, 19, "First salon password"), (second, 73, "Second salon password")]:
            customer = uuid.uuid4().hex
            p.platform_execute(
                "insert into cloud_customers (business_account_public_id,customer_public_id,email,status) values (?,?,?,'active')",
                (business, customer, email),
            )
            p.platform_execute(
                "insert into cloud_customer_minute_summary (business_account_public_id,customer_public_id,minutes_available) values (?,?,?)",
                (business, customer, balance),
            )
            p.platform_execute(
                "insert into web_accounts (account_id,tenant,login,role,customer_id,password_hash) values (?,?,?,'customer',?,?)",
                (uuid.uuid4().hex, business, email, customer, generate_password_hash(password)),
            )
    client = app.test_client()
    for business, wrong_password, correct_password, balance in [
        (first, "Second salon password", "First salon password", 19),
        (second, "First salon password", "Second salon password", 73),
    ]:
        base = f"/salon/{business}/"
        manifest = client.get(base + "manifest.webmanifest").json
        assert manifest["id"] == manifest["start_url"] == manifest["scope"] == base
        response = client.get(base + "customer")
        assert b"Email address" in response.data and b"Account type" not in response.data
        with client.session_transaction() as state:
            csrf = state["salon_csrf"]
        assert b"did not match" in client.post(base + "customer", data={
            "csrf": csrf, "login": email, "password": wrong_password
        }).data
        assert client.post(base + "customer", data={
            "csrf": csrf, "login": email, "password": correct_password
        }).status_code == 303
        assert f"{balance} available".encode() in client.get(base).data
        other = second if business == first else first
        assert client.get(f"/salon/{other}/").status_code == 403


def test_onboarding_provisions_hosted_backoffice(platform, monkeypatch):
    import json
    import re
    from contextlib import nullcontext

    import backoffice

    app, p = platform
    suffix = uuid.uuid4().hex[:8]
    name = f"Ultra Violet {suffix}"
    email = f"{suffix}@example.invalid"
    admin = owner_client(app)
    response = admin.post("/platform/onboard/create", data={
        "business_name": name, "site_name": "Main", "terminal_name": "Till 1", "contact_email": email,
    })
    assert response.status_code == 302
    path = urlparse(response.location).path
    tenant = path.rsplit("/", 1)[-1]
    address = f"/salonmax-ultraviolet{suffix}-backoffice"
    assert address.encode() in admin.get(path).data
    commissioning_page = admin.get(f"/commissioning/platform/business/{tenant}")
    assert commissioning_page.status_code == 200
    assert address.encode() in commissioning_page.data
    assert b"Salon owner web access" in commissioning_page.data
    with app.app_context():
        assert p.platform_query_one("select login from web_accounts where tenant=? and role='owner'", (tenant,))["login"] == email
        other = "biz_other_" + suffix
        p.ensure_cloud_business_account(other, "Other Business " + suffix)
        other_address = "/" + backoffice.setup(other, "Other Business " + suffix)
    management = f"/platform/business/{tenant}/web-access"
    assert b"Awaiting setup" in admin.get(management).data
    with admin.session_transaction() as state:
        csrf = state["salon_csrf"]
    result = admin.post(management, data={"csrf": csrf, "role": "owner", "login": email})
    link = re.search(rb'/salon/[^/]+/activate/[A-Za-z0-9_-]+', result.data)[0].decode()
    browser = app.test_client()
    assert browser.get(address).location == address + "/register"
    assert b"Forgot your password?" in browser.get(address + "/sign-in").data
    assert b"Register your back office account" in browser.get(link).data
    with browser.session_transaction() as state:
        csrf = state["salon_csrf"]
    assert browser.post(link, data={
        "csrf": csrf, "email": "wrong@example.invalid", "password": "First valid password"
    }).status_code == 400
    registration = {"csrf": csrf, "email": email, "password": "First valid password",
                    "setup_code": "https://www.salonmax.co.uk" + link}
    registered = browser.post(address + "/register", data=registration)
    code = re.search(rb'Recovery code<input readonly value="([A-Za-z0-9_-]{32})"', registered.data)[1].decode()
    replay = browser.post(address + "/register", data=registration)
    assert replay.status_code == 200 and b"already used" in replay.data
    assert browser.post(address + "/sign-in", data={
        "csrf": csrf, "email": email, "role": "owner", "password": "First valid password"
    }).status_code == 303
    assert browser.get(address).status_code == 200
    assert browser.get(f"/salon/{tenant}/").location == address
    for section in ("access", "offers", "marketing"):
        management_page = browser.get(f"/salon/{tenant}/{section}")
        assert management_page.status_code == 200
        assert b"/static/backoffice.css" in management_page.data
        assert b'aria-label="Back office"' in management_page.data
    for section in ("settings", "sunbeds", "customers", "pricing", "store", "transactions", "tills", "reports"):
        assert browser.get(f"{address}/{section}").status_code == 200, section
    assert browser.get(other_address + "/customers").status_code == 403
    assert browser.post(other_address + "/settings", data={"phone": "unauthorised"}).status_code == 403

    reset_path = address + "/forgot-password"
    resetter = app.test_client()
    assert b"Reset with a recovery code" in resetter.get(reset_path).data
    with resetter.session_transaction() as state:
        csrf = state["salon_csrf"]
    recovered = resetter.post(reset_path, data={
        "csrf": csrf, "email": email, "method": "recovery-code", "code": code,
        "password": "Recovered fixture password",
    })
    assert b"Password saved" in recovered.data
    assert b"did not match" in resetter.post(reset_path, data={
        "csrf": csrf, "email": email, "method": "recovery-code", "code": code,
        "password": "Cannot reuse the code",
    }).data
    assert browser.get(address).status_code == 403
    assert resetter.post(address + "/sign-in", data={
        "csrf": csrf, "email": email, "role": "owner", "password": "Recovered fixture password"
    }).status_code == 303

    sent = []
    monkeypatch.setenv("SALONMAX_TRANSACTIONAL_EMAIL_KEY", "fixture-key")
    monkeypatch.setenv("SALONMAX_TRANSACTIONAL_EMAIL_FROM", "fixture@example.invalid")
    def deliver(message, **kwargs):
        sent.append(json.loads(message.data))
        return nullcontext()
    monkeypatch.setattr(backoffice, "urlopen", deliver)
    signed_in = resetter
    resetter = app.test_client()
    reset_path = address + "/forgot-password"
    assert resetter.get(reset_path).status_code == 200
    with resetter.session_transaction() as state:
        csrf = state["salon_csrf"]
    assert resetter.post(reset_path, data={"csrf": csrf, "email": email}).status_code == 200
    assert sent[0]["to"] == [email]
    reset_link = re.search(r'/salon/[^/]+/activate/[A-Za-z0-9_-]+', sent[0]["text"])[0]
    assert b"Password saved" in resetter.post(reset_link, data={
        "csrf": csrf, "email": email, "password": "Second valid password"
    }).data
    assert resetter.get(reset_link).status_code == 404
    assert signed_in.get(address).status_code == 403
    assert b"did not match" in resetter.post(address + "/sign-in", data={
        "csrf": csrf, "email": email, "role": "owner", "password": "First valid password"
    }).data
    assert resetter.post(address + "/sign-in", data={
        "csrf": csrf, "email": email, "role": "owner", "password": "Second valid password"
    }).status_code == 303


def test_pi_backoffice_links_stay_with_paired_terminal(platform, tenant):
    import backoffice

    app, p = platform
    business, _, terminal, device_headers = tenant
    octets = uuid.uuid4().bytes
    origin = f"http://10.{octets[0]}.{octets[1]}.{octets[2]}:5001"
    with app.app_context():
        slug = backoffice.setup(business, "Linked salon")
        account = uuid.uuid4().hex
        p.platform_execute("insert into web_accounts (account_id,tenant,login,role) values (?,?,?,'owner')",
                           (account, business, "link@example.invalid"))
    path = f"/platform/business/{business}/pi-back-office"
    browser = app.test_client()
    with browser.session_transaction() as state:
        state["salon_account"] = account
    assert browser.get(path).status_code == 302
    admin = owner_client(app)
    assert terminal.encode() in admin.get(path).data
    with admin.session_transaction() as state:
        csrf = state["salon_csrf"]
    assert admin.post(path, data={"csrf": csrf, "terminal_id": "another-tenant",
                                  "origin": origin}).status_code == 404
    assert admin.post(path, data={"csrf": csrf, "terminal_id": terminal,
                                  "origin": "https://example.invalid:5001"}).status_code == 400
    assert admin.post(path, data={"csrf": csrf, "terminal_id": terminal,
                                  "origin": origin}).status_code == 303
    result = browser.get(f"/{slug}")
    assert result.status_code == 200
    assert result.data.count(f'href="{origin}/"'.encode()) == 1
    assert b'<nav' not in result.data
    assert browser.get(f"/{slug}/store").location == f"/{slug}"
    payload = sale(terminal, event_id="backoffice-sale")
    payload["events"][0]["payload"].update(transaction_number="TX-VERIFY", total_amount="12.50")
    assert app.test_client().post("/v1/sync/events/push", json=payload, headers=device_headers).status_code == 200
    with app.app_context():
        p.platform_execute("update cloud_terminal_registry set management_status='suspended' where terminal_device_public_id=?",
                           (terminal,))
    assert f"{origin}/".encode() not in browser.get(f"/{slug}").data
    transactions = browser.get(f"/{slug}/transactions").data
    assert b"TX-VERIFY" in transactions and b"GBP 12.50" in transactions
    assert b"Verification" in browser.get(f"/{slug}/customers?q=verification").data
    assert b"No records yet." in browser.get(f"/{slug}/customers?q=not-a-customer").data


def test_customer_claim_and_tanning_history(platform, tenant, monkeypatch):
    import contextlib
    import json
    import re
    import salon_web

    app, p = platform
    business, _, terminal, headers = tenant
    email = "customer@example.invalid"
    for customer_id, minutes in ((123, 17), (456, 29)):
        payload = sale(terminal, event_id=f"visit-{customer_id}", customer_id=customer_id)
        payload["events"][0]["payload"].update(
            transaction_type="tanning_sale", minutes=minutes,
            customer_email=email if customer_id == 123 else "other@example.invalid",
        )
        assert app.test_client().post("/v1/sync/events/push", json=payload, headers=headers).status_code == 200

    emails = []
    def record_email(request, timeout):
        emails.append(json.loads(request.data))
        return contextlib.nullcontext()

    monkeypatch.setenv("SALONMAX_TRANSACTIONAL_EMAIL_KEY", "test-key")
    monkeypatch.setenv("SALONMAX_TRANSACTIONAL_EMAIL_FROM", "salon@example.invalid")
    monkeypatch.setattr(salon_web, "urlopen", record_email)
    browser = app.test_client()
    login = f"/salon/{business}/customer"
    browser.get(login)
    with browser.session_transaction() as state:
        csrf = state["salon_csrf"]
    assert browser.post(login + "/register", data={"csrf": csrf, "email": email}).status_code == 200
    assert len(emails) == 1 and emails[0]["to"] == [email]
    link = re.search(r"/salon/[^/]+/activate/[A-Za-z0-9_-]+", emails[0]["text"]).group()
    password = "Customer test password"
    assert browser.post(link, data={"csrf": csrf, "password": password}).status_code == 303
    assert browser.post(link, data={"csrf": csrf, "password": password}).status_code == 404
    result = browser.post(login, data={"csrf": csrf, "login": email, "password": password}, follow_redirects=True)
    assert b"Top up now" in result.data
    with app.app_context():
        p.platform_execute("insert into web_offers (tenant,offer_id,title,minutes,amount) values (?,?,?,?,?)",
                           (business, "course", "Salon course", 30, 1250))
        p.platform_execute("insert into web_offers (tenant,offer_id,title,minutes,amount) values (?,?,?,?,?)",
                           ("other-" + business, "course", "Other salon course", 30, 2000))
    catalogue = browser.get(login + "/top-up")
    assert b"Salon course" in catalogue.data and b"12.50" in catalogue.data
    assert b"Other salon course" not in catalogue.data
    history = browser.get(login + "/history")
    assert b"17 minutes" in history.data
    assert b"29 minutes" not in history.data


def test_salon_issues_customer_code_for_existing_account(platform, tenant):
    import re

    app, p = platform
    business, _, terminal, headers = tenant
    assert app.test_client().post("/v1/sync/events/push", json=sale(terminal), headers=headers).status_code == 200
    customer_id = p.make_cloud_customer_public_id(business, 123)
    with app.app_context():
        p.platform_execute("update cloud_customers set email='' where customer_public_id=?", (customer_id,))
    admin = owner_client(app)
    manage = f"/platform/business/{business}/customer-access"
    assert admin.get(manage).status_code == 200
    with admin.session_transaction() as state:
        csrf = state["salon_csrf"]
    email = "linked@example.invalid"
    response = admin.post(manage, data={"csrf": csrf, "role": "customer", "customer_id": customer_id, "login": email})
    assert response.status_code == 200
    code = re.search(rb'One-time setup code<input readonly value="([^"]+)"', response.data)[1].decode()
    browser = app.test_client()
    register = f"/salon/{business}/customer/register"
    browser.get(register)
    with browser.session_transaction() as state:
        csrf = state["salon_csrf"]
    response = browser.post(register, data={"csrf": csrf, "setup_code": code})
    assert response.status_code == 303
    assert browser.post(response.location, data={"csrf": csrf, "password": "New linked password"}).status_code == 303
    assert browser.post(register, data={"csrf": csrf, "setup_code": code}).status_code == 404
    result = browser.post(register.rsplit("/", 1)[0], data={
        "csrf": csrf, "login": email, "password": "New linked password",
    }, follow_redirects=True)
    assert b"30 available" in result.data


def test_customer_can_request_link_and_keep_existing_minutes(platform, tenant):
    app, p = platform
    business, _, terminal, headers = tenant
    assert app.test_client().post("/v1/sync/events/push", json=sale(terminal), headers=headers).status_code == 200
    customer_id = p.make_cloud_customer_public_id(business, 123)
    path = f"/salon/{business}/customer"
    browser = app.test_client()
    browser.get(path + "/join")
    with browser.session_transaction() as state:
        csrf = state["salon_csrf"]
    result = browser.post(path + "/join", data={
        "csrf": csrf, "full_name": "Link Test", "address_line": "1 Test Street", "town": "Test Town",
        "postcode": "AB1 2CD", "phone": "01234567890", "email": "link-test@example.invalid",
        "password": "New customer password",
    }, follow_redirects=True)
    assert b"Link my old salon account" in result.data
    assert b"30 available" not in result.data
    with browser.session_transaction() as state:
        csrf, account_id = state["salon_csrf"], state["salon_account"]
    assert browser.post(path + "/link", data={"csrf": csrf, "account_number": "123"}).status_code == 303
    admin = owner_client(app)
    manage = f"/platform/business/{business}/customer-access"
    assert b"1 Test Street" in admin.get(manage).data
    with admin.session_transaction() as state:
        csrf = state["salon_csrf"]
    form = {"csrf": csrf, "action": "link-customer", "account_id": account_id, "customer_id": customer_id}
    assert admin.post(manage, data=form).status_code == 400
    form["verified"] = "yes"
    assert admin.post(manage, data=form).status_code == 303
    assert admin.post(manage, data=form).status_code == 303
    assert b"30 available" in browser.get(f"/salon/{business}/").data


def test_till_course_import_updates_by_package_code(platform, tenant):
    app, p = platform
    business, *_ = tenant
    owner = uuid.uuid4().hex
    with app.app_context():
        p.platform_execute("insert into web_accounts (account_id,tenant,login,role) values (?,?,'owner','owner')",
                           (owner, business))
    client = app.test_client()
    with client.session_transaction() as state:
        state["salon_account"] = owner
        state["salon_csrf"] = "course-test"
    path = f"/salon/{business}/offers"
    form = {"csrf": "course-test", "action": "import-till-courses",
            "courses": "30 Minute Package,PKG30,30,30.00,365"}
    assert client.post(path, data=form).status_code == 303
    form["courses"] = "30 Minute Package,PKG30,30,32.50,365"
    assert client.post(path, data=form).status_code == 303
    with app.app_context():
        rows = p.platform_query_all("select amount from web_offers where tenant=?", (business,))
        assert [row["amount"] for row in rows] == [3250]
