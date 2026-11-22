import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import zipfile

import pytest

spec = importlib.util.spec_from_file_location('backup_bundle', Path(__file__).resolve().parents[1] / 'pi/create_backup_bundle.py')
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


@pytest.fixture
def home(tmp_path):
    (tmp_path / 'till-build').mkdir()
    (tmp_path / 'Salon-Max').mkdir()
    path = tmp_path / 'till-build/till_backoffice.db'
    with sqlite3.connect(path) as db:
        db.executescript('''pragma journal_mode=wal;
            create table customers(id integer primary key,customer_number text,first_name text,
                last_name text,account_balance real,package_minutes integer,is_active integer);
            insert into customers values(1,'1001','Test','Account',3.50,47,1);
            insert into customers values(2,'1002','Old','Account',0,9,0);
            create table local_sync_outbox(event_id text,payload text);
            insert into local_sync_outbox values('pending-1','unsent sale');''')
    return tmp_path


def test_bundle_restores_accounts_minutes_and_pending_writes(home, monkeypatch):
    monkeypatch.setattr(bundle, 'cloud_snapshot', lambda root: (b'{"test":true}', 2))
    data, manifest = bundle.create_bundle(home)
    assert manifest['cloud_status'] == 'ok'
    assert manifest['databases']['databases/till_backoffice.db']['total_minutes'] == 56
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.testzip() is None
        for name, meta in manifest['files'].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == meta['sha256']
        restored = home / 'restored.db'
        restored.write_bytes(archive.read('databases/till_backoffice.db'))
    with sqlite3.connect(restored) as db:
        assert db.execute('pragma integrity_check').fetchall() == [('ok',)]
        assert db.execute('select customer_number,account_balance,package_minutes,is_active from customers order by id').fetchall() == [('1001', 3.5, 47, 1), ('1002', 0, 9, 0)]
        assert db.execute('select * from local_sync_outbox').fetchall() == [('pending-1', 'unsent sale')]


def test_cloud_outage_still_preserves_pi_but_is_not_reported_success(home, monkeypatch):
    def unavailable(root):
        raise TimeoutError('private details must not be logged')
    monkeypatch.setattr(bundle, 'cloud_snapshot', unavailable)
    data, manifest = bundle.create_bundle(home)
    assert manifest['cloud_status'] == 'failed'
    assert manifest['cloud_error_type'] == 'TimeoutError'
    assert 'private details' not in json.dumps(manifest)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.getinfo('databases/till_backoffice.db').file_size > 0
