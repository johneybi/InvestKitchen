from copy import deepcopy
import sys

import pytest

from deploy.gemini import service_resources as resources


def baseline():
    return {unit: {'memory.swappiness': '60', 'memory.soft_limit_in_bytes': '9223372036854771712',
                   'memory.limit_in_bytes': str(160 * 1024 * 1024 if 'mcp' in unit else 256 * 1024 * 1024)}
            for unit in resources.PROFILES}


class Controller:
    def __init__(self, values=None):
        self.values = deepcopy(values or baseline())
        self.writes = []
        self.fail_once = None

    def read(self, unit):
        return deepcopy(self.values[unit])

    def write(self, unit, key, value):
        self.writes.append((unit, key, value))
        self.values[unit][key] = value
        if self.fail_once == (unit, key):
            self.fail_once = None
            raise OSError('synthetic sysfs failure after a write')


def test_scoped_preferences_apply_and_restore_without_changing_hard_limits():
    c = Controller()
    old = baseline()
    resources.transition(c, old)
    for unit in resources.PROFILES:
        assert {key: c.values[unit][key] for key in resources.KEYS} == resources.desired(unit)
        assert c.values[unit]['memory.limit_in_bytes'] == old[unit]['memory.limit_in_bytes']
    resources.transition(c, old, restore=True)
    assert c.values == old
    assert all(unit in resources.PROFILES and key in resources.KEYS for unit, key, _ in c.writes)


def test_repeated_apply_does_not_overwrite_original_preferences():
    c = Controller()
    old = baseline()
    resources.transition(c, old)
    writes = list(c.writes)
    resources.transition(c, old)
    assert c.writes == writes
    resources.transition(c, old, restore=True)
    assert c.values == old


def test_partial_transition_failure_restores_every_touched_value():
    c = Controller()
    c.fail_once = (list(resources.PROFILES)[1], 'memory.soft_limit_in_bytes')
    with pytest.raises(OSError):
        resources.transition(c, baseline())
    assert c.values == baseline()


@pytest.mark.parametrize('field,value', [('memory.swappiness', '10'),
                                       ('memory.limit_in_bytes', '536870912')])
def test_operator_drift_is_checked_before_any_write(field, value):
    c = Controller()
    c.values[list(resources.PROFILES)[1]][field] = value
    with pytest.raises(ValueError):
        resources.transition(c, baseline())
    assert not c.writes


def test_unavailable_target_does_not_partially_apply_preferences():
    c = Controller()
    del c.values[list(resources.PROFILES)[1]]
    with pytest.raises(KeyError):
        resources.transition(c, baseline())
    assert not c.writes


def test_other_services_and_invalid_values_are_not_configurable():
    for value in [{}, {**baseline(), 'unrelated.service': {}},
                  {**baseline(), list(resources.PROFILES)[0]: {'memory.swappiness': '-1'}}]:
        c = Controller()
        with pytest.raises(ValueError):
            resources.transition(c, value)
        assert not c.writes


def test_cli_failure_omits_paths_and_exception_material(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['service_resources', 'inspect'])
    def fail(*args):
        raise RuntimeError('/private/synthetic credential-material')
    monkeypatch.setattr(resources.ResourceController, 'read', fail)
    assert resources.main() == 1
    assert capsys.readouterr().out == 'SCOPED RESOURCE STOPPED\n'


def test_cgroup_controls_do_not_follow_links(tmp_path, monkeypatch):
    c = resources.ResourceController()
    monkeypatch.setattr(c, 'group', lambda unit: tmp_path)
    target = tmp_path/'protected'
    target.write_text('60')
    (tmp_path/'memory.swappiness').symlink_to(target)
    with pytest.raises(ValueError):
        c.write(next(iter(resources.PROFILES)), 'memory.swappiness', '0')
    assert target.read_text() == '60'
