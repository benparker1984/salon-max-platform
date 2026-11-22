import importlib.util
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location('groundhog_guard', Path(__file__).resolve().parents[1]/'pi/till-source/native_till_app/groundhog.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def test_owner_access_rechecks_database_role():
    class Repo:
        def __init__(self,role,active=1): self.role,self.active=role,active
        def fetch_one(self,*args): return {'role':self.role,'is_active':self.active}
    assert guard.owner_allowed(Repo('owner'),{'id':1})
    assert not guard.owner_allowed(Repo('manager'),{'id':1,'role':'owner'})
    assert not guard.owner_allowed(Repo('staff'),{'id':1})
    assert not guard.owner_allowed(Repo('owner',0),{'id':1})
    assert not guard.owner_allowed(Repo('owner'),None)


def test_active_work_blocks_recovery():
    assert guard.hardware_idle(SimpleNamespace(runtime_state={}))
    for state in ({'bed_queues':{1:['waiting']}},{'sale_baskets':{1:['course']}},
                  {'pending_sale':{'minutes':5}}, {'bed_states':{1:{'current_phase':'tanning'}}},
                  {'bed_states':{1:{'current_phase':'cooldown'}}}):
        assert not guard.hardware_idle(SimpleNamespace(runtime_state=state))


def test_missing_controller_never_falls_back_to_erasing(tmp_path,monkeypatch):
    monkeypatch.setattr(guard,'HELPER',tmp_path/'not-installed.py')
    result=guard.helper_request({'action':'arm'})
    assert result['ok'] is False
    assert 'not installed' in result['message']
