import importlib.util
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    import sys
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


@pytest.fixture
def repo(tmp_path):
    native = module('consistency_native_database', 'pi/till-source/native_till_app/database.py')
    repo = native.TillRepository.__new__(native.TillRepository)
    repo.database_path = tmp_path / 'till.db'
    with repo.connection() as db:
        db.execute('''create table till_sessions (
            id integer primary key, site_id integer, terminal_id integer,
            opened_by_user_id integer, opening_float real, expected_cash real,
            counted_cash real, variance real, status text, closing_notes text)''')
    return repo


def test_concurrent_open_resumes_one_cash_session(repo):
    with ThreadPoolExecutor(max_workers=4) as pool:
        sessions = list(pool.map(lambda staff: repo.open_till(1, 1, staff, 40), range(1, 5)))
    assert len({s.id for s in sessions}) == 1
    first = sessions[0]
    resumed = repo.open_till(1, 1, 8, 999)
    assert resumed.id == first.id
    assert resumed.opening_float == 40
    assert resumed.opened_by_user_id == first.opened_by_user_id
    assert len(repo.fetch_all('select * from till_sessions')) == 1


def test_closed_drawer_allows_next_session_and_other_terminals(repo):
    first = repo.open_till(1, 1, 1, 40)
    repo.execute("update till_sessions set status='closed' where id=?", (first.id,))
    assert repo.open_till(1, 1, 1, 30).id != first.id
    assert repo.open_till(1, 2, 1, 20).terminal_id == 2
    with pytest.raises(ValueError, match='another site'):
        repo.open_till(2, 2, 1, 20)


def test_dashboard_patch_counts_active_customers_and_distinct_drawers():
    patcher = module('consistency_patch', 'pi/backoffice_consistency.py')
    original = 'def dashboard_totals():\n' + ''.join(
        f'    value = query_one("{before}")\n' for before in patcher.REPLACEMENTS
    ) + '\ndef another():\n    pass\n'
    patched = patcher.patch(original)
    assert patcher.patch(patched) == patched
    with sqlite3.connect(':memory:') as db:
        db.executescript('''
            create table customers(is_active integer, package_minutes integer, account_balance real);
            insert into customers values(1,25,0),(1,101,0),(0,53,10);
            create table till_sessions(terminal_id integer,status text);
            insert into till_sessions values(1,'open'),(1,'open'),(2,'closed');
        ''')
        queries = list(patcher.REPLACEMENTS.values())
        assert [db.execute(q.rstrip('"')).fetchone()[0] for q in queries] == [2, 1, 126, 0]


def test_credit_retries_do_not_double_apply_or_skip_missing_account(repo):
    with repo.connection() as db:
        db.executescript('''
            create table customers(id integer primary key,customer_number text,account_number text,
                first_name text,last_name text,package_minutes integer,account_balance real);
            insert into customers values(1,'1001','','Same','Name',20,0);
            create table local_cloud_correction_checkpoint(id integer primary key,
                last_minute_ledger_id integer,last_balance_ledger_id integer,last_checked_at text);
            insert into local_cloud_correction_checkpoint values(1,0,0,null);
        ''')
    payload = {'minute_entries': [
        {'id': 1, 'customer_number': '1001', 'delta_minutes': 4},
        {'id': 2, 'customer_number': '1002', 'customer_first_name': 'Same',
         'customer_last_name': 'Name', 'delta_minutes': 8},
        {'id': 3, 'customer_number': '1001', 'delta_minutes': 2},
    ]}
    assert repo.apply_cloud_customer_corrections(payload)['applied_count'] == 1
    assert repo.apply_cloud_customer_corrections(payload)['applied_count'] == 0
    assert repo.fetch_one('select package_minutes from customers where id=1')[0] == 24
    assert repo.cloud_correction_checkpoint()['last_minute_ledger_id'] == 1
    repo.execute("insert into customers values(2,'1002','','Other','Person',0,0)")
    assert repo.apply_cloud_customer_corrections(payload)['applied_count'] == 2
    assert repo.apply_cloud_customer_corrections(payload)['applied_count'] == 0
    assert repo.fetch_one('select package_minutes from customers where id=1')[0] == 26
    assert repo.fetch_one('select package_minutes from customers where id=2')[0] == 8
