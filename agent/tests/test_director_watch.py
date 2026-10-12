"""Real locks and fake game/clock: no game or model calls."""
from pathlib import Path
import sys
from unittest.mock import patch
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'skills/rimworld-director/scripts'))
import bridge_watch as watch
GOAL = {'kind': 'building_count', 'def': 'Bed', 'at_least': 1}

class Game:
    def __init__(self, **options):
        self.options = options
        self.paused = True
        self.resumed = False
        self.tick = 100
        self.calls = []
        self.requests = []
    def rpc(self, url, method, params):
        self.calls.append(method)
        self.requests.append((method, params))
        if method == 'game.speed':
            self.resumed, self.paused = True, False
            if self.options.get('fail_resume'): raise TimeoutError('resume timeout')
            return {'paused': False}
        if method == 'game.pause':
            if self.options.get('fail_pause'): raise TimeoutError('pause timeout')
            self.paused = True
            return {'paused': True}
        if method == 'game.status':
            return {'state': 'playing', 'paused': self.paused, 'tick': self.tick, 'seq': self.tick,
                    'seed': 'test', 'map_size': [250,250]}
        if method == 'state.summary':
            if self.resumed and self.options.get('fail_read'): raise TimeoutError('read timeout')
            return {'downed': int(bool(self.options.get('danger'))), 'hostiles': [], 'home_center': [100, 100]}
        if method == 'state.alerts':
            return [{'label': 'Food', 'priority': 'High'}] if self.resumed and self.options.get('event') else []
        if method == 'state.letters': return []
        if method == 'map.find':
            if params['def'] == 'Fire':
                return {'things': [{'id': 'Fire1'}] if self.resumed and self.options.get('fire') else []}
            built = self.options.get('precompleted') or (self.resumed and self.options.get('complete'))
            return {'count': 100, 'things': [{'id':'Bed1'}] if built else []}
        if method == 'state.stocks': return {'counted': {'WoodLog': 50}}
        raise AssertionError(method)

class Clock:
    def __init__(self, game): self.game, self.elapsed = game, 0
    def monotonic(self): return self.elapsed
    def sleep(self, duration):
        self.elapsed += duration
        self.game.tick += -1 if self.game.options.get('rollback') else 100


class CombatGame(Game):
    def rpc(self, url, method, params):
        result = super().rpc(url, method, params)
        if method == 'state.summary' and (self.resumed or self.options.get('existing_hostile')):
            result['hostiles'] = [{'id': 'Enemy', 'pos': [180, 180]}]
        return result


@pytest.mark.parametrize('existing', [False, True])
def test_human_handoff_cannot_be_bypassed_by_acknowledgement(tmp_path, existing):
    watch.bridge.mission_command('unused', tmp_path, 'start', 'human')
    game = CombatGame(existing_hostile=existing)
    result = run(tmp_path, game, acknowledged_hostiles=['Enemy'] if existing else [])
    assert result['reason'] == 'human_handoff'
    assert result['paused'] and result['human_control']
    assert game.resumed is (not existing)
    assert watch.bridge.mission_state(tmp_path)['human_control']
    with patch.object(watch.bridge, 'rpc') as rpc:
        with pytest.raises(RuntimeError, match='Human combat control'):
            watch.wait('unused', tmp_path, GOAL, write=True)
        rpc.assert_not_called()


def test_codex_default_retains_supervised_combat(tmp_path):
    game = CombatGame(existing_hostile=True)
    result = run(tmp_path, game, acknowledged_hostiles=['Enemy'])
    assert result['reason'] == 'timeout'
    assert result['combat_owner'] == 'codex'
    assert not result['human_control']


def test_human_handoff_on_new_threat_letter_without_visible_enemy(tmp_path):
    class LetterGame(Game):
        def rpc(self, url, method, params):
            result = super().rpc(url, method, params)
            if method == 'state.letters' and self.resumed:
                return [{'id': 1, 'def': 'ThreatBig', 'target': 'Enemy'}]
            return result
    watch.bridge.mission_command('unused', tmp_path, 'start', 'human')
    result = run(tmp_path, LetterGame())
    assert result['reason'] == 'human_handoff'
    assert watch.bridge.mission_state(tmp_path)['human_control']


def test_handoff_remains_latched_when_pause_fails(tmp_path):
    watch.bridge.mission_command('unused', tmp_path, 'start', 'human')
    result = run(tmp_path, CombatGame(existing_hostile=True, fail_pause=True))
    assert result['reason'] == 'error' and not result['paused']
    assert result['human_control']
    assert watch.bridge.mission_state(tmp_path)['human_control']


def test_handoff_request_interrupts_active_routine_wait(tmp_path):
    game = Game()
    clock = Clock(game)
    def request_during_sleep(duration):
        clock.sleep(duration)
        pending = watch.bridge.mission_command('unused', tmp_path, 'handoff')
        assert pending['handoff_pending'] and not pending['human_control']
    with patch.object(watch.bridge, 'rpc', side_effect=game.rpc), \
            patch.object(watch.time, 'monotonic', side_effect=clock.monotonic), \
            patch.object(watch.time, 'sleep', side_effect=request_during_sleep):
        result = watch.wait('unused', tmp_path, GOAL, write=True, mode='routine')
    assert result['reason'] == 'human_handoff'
    assert result['elapsed_wall_seconds'] == 2
    assert result['combat_owner'] == 'human'
    assert game.calls.count('game.speed') == 1
    assert game.calls.count('game.pause') == 1
    assert watch.bridge.mission_state(tmp_path)['combat_owner'] == 'human'
    assert not watch.bridge.handoff_requested(tmp_path)

def run(tmp_path, game, *, mode='emergency', seconds=3, poll=1, **kwargs):
    clock = Clock(game)
    with patch.object(watch.bridge, 'rpc', side_effect=game.rpc), patch.object(watch.time, 'monotonic', side_effect=clock.monotonic), patch.object(watch.time, 'sleep', side_effect=clock.sleep):
        return watch.wait('http://localhost', tmp_path, GOAL, write=True,
                          seconds=seconds, poll=poll, mode=mode, **kwargs)

@pytest.mark.parametrize('options,reason', [({'complete': True},'completed'), ({'event':True},'event'), ({'danger':True},'danger'), ({},'timeout'), ({'rollback':True},'error'), ({'fail_resume':True},'error'), ({'fail_pause':True},'error'), ({'fail_read':True},'error')])
def test_wait_exits_and_attempts_pause(tmp_path, options, reason):
    game = Game(**options)
    result = run(tmp_path, game)
    assert result['reason'] == reason
    assert game.calls.count('game.pause') == 1
    assert game.calls.count('game.speed') <= 1
    assert result['paused'] is (not options.get('fail_pause',False) and not options.get('rollback',False))
    if reason == 'error': assert result['goal_met'] is False

def test_precompleted_never_resumes(tmp_path):
    game = Game(precompleted=True)
    assert run(tmp_path,game)['goal_met']
    assert 'game.speed' not in game.calls

@pytest.mark.parametrize('precompleted', [False, True])
@pytest.mark.parametrize('mode', ['routine', 'maintenance', 'emergency'])
def test_final_observation_must_still_be_paused(tmp_path, precompleted, mode):
    class ResumedDuringFinalRead(Game):
        def __init__(self):
            super().__init__(complete=True, precompleted=precompleted)
            self.final_pause = False
            self.status_after_pause = 0
        def rpc(self, url, method, params):
            if method == 'game.pause':
                self.final_pause = True
            if method == 'game.status' and self.final_pause:
                self.status_after_pause += 1
                if self.status_after_pause == 2:
                    self.paused = False  # Human/mod resumes after the first pause verification.
            return super().rpc(url, method, params)
    game = ResumedDuringFinalRead()
    result = run(tmp_path, game, mode=mode)
    assert result['reason'] == 'error'
    assert result['paused'] is False
    assert result['goal_met'] is False
    assert result['progress']['current'] == 1
    assert 'resumed during final observation' in result['pause_error']
    assert game.calls.count('game.pause') == 1
    assert game.calls.count('game.speed') == (0 if precompleted else 1)

def test_limits(tmp_path):
    assert run(tmp_path,Game(),stall_ticks=100)['reason']=='stalled'
    assert run(tmp_path,Game(),max_ticks=100)['reason']=='game_time_limit'

def test_event_wins_over_completion(tmp_path):
    result=run(tmp_path,Game(complete=True,event=True))
    assert result['reason']=='event' and result['goal_met']

def test_lock_blocks_all_calls(tmp_path):
    with watch.bridge.lock(tmp_path), patch.object(watch.bridge,'rpc') as rpc:
        with pytest.raises(RuntimeError,match='active'): watch.wait('http://localhost',tmp_path,GOAL,write=True)
        rpc.assert_not_called()

@pytest.mark.parametrize('kwargs',[{'write':False},{'write':True,'seconds':301},{'write':True,'speed':4},{'write':True,'poll':0},{'write':True,'seconds':float('nan')}])
def test_invalid_options_do_not_contact_game(tmp_path,kwargs):
    with patch.object(watch.bridge,'rpc') as rpc:
        with pytest.raises(ValueError): watch.wait('http://localhost',tmp_path,GOAL,**kwargs)
        rpc.assert_not_called()

def test_deltas_and_no_repeat():
    a={'alerts':[{'label':'Food','priority':'Low'},{'label':'Cold'}],'letters':[{'id':1}]}
    b={'alerts':[{'label':'Food','priority':'High'}],'letters':[{'id':1},{'id':2}]}
    delta=watch.changes(a,b)
    assert [x['label'] for x in delta['worsened_alerts']]==['Food']
    assert [x['label'] for x in delta['resolved_alerts']]==['Cold']
    assert delta['new_letters']==[{'id':2}]
    assert not any(watch.changes(b,b).values())

def test_filtered_count_and_stock():
    with patch.object(watch.bridge,'rpc',side_effect=Game().rpc):
        assert watch.observe('http://localhost',GOAL)['value']==0
        assert watch.observe('http://localhost',{'kind':'stock_count','def':'WoodLog','at_least':100})['value']==50

def test_routine_runs_full_interval_despite_progress_and_normal_alerts(tmp_path):
    game = Game(complete=True, event=True)
    result = run(tmp_path, game, mode='routine', seconds=300, poll=2)
    assert result['reason'] == 'completed'
    assert result['goal_met'] and result['paused']
    assert result['elapsed_wall_seconds'] == 300
    assert result['new_alerts'][0]['label'] == 'Food'
    assert game.calls.count('game.speed') == 1
    assert game.calls.count('game.pause') == 1
    assert sum(method == 'map.find' and params['def'] == 'Bed'
               for method, params in game.requests) == 2  # Only baseline and final measure progress.

def test_routine_default_does_not_stop_at_old_tick_limit(tmp_path):
    game = Game()
    clock = Clock(game)
    with patch.object(watch.bridge, 'rpc', side_effect=game.rpc), patch.object(watch.time, 'monotonic', side_effect=clock.monotonic), patch.object(watch.time, 'sleep', side_effect=clock.sleep):
        result = watch.wait('http://localhost', tmp_path, GOAL, write=True)
    assert result['reason'] == 'routine_check'
    assert result['elapsed_wall_seconds'] == 300
    assert result['elapsed_game_ticks'] == 15000

@pytest.mark.parametrize('threat', ['hostile', 'critical', 'letter', 'high_danger'])
def test_routine_interrupts_for_emergencies(tmp_path, threat):
    class Threat(Game):
        def rpc(self, url, method, params):
            result = super().rpc(url, method, params)
            if self.resumed:
                if method == 'state.summary' and threat == 'hostile': result['hostiles'] = [{'id':'Raider'}]
                if method == 'state.summary' and threat == 'high_danger': result['danger'] = 'High'
                if method == 'state.alerts' and threat == 'critical': result = [{'label':'Emergency','priority':'Critical'}]
                if method == 'state.letters' and threat == 'letter': result = [{'id':1,'def':'ThreatBig'}]
            return result
    game = Threat()
    result = run(tmp_path, game, mode='routine', seconds=300, poll=2)
    assert result['reason'] == 'danger' and result['paused']
    assert result['elapsed_wall_seconds'] == 2

def test_emergency_mode_checks_each_second(tmp_path):
    class Delayed(Game):
        def rpc(self, url, method, params):
            result = super().rpc(url, method, params)
            if method == 'state.summary' and self.tick >= 200: result['downed'] = 1
            return result
    game = Delayed()
    clock = Clock(game)
    with patch.object(watch.bridge, 'rpc', side_effect=game.rpc), patch.object(watch.time, 'monotonic', side_effect=clock.monotonic), patch.object(watch.time, 'sleep', side_effect=clock.sleep):
        result = watch.wait('http://localhost', tmp_path, GOAL, write=True, mode='emergency')
    assert result['reason'] == 'danger' and result['elapsed_wall_seconds'] == 1
    assert ('game.speed', {'speed': 1}) in game.requests


@pytest.mark.parametrize('label', ['Needs tending', 'Breakdown'])
def test_maintenance_batches_care_and_repairs_at_full_speed(tmp_path, label):
    class Maintenance(Game):
        def rpc(self, url, method, params):
            result = super().rpc(url, method, params)
            if method == 'state.alerts' and self.resumed:
                return [{'label': label, 'priority': 'High'}]
            return result
    game = Maintenance()
    clock = Clock(game)
    with patch.object(watch.bridge, 'rpc', side_effect=game.rpc), patch.object(watch.time, 'monotonic', side_effect=clock.monotonic), patch.object(watch.time, 'sleep', side_effect=clock.sleep):
        result = watch.wait('http://localhost', tmp_path, GOAL, write=True, mode='maintenance')
    assert result['reason'] == 'maintenance_check' and result['paused']
    assert result['elapsed_wall_seconds'] == 60
    assert ('game.speed', {'speed': 3}) in game.requests
    assert game.calls.count('game.pause') == 1
    assert result['observed_events']['new_alerts'][0]['label'] == label


@pytest.mark.parametrize('mode', ['routine', 'maintenance', 'emergency'])
def test_small_fire_interrupts_every_mode(tmp_path, mode):
    result = run(tmp_path, Game(fire=True), mode=mode, seconds=300, poll=2)
    assert result['reason'] == 'danger' and result['paused']
    assert result['summary']['fires'] == 1
    assert result['elapsed_wall_seconds'] == 2


def test_acknowledged_fire_can_be_extinguished_but_new_fire_interrupts(tmp_path):
    class FireHandling(Game):
        def rpc(self, url, method, params):
            result = super().rpc(url, method, params)
            if method == 'map.find' and params['def'] == 'Fire':
                return {'things': ([{'id': 'Fire1'}] if self.options.get('fire') else []) +
                        ([{'id': 'Fire2'}] if self.resumed and self.options.get('new_fire') else [])}
            return result
    game = FireHandling()
    baseline = game.rpc('', 'map.find', {'def': 'Fire', 'limit': 50})
    assert baseline == {'things': []}
    # The known fire exists before this supervised response window.
    game.options['fire'] = True
    game.resumed = True
    with patch.object(watch.bridge, 'rpc', side_effect=game.rpc):
        current = watch.observe('http://localhost', GOAL)
    assert current['summary']['fire_ids'] == ['Fire1']
    result = run(tmp_path, FireHandling(fire=True), mode='emergency', seconds=3,
                 acknowledged_fires=['Fire1'])
    assert result['reason'] == 'timeout'
    result = run(tmp_path, FireHandling(fire=True, new_fire=True), mode='emergency', seconds=10,
                 acknowledged_fires=['Fire1'])
    assert result['reason'] == 'danger' and result['elapsed_wall_seconds'] == 1


def test_known_fire_alert_does_not_block_extinguishing():
    current = {'summary': {'fires': 1, 'fire_ids': ['Fire1'], 'hostiles': [], 'home_center': [100, 100]},
               'alerts': [{'label': '火災発生!', 'priority': 'Critical'}], 'value': None}
    delta = {'new_alerts': [], 'worsened_alerts': [], 'new_letters': []}
    assert watch.reason_for(current, delta, GOAL, mode='emergency',
                           acknowledged_fires=['Fire1']) is None
    assert watch.reason_for(current, delta, GOAL, mode='emergency') == 'danger'


def test_supervised_emergency_ignores_ordinary_alerts_but_keeps_critical_stop(tmp_path):
    class Event(Game):
        def rpc(self, url, method, params):
            result = super().rpc(url, method, params)
            if method == 'state.summary': result['hostiles'] = [{'id':'Iguana','pos':[130,130]}]
            if method == 'state.alerts' and self.resumed:
                return [{'label':'Mood risk','priority':self.options.get('priority','Medium')}]
            return result
    ordinary = run(tmp_path, Event(priority='Medium'), mode='emergency', seconds=3, poll=1,
                   acknowledged_hostiles=['Iguana'])
    assert ordinary['reason'] == 'timeout' and ordinary['elapsed_wall_seconds'] == 3
    critical = run(tmp_path, Event(priority='Critical'), mode='emergency', seconds=10, poll=1,
                    acknowledged_hostiles=['Iguana'])
    assert critical['reason'] == 'danger' and critical['elapsed_wall_seconds'] == 1


@pytest.mark.parametrize('threat', ['hostile', 'downed'])
def test_maintenance_does_not_delay_new_emergencies(tmp_path, threat):
    class Threat(Game):
        def rpc(self, url, method, params):
            result = super().rpc(url, method, params)
            if method == 'state.summary' and self.resumed:
                result.update({'hostiles': [{'id': 'Raider'}]} if threat == 'hostile' else {'downed': 1})
            return result
    result = run(tmp_path, Threat(), mode='maintenance', seconds=60, poll=2)
    assert result['reason'] == 'danger' and result['paused']
    assert result['elapsed_wall_seconds'] == 2

def test_routine_remembers_a_normal_alert_that_resolved_between_reviews(tmp_path):
    class Transient(Game):
        def rpc(self, url, method, params):
            result = super().rpc(url, method, params)
            if method == 'state.alerts' and self.tick == 200:
                return [{'label': 'Idle', 'priority': 'Medium'}]
            return result
    result = run(tmp_path, Transient(), mode='routine', seconds=6, poll=2)
    assert result['reason'] == 'routine_check' and result['paused']
    assert result['new_alerts'] == []
    assert result['observed_events']['new_alerts'][0]['label'] == 'Idle'
    assert result['observed_events']['resolved_alerts'][0]['label'] == 'Idle'

@pytest.mark.parametrize('outcome,expected', [('cleared','threat_cleared'),('new_enemy','danger'),('downed','danger'),('unrelated_high','danger')])
def test_acknowledged_threat_can_be_handled_without_ignoring_new_danger(tmp_path, outcome, expected):
    class Combat(Game):
        def rpc(self, url, method, params):
            result = super().rpc(url, method, params)
            if method == 'state.summary':
                result['hostiles'] = [{'id':'Iguana'}]
                if self.resumed:
                    if outcome == 'cleared': result['hostiles'] = []
                    if outcome == 'new_enemy': result['hostiles'].append({'id':'Raider'})
                    if outcome == 'downed': result['downed'] = 1
                    if outcome == 'unrelated_high':
                        result['hostiles'] = []
                        result['danger'] = 'High'
            return result
    game = Combat()
    result = run(tmp_path, game, acknowledged_hostiles=['Iguana'])
    assert result['reason'] == expected and result['paused']
    assert game.calls.count('game.speed') == 1
    assert result['acknowledged_hostiles'] == ['Iguana']

def test_known_distant_hostile_can_be_monitored_in_routine(tmp_path):
    class Distant(Game):
        def rpc(self, url, method, params):
            result = super().rpc(url, method, params)
            if method == 'state.summary': result['hostiles'] = [{'id':'Iguana','pos':[230,20]}]
            return result
    result = run(tmp_path, Distant(), mode='routine', seconds=6, poll=2,
                 acknowledged_hostiles=['Iguana'])
    assert result['reason'] == 'routine_check' and result['elapsed_wall_seconds'] == 6


def test_acknowledged_hostile_triggers_emergency_inside_45_cells(tmp_path):
    class Approaching(Game):
        def rpc(self, url, method, params):
            result = super().rpc(url, method, params)
            if method == 'state.summary':
                result['hostiles'] = [{'id':'Iguana','pos':[130,130] if self.tick >= 200 else [230,20]}]
            return result
    result = run(tmp_path, Approaching(), mode='routine', seconds=300, poll=2,
                 acknowledged_hostiles=['Iguana'])
    assert result['reason'] == 'danger' and result['elapsed_wall_seconds'] == 2


def test_close_acknowledged_hostile_can_run_emergency_ticks(tmp_path):
    class CloseHostile(Game):
        def rpc(self, url, method, params):
            result = super().rpc(url, method, params)
            if method == 'state.summary': result['hostiles'] = [{'id':'Iguana','pos':[130,130]}]
            return result
    result = run(tmp_path, CloseHostile(), mode='emergency', seconds=6, poll=1,
                 acknowledged_hostiles=['Iguana'])
    assert result['reason'] == 'timeout' and result['elapsed_wall_seconds'] == 6

def test_acknowledged_downed_pawn_can_be_rescued_but_new_down_stops():
    goal = GOAL
    current = {'summary': {'downed': 1, 'downed_ids': ['Sapphire'], 'hostiles': [],
                           'home_center': [100, 100]},
               'alerts': [], 'value': None}
    delta = {'new_alerts': [], 'worsened_alerts': [], 'new_letters': []}
    assert watch.reason_for(current, delta, goal, acknowledged_downed=['Sapphire']) is None
    current['summary']['downed_ids'].append('Mila')
    current['summary']['downed'] = 2
    assert watch.reason_for(current, delta, goal, acknowledged_downed=['Sapphire']) == 'danger'


def test_routine_acknowledgement_does_not_ignore_new_hostile(tmp_path):
    class NewEnemy(Game):
        def rpc(self, url, method, params):
            result = super().rpc(url, method, params)
            if method == 'state.summary':
                result['hostiles'] = [{'id':'Iguana','pos':[230,20]}]
                if self.resumed: result['hostiles'].append({'id':'Raider','pos':[240,20]})
            return result
    result = run(tmp_path, NewEnemy(), mode='routine', seconds=300, poll=2,
                 acknowledged_hostiles=['Iguana'])
    assert result['reason'] == 'danger' and result['elapsed_wall_seconds'] == 2

@pytest.mark.parametrize('goal',[
    {'kind':'python','def':'Bed','at_least':1},
    {'kind':'building_count','def':'Bed','at_least':501},
    {'kind':'stock_count','def':'WoodLog','at_least':True},
    {'kind':'stock_count','def':'WoodLog','at_least':1,'code':'anything'},
])
def test_invalid_goals_never_contact_game(goal):
    with patch.object(watch.bridge,'rpc') as rpc:
        with pytest.raises(ValueError): watch.observe('http://localhost',goal)
        rpc.assert_not_called()


def test_cli_http_roundtrip(tmp_path):
    """Exercise the actual CLI and JSON RPC over loopback HTTP."""
    import json
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    game = Game(complete=True)
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            body=json.dumps({'ok':True,'result':game.rpc('',payload['method'],payload['params'])}).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    root=tmp_path/'agent/rimagent'
    root.mkdir(parents=True)
    (root/'session_lock.py').touch()
    goal_path=tmp_path/'goal.json'
    goal_path.write_text(json.dumps(GOAL))
    try:
        result=subprocess.run([sys.executable,str(Path(watch.__file__)),
            '--url',f'http://127.0.0.1:{server.server_port}','--repo',str(tmp_path),
            'wait','--goal-file',str(goal_path),'--write','--mode','emergency','--seconds','0.1','--poll','0.01'],
            capture_output=True,text=True,timeout=10)
        assert result.returncode==0,result.stderr
        packet=json.loads(result.stdout)
        assert packet['reason']=='completed' and packet['paused'] and packet['goal_met']
        assert game.paused
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
