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


def test_mission_defaults_and_restart_persistence(tmp_path):
    assert control.mission_state(tmp_path)['combat_owner'] == 'codex'
    control.mission_command('unused', tmp_path, 'start', 'human')
    control.handoff_combat(tmp_path, {'tick': 10}, {'hostiles': [{'id': 'Enemy'}]})
    with patch.object(control, 'rpc') as rpc:
        for method in ('game.speed', 'game.pause', 'ui.goto', 'ui.attack', 'ui.build', 'ui.dialog'):
            with pytest.raises(RuntimeError, match='Human combat control'):
                control.execute('unused', tmp_path, method, {'speed': 3, 'paused': True}, True)
        with pytest.raises(RuntimeError, match='Human control'):
            control.mission_command('unused', tmp_path, 'start', 'codex')
        with pytest.raises(RuntimeError, match='already active'):
            control.mission_command('unused', tmp_path, 'handoff')
        rpc.assert_not_called()
    with patch.object(control, 'rpc', return_value=[]) as rpc:
        assert control.execute('unused', tmp_path, 'state.pawns', {}) == []
        rpc.assert_called_once()


def test_human_owner_blocks_mutation_on_hostile_detection(tmp_path):
    control.mission_command('unused', tmp_path, 'start', 'human')
    with patch.object(control, 'rpc', side_effect=[{'state': 'playing', 'paused': True, 'tick': 100},
                                                   {'hostiles': [{'id': 'Enemy'}]}]) as rpc:
        with pytest.raises(RuntimeError, match='Combat detected'):
            control.execute('unused', tmp_path, 'ui.goto', {}, True)
        assert [c.args[1] for c in rpc.call_args_list] == ['game.status', 'state.summary']
    assert control.mission_state(tmp_path)['human_control']


def test_handoff_pause_failure_keeps_control_blocked(tmp_path):
    with patch.object(control, 'rpc', side_effect=TimeoutError('pause failed')):
        with pytest.raises(TimeoutError):
            control.mission_command('unused', tmp_path, 'handoff')
    assert control.mission_state(tmp_path)['human_control']


def test_return_requires_human_pause_and_explicit_codex_takeover(tmp_path):
    control.handoff_combat(tmp_path, {'tick': 10}, {})
    with patch.object(control, 'rpc', return_value={'state': 'playing', 'paused': False}):
        with pytest.raises(RuntimeError, match='Human must pause'):
            control.mission_command('unused', tmp_path, 'return')
    assert control.mission_state(tmp_path)['human_control']
    replies = [{'state': 'playing', 'paused': True}, {'hostiles': [{'id': 'Enemy'}]}]
    with patch.object(control, 'rpc', side_effect=replies):
        with pytest.raises(RuntimeError, match='Hostiles remain'):
            control.mission_command('unused', tmp_path, 'return')
    with patch.object(control, 'rpc', side_effect=replies) as rpc:
        result = control.mission_command('unused', tmp_path, 'return', 'codex')
        assert not result['human_control']
        assert result['combat_owner'] == 'codex'
        assert 'game.speed' not in [c.args[1] for c in rpc.call_args_list]
    control.handoff_combat(tmp_path, {'tick': 100}, {})
    with patch.object(control, 'rpc', side_effect=[{'state': 'playing', 'paused': True},
                                                   {'hostiles': []}]) as rpc:
        result = control.mission_command('unused', tmp_path, 'return')
        assert result == {'combat_owner': 'human', 'human_control': False}
        assert 'game.speed' not in [c.args[1] for c in rpc.call_args_list]
