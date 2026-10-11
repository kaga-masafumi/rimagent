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
    def rpc(self, url, method, params):
        self.calls.append(method)
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
            return {'downed': int(bool(self.options.get('danger'))), 'hostiles': []}
        if method == 'state.alerts':
            return [{'label': 'Food', 'priority': 'High'}] if self.resumed and self.options.get('event') else []
        if method == 'state.letters': return []
        if method == 'map.find':
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

def run(tmp_path, game, **kwargs):
    clock = Clock(game)
    with patch.object(watch.bridge, 'rpc', side_effect=game.rpc), patch.object(watch.time, 'monotonic', side_effect=clock.monotonic), patch.object(watch.time, 'sleep', side_effect=clock.sleep):
        return watch.wait('http://localhost', tmp_path, GOAL, write=True, seconds=3, poll=1, **kwargs)

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
def test_final_observation_must_still_be_paused(tmp_path, precompleted):
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
    result = run(tmp_path, game)
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

@pytest.mark.parametrize('kwargs',[{'write':False},{'write':True,'seconds':61},{'write':True,'speed':4},{'write':True,'poll':0},{'write':True,'seconds':float('nan')}])
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
            'wait','--goal-file',str(goal_path),'--write','--seconds','0.1','--poll','0.01'],
            capture_output=True,text=True,timeout=10)
        assert result.returncode==0,result.stderr
        packet=json.loads(result.stdout)
        assert packet['reason']=='completed' and packet['paused'] and packet['goal_met']
        assert game.paused
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
