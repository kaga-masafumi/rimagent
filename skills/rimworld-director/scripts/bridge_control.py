"""Local RimBridge control. No LLM calls or retries. Use params files for JSON."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from urllib.request import Request, urlopen
from urllib.parse import urlencode, urlparse
import uuid

READ = {'game.status', 'map.view', 'map.detail', 'map.find', 'map.open_rects',
        'map.power', 'ui.gizmos', 'ui.orders_at', 'anchor.list', 'steward.status',
        'steward.stock.list', 'steward.orders', 'steward.orders.explain', 'steward.explain'}
WRITE = {'game.pause', 'game.speed', 'ui.build', 'ui.build_many', 'ui.designate',
         'ui.zone', 'ui.area', 'ui.set_work', 'ui.set_work_many', 'ui.set_policies', 'ui.bill',
         'ui.add_bill', 'ui.set_research', 'ui.set_schedule', 'ui.storage', 'ui.animal',
         'ui.cancel_job', 'ui.select', 'ui.draft', 'ui.goto', 'ui.attack', 'ui.order',
         'ui.job', 'ui.letter', 'ui.dialog', 'ui.press', 'anchor.set', 'anchor.delete',
         'steward.enable', 'steward.orders.set', 'steward.stock.set', 'steward.stock.add',
         'steward.research', 'steward.posture', 'steward.pawn', 'steward.orders.rally'}


def request(url, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    req = Request(url.rstrip('/') + path, data=data,
                  headers={'Content-Type': 'application/json'} if data else {})
    with urlopen(req, timeout=15) as response:
        return response.read(), response.headers.get('Content-Type', '')


def rpc(url, method, params):
    body, _ = request(url, '/rpc', {'method': method, 'params': params})
    result = json.loads(body)
    if not result.get('ok'):
        raise RuntimeError(result.get('error', 'RPC rejected'))
    return result.get('result')


class ControllerBusy(RuntimeError):
    pass


@contextmanager
def lock(repo):
    runs = repo / 'runs'
    runs.mkdir(parents=True, exist_ok=True)
    with (runs / 'controller.lock').open('a+b') as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ControllerBusy('play/assist or another controller is active; stop it first') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def record(repo, row):
    with (repo / 'runs' / 'codex-play.jsonl').open('a', encoding='utf-8') as out:
        out.write(json.dumps({'time': datetime.now(timezone.utc).isoformat(), **row},
                             ensure_ascii=False) + '\n')
        out.flush()
        os.fsync(out.fileno())


def find_repo():
    if os.environ.get('RIMAGENT_ROOT'):
        return Path(os.environ['RIMAGENT_ROOT']).resolve()
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        for candidate in (start, *start.parents):
            if (candidate / 'agent' / 'rimagent' / 'session_lock.py').is_file():
                return candidate
    return Path.cwd()


def mission_state(repo):
    path = repo / 'runs/director-mission.json'
    if not path.exists():
        return {'combat_owner': 'codex', 'human_control': False}
    state = json.loads(path.read_text(encoding='utf-8'))
    if (state.get('combat_owner') not in ('codex', 'human') or
            type(state.get('human_control')) is not bool):
        raise ValueError('Invalid director mission state; inspect before continuing')
    return state


def save_mission(repo, state):
    path = repo / 'runs/director-mission.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    record(repo, {'stage': 'mission', 'state': state})
    return state


def require_codex_control(repo):
    state = mission_state(repo)
    if state['human_control'] or handoff_requested(repo):
        raise RuntimeError('Human combat control is active; no Codex mutations or waits until mission return')
    return state


def handoff_requested(repo):
    return (repo / 'runs/director-human-request').exists()


def combat_present(summary, letters=()):
    return bool(summary.get('hostiles') or any(
        item.get('def') in ('ThreatBig', 'ThreatSmall') for item in letters))


def handoff_combat(repo, status, summary):
    state = mission_state(repo)
    result = save_mission(repo, {**state, 'combat_owner': 'human', 'human_control': True,
        'handoff': {'seed': status.get('seed'), 'map_size': status.get('map_size'),
                    'tick': status['tick'], 'hostiles': summary.get('hostiles', [])}})
    (repo / 'runs/director-human-request').unlink(missing_ok=True)
    return result


def mission_command(url, repo, action, combat_owner=None):
    if combat_owner not in (None, 'codex', 'human'):
        raise ValueError('Combat owner must be codex or human')
    if action == 'status':
        return {**mission_state(repo), 'handoff_pending': handoff_requested(repo)}
    if action == 'handoff':
        if mission_state(repo)['human_control']:
            raise RuntimeError('Human control is already active; do not interrupt the human')
        marker = repo / 'runs/director-human-request'
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        try:
            with lock(repo):
                state = mission_state(repo)
                save_mission(repo, {**state, 'combat_owner': 'human', 'human_control': True})
                marker.unlink(missing_ok=True)
                rpc(url, 'game.pause', {'paused': True})
                status = rpc(url, 'game.status', {})
                if status.get('state') != 'playing' or not status.get('paused'):
                    raise RuntimeError('Handoff pause was not confirmed; pause manually')
                return handoff_combat(repo, status, rpc(url, 'state.summary', {}))
        except ControllerBusy:
            return {'handoff_pending': True, 'human_control': False,
                    'message': 'Wait will pause at its next poll; await confirmed human_handoff. If play/assist holds the lock, stop it and retry handoff.'}
    with lock(repo):
        state = mission_state(repo)
        if action == 'start' and (state['human_control'] or handoff_requested(repo)):
            raise RuntimeError('Human control is active; use mission return explicitly')
        if action == 'start':
            # The caller must ask the user at mission start; omitted choice uses Codex.
            return save_mission(repo, {'combat_owner': combat_owner or 'codex',
                                      'human_control': False})
        if action != 'return':
            raise ValueError('Unknown mission action')
        status = rpc(url, 'game.status', {})
        if status.get('state') != 'playing' or not status.get('paused'):
            raise RuntimeError('Human must pause the game before returning control')
        summary = rpc(url, 'state.summary', {})
        if combat_present(summary) and combat_owner != 'codex':
            raise RuntimeError('Hostiles remain; finish combat or explicitly return with --combat-owner codex')
        result = save_mission(repo, {'combat_owner': combat_owner or state['combat_owner'],
                                   'human_control': False})
        (repo / 'runs/director-human-request').unlink(missing_ok=True)
        return result


def execute(url, repo, method, params, write=False):
    if method in READ or method.startswith(('state.', 'defs.')):
        return rpc(url, method, params)
    if not write or method not in WRITE:
        raise ValueError('Mutation requires --write and an allowed ordinary-play RPC')
    if method == 'game.speed' and (type(params.get('speed')) is not int or params['speed'] not in (0, 1, 2, 3)):
        raise ValueError('Only ordinary speeds 0..3 are allowed')
    if method == 'game.pause' and params.get('paused') is not True:
        raise ValueError('Use game.speed to resume; game.pause only accepts paused=true')
    with lock(repo):
        mission = require_codex_control(repo)
        status = rpc(url, 'game.status', {})
        if status.get('state') != 'playing':
            raise RuntimeError('Load a colony first')
        if method != 'game.pause' and not status.get('paused'):
            raise RuntimeError('Pause and verify the game before changing it')
        if mission['combat_owner'] == 'human' and method != 'game.pause':
            summary = rpc(url, 'state.summary', {})
            if combat_present(summary):
                handoff_combat(repo, status, summary)
                raise RuntimeError('Combat detected; human control activated, mutation refused')
            if method == 'ui.attack' or (method == 'ui.draft' and params.get('drafted')):
                raise RuntimeError('Combat belongs to the human for this mission')
        operation = str(uuid.uuid4())
        record(repo, {'id': operation, 'stage': 'intent', 'method': method, 'params': params})
        try:
            result = rpc(url, method, params)
        except Exception as exc:
            record(repo, {'id': operation, 'stage': 'error', 'error': str(exc),
                         'outcome': 'unverified; inspect before any retry'})
            raise
        record(repo, {'id': operation, 'stage': 'response', 'result': result})
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8765')
    parser.add_argument('--repo', type=Path, default=find_repo())
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('health')
    mission = commands.add_parser('mission')
    mission.add_argument('action', choices=('start', 'status', 'handoff', 'return'))
    mission.add_argument('--combat-owner', choices=('codex', 'human'))
    methods = commands.add_parser('methods')
    methods.add_argument('--filter', default='')
    call = commands.add_parser('rpc')
    call.add_argument('method')
    call.add_argument('--params-file', type=Path)
    call.add_argument('--write', action='store_true')
    picture = commands.add_parser('screenshot')
    picture.add_argument('--output', type=Path, required=True)
    picture.add_argument('--x', type=int)
    picture.add_argument('--z', type=int)
    picture.add_argument('--w', type=float, default=60)
    args = parser.parse_args()
    parsed = urlparse(args.url)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
        parser.error('Only a loopback HTTP bridge is supported')
    if args.command == 'mission':
        if not (args.repo / 'agent' / 'rimagent' / 'session_lock.py').is_file():
            raise ValueError('Use --repo to select the rimagent checkout')
        result = mission_command(args.url, args.repo, args.action, args.combat_owner)
    elif args.command == 'rpc':
        params = json.loads(args.params_file.read_text(encoding='utf-8-sig')) if args.params_file else {}
        if not isinstance(params, dict):
            raise ValueError('Parameters must be a JSON object')
        if args.write and not (args.repo / 'agent' / 'rimagent' / 'session_lock.py').is_file():
            raise ValueError('Use --repo to select the rimagent checkout with its controller lock')
        result = execute(args.url, args.repo, args.method, params, args.write)
    elif args.command == 'screenshot':
        query = {key: getattr(args, key) for key in ('x', 'z', 'w') if getattr(args, key) is not None}
        body, content_type = request(args.url, '/screenshot?' + urlencode(query))
        if not content_type.startswith('image/'):
            raise RuntimeError(body.decode('utf-8', errors='replace')[:500])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(body)
        result = {'image': str(args.output.resolve())}
    else:
        body, _ = request(args.url, '/' + args.command)
        result = json.loads(body)
        if args.command == 'methods':
            result = [item for item in result['result'] if args.filter in item.get('method', item.get('name', ''))]
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'error': str(error), 'retried': False}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
