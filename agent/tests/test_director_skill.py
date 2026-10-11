"""Direct Codex control policy, with no real game or model requests."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    'director_control', ROOT / 'skills/rimworld-director/scripts/bridge_control.py')
control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(control)


@pytest.mark.parametrize('method,write', [('ui.build', False), ('dev.spawn', True), ('game.new', True)])
def test_unapproved_mutation_never_contacts_bridge(tmp_path, method, write):
    with patch.object(control, 'rpc') as rpc:
        with pytest.raises(ValueError):
            control.execute('http://localhost', tmp_path, method, {}, write)
        rpc.assert_not_called()


def test_unpaused_game_refuses_mutation(tmp_path):
    with patch.object(control, 'rpc', return_value={'state': 'playing', 'paused': False}) as rpc:
        with pytest.raises(RuntimeError, match='Pause'):
            control.execute('http://localhost', tmp_path, 'ui.build', {'at': [1, 2]}, True)
        assert rpc.call_args_list[0].args[1] == 'game.status'
        assert rpc.call_count == 1


def test_controller_lock_prevents_any_rpc(tmp_path):
    with control.lock(tmp_path), patch.object(control, 'rpc') as rpc:
        with pytest.raises(RuntimeError, match='active'):
            control.execute('http://localhost', tmp_path, 'game.pause', {'paused': True}, True)
        rpc.assert_not_called()


def test_timeout_is_journaled_and_not_retried(tmp_path):
    with patch.object(control, 'rpc', side_effect=[{'state': 'playing', 'paused': True}, TimeoutError('timeout')]) as rpc:
        with pytest.raises(TimeoutError):
            control.execute('http://localhost', tmp_path, 'ui.build', {'at': [1, 2]}, True)
        assert rpc.call_count == 2
    rows = [json.loads(line) for line in (tmp_path / 'runs/codex-play.jsonl').read_text().splitlines()]
    assert [row['stage'] for row in rows] == ['intent', 'error']
    assert rows[0]['id'] == rows[1]['id']


def test_live_method_catalog_uses_method_field(monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['bridge_control.py', 'methods', '--filter', 'ui.build'])
    catalog = {'result': [{'method': 'ui.build', 'doc': '{at:[x,z]}'}, {'method': 'game.status', 'doc': ''}]}
    with patch.object(control, 'request', return_value=(json.dumps(catalog).encode(), 'application/json')):
        control.main()
    assert json.loads(capsys.readouterr().out) == [catalog['result'][0]]


def test_repo_discovery_works_outside_personal_install(monkeypatch):
    monkeypatch.delenv('RIMAGENT_ROOT', raising=False)
    monkeypatch.chdir(ROOT / 'agent')
    assert control.find_repo() == ROOT
