"""Bounded observation/wait for Codex. Standard library only; no model calls."""
import argparse
import json
import math
from pathlib import Path
import sys
import time
from urllib.parse import urlparse

import bridge_control as bridge


def validate(goal):
    if not isinstance(goal, dict) or set(goal) != {'kind', 'def', 'at_least'}:
        raise ValueError('Goal requires exactly kind, def, at_least')
    if goal['kind'] not in ('building_count', 'stock_count'):
        raise ValueError('Supported goals: building_count, stock_count')
    if not isinstance(goal['def'], str) or not goal['def'].strip():
        raise ValueError('Use an observed defName')
    if type(goal['at_least']) is not int or goal['at_least'] < 1:
        raise ValueError('at_least must be a positive integer')
    if goal['kind'] == 'building_count' and goal['at_least'] > 500:
        raise ValueError('Building target exceeds map.find limit (500)')


def observe(url, goal, *, measure_progress=True):
    validate(goal)
    before = bridge.rpc(url, 'game.status', {})
    if before.get('state') != 'playing':
        raise RuntimeError('Load a colony first')
    summary = bridge.rpc(url, 'state.summary', {})
    alerts = bridge.rpc(url, 'state.alerts', {})
    letters = bridge.rpc(url, 'state.letters', {})
    if not isinstance(alerts, list) or any('error' in a for a in alerts):
        raise RuntimeError('Alert observation failed')
    # Fire is an actual burning thing, unlike a campfire building. Do not
    # wait for the game danger rating to rise before detecting a small fire.
    fires = bridge.rpc(url, 'map.find', {'def': 'Fire', 'limit': 1})
    summary = {**summary, 'fires': len(fires['things'])}
    value = None
    if measure_progress and goal['kind'] == 'building_count':
        found = bridge.rpc(url, 'map.find', {
            'def': goal['def'], 'faction': 'player', 'limit': 500})
        # Exact built defName excludes Blueprint_/Frame_ defs; count returned
        # things rather than the pre-filter count in the bridge response.
        value = len(found['things'])
    elif measure_progress:
        stocks = bridge.rpc(url, 'state.stocks', {})
        value = stocks['counted'].get(goal['def'], 0)
    after = bridge.rpc(url, 'game.status', {})
    check_session(before, after)
    return {'status': after, 'value': value, 'alerts': alerts, 'letters': letters,
            'summary': {key: summary[key] for key in
                        ('downed', 'hostiles', 'fires', 'danger', 'home_center', 'food_days', 'key_stocks', 'blueprints', 'frames')
                        if key in summary}}


def check_session(previous, current):
    if current.get('state') != 'playing':
        raise RuntimeError('Colony is no longer playing')
    for key in ('seed', 'map_size'):
        if current.get(key) != previous.get(key):
            raise RuntimeError('Colony/map changed')
    for key in ('tick', 'seq'):
        if current[key] < previous[key]:
            raise RuntimeError('Game or event sequence moved backwards')


def changes(previous, current):
    old = {a['label']: a for a in previous['alerts']}
    new = {a['label']: a for a in current['alerts']}
    ranks = {'Low': 0, 'Medium': 1, 'High': 2, 'Critical': 3}
    old_letters = {letter['id'] for letter in previous['letters']}
    return {
        'new_alerts': [a for label, a in new.items() if label not in old],
        'worsened_alerts': [a for label, a in new.items() if label in old and
                            ranks.get(a.get('priority'), -1) >
                            ranks.get(old[label].get('priority'), -1)],
        'resolved_alerts': [a for label, a in old.items() if label not in new],
        'new_letters': [a for a in current['letters'] if a['id'] not in old_letters],
    }


def reason_for(current, delta, goal, *, mode='emergency', allow_completion=True,
               acknowledged_hostiles=()):
    hostiles = current['summary'].get('hostiles', [])
    home = current['summary'].get('home_center')
    if (current['summary'].get('fires', 0) or current['summary'].get('downed', 0) or
            any(h.get('id') not in acknowledged_hostiles for h in hostiles) or
            (home and any(h.get('id') in acknowledged_hostiles and h.get('pos') and
                          math.dist(home, h['pos']) <= 45 for h in hostiles)) or
            (current['summary'].get('danger') == 'High' and
             not any(h.get('id') in acknowledged_hostiles for h in hostiles))):
        return 'danger'
    if any(a.get('priority') == 'Critical' for a in current['alerts']):
        return 'danger'
    if any(letter.get('def') == 'Death' or
           (letter.get('def') in ('ThreatBig', 'ThreatSmall') and
            letter.get('target') not in acknowledged_hostiles)
           for letter in delta['new_letters']):
        return 'danger'
    if acknowledged_hostiles and not any(h.get('id') in acknowledged_hostiles for h in hostiles):
        return 'threat_cleared'
    if mode == 'emergency' and (delta['new_alerts'] or delta['worsened_alerts'] or delta['new_letters']):
        return 'event'
    if allow_completion and current['value'] is not None and current['value'] >= goal['at_least']:
        return 'completed'
    return None


def wait(url, repo, goal, *, write=False, seconds=None, poll=None, speed=None,
         max_ticks=None, stall_ticks=None, mode='routine', acknowledged_hostiles=()):
    validate(goal)
    if mode not in ('routine', 'maintenance', 'emergency'):
        raise ValueError('Mode must be routine, maintenance or emergency')
    if (not isinstance(acknowledged_hostiles, (list, tuple)) or
            any(not isinstance(x, str) or not x.strip() for x in acknowledged_hostiles) or
            len(acknowledged_hostiles) > 50 or
            len(set(acknowledged_hostiles)) != len(acknowledged_hostiles)):
        raise ValueError('Known hostile IDs must be unique nonempty strings (maximum 50)')
    seconds = {'routine': 300, 'maintenance': 60, 'emergency': 30}[mode] if seconds is None else seconds
    poll = (1 if mode == 'emergency' else 2) if poll is None else poll
    speed = (1 if mode == 'emergency' else 3) if speed is None else speed
    if not write:
        raise ValueError('Wait resumes/pauses the game and requires --write')
    if type(speed) is not int or speed not in (1, 2, 3):
        raise ValueError('Only ordinary speeds 1..3 are allowed')
    if not all(math.isfinite(x) for x in (seconds, poll)) or not 0 < poll <= seconds <= 300:
        raise ValueError('Require 0 < poll <= seconds <= 300')
    if any(x is not None and (type(x) is not int or x < 1) for x in (max_ticks, stall_ticks)):
        raise ValueError('Tick limits must be positive integers')
    # Hold the same controller lock for the entire resume/observe/pause interval.
    with bridge.lock(repo):
        status = bridge.rpc(url, 'game.status', {})
        if status.get('state') != 'playing' or not status.get('paused'):
            raise RuntimeError('Pause and verify a loaded colony before waiting')
        baseline = current = observe(url, goal)
        reason = reason_for(current, changes(current, current), goal, mode=mode,
                            acknowledged_hostiles=acknowledged_hostiles)
        bridge.record(repo, {'stage': 'wait_intent', 'goal': goal, 'seconds': seconds,
                             'speed': speed, 'mode': mode, 'poll': poll, 'baseline': baseline,
                             'acknowledged_hostiles': list(acknowledged_hostiles)})
        error = None
        pause_error = None
        started = time.monotonic()
        last_progress = baseline['status']['tick']
        best = baseline['value']
        seen_events = {key: {} for key in changes(baseline, baseline)}
        try:
            if reason is None:
                resumed = bridge.rpc(url, 'game.speed', {'speed': speed})
                if resumed.get('paused') is not False:
                    raise RuntimeError('Resume was not confirmed')
            while reason is None:
                remaining = seconds - (time.monotonic() - started)
                if remaining <= 0:
                    reason = 'timeout' if mode == 'emergency' else f'{mode}_check'
                    break
                time.sleep(min(poll, remaining))
                sample = observe(url, goal, measure_progress=mode == 'emergency')
                if sample['value'] is None:
                    sample['value'] = current['value']
                check_session(current['status'], sample['status'])
                for key, items in changes(current, sample).items():
                    for item in items:
                        identity = (item.get('id', item.get('label')), item.get('priority'))
                        seen_events[key][identity] = item
                current = sample
                delta = changes(baseline, current)
                reason = reason_for(current, delta, goal, mode=mode,
                                    allow_completion=mode == 'emergency',
                                    acknowledged_hostiles=acknowledged_hostiles)
                tick = current['status']['tick']
                if current['value'] > best:
                    best, last_progress = current['value'], tick
                if reason is None and current['status'].get('paused'):
                    reason = 'game_paused'
                if reason is None and max_ticks is not None and tick - baseline['status']['tick'] >= max_ticks:
                    reason = 'game_time_limit'
                if reason is None and mode == 'emergency' and stall_ticks is not None and tick - last_progress >= stall_ticks:
                    reason = 'stalled'
        except (Exception, KeyboardInterrupt) as exc:
            reason, error = 'error', str(exc) or type(exc).__name__
        finally:
            # Even ambiguous resume failure gets one best-effort pause, never
            # another resume or an automatic retry of the failed mutation.
            try:
                bridge.rpc(url, 'game.pause', {'paused': True})
                stopped = bridge.rpc(url, 'game.status', {})
                if not stopped.get('paused') or stopped.get('state') != 'playing':
                    raise RuntimeError('Pause was not confirmed')
                check_session(current['status'], stopped)
                if error is None:
                    final = observe(url, goal)
                    check_session(current['status'], final['status'])
                    current = final
                    if current['status'].get('paused') is not True:
                        raise RuntimeError('Game resumed during final observation; pause was not confirmed')
                    reason = reason_for(current, changes(baseline, current), goal, mode=mode,
                                        acknowledged_hostiles=acknowledged_hostiles) or reason
                    if (reason in ('routine_check', 'maintenance_check') and stall_ticks is not None and
                            current['value'] <= best and
                            current['status']['tick'] - last_progress >= stall_ticks):
                        reason = 'stalled'
            except Exception as exc:
                pause_error = str(exc)
        delta = changes(baseline, current)
        packet = {'reason': 'error' if pause_error else reason, 'mode': mode,
                  'acknowledged_hostiles': list(acknowledged_hostiles),
                  'goal_met': error is None and pause_error is None and current['value'] >= goal['at_least'],
                  'goal': goal, 'progress': {'before': baseline['value'], 'current': current['value'],
                                           'target': goal['at_least']},
                  **delta, 'active_alerts': current['alerts'], 'summary': current['summary'],
                  'observed_events': {key: list(items.values()) for key, items in seen_events.items()},
                  'elapsed_game_ticks': current['status']['tick'] - baseline['status']['tick'],
                  'elapsed_wall_seconds': round(time.monotonic() - started, 2),
                  'paused': pause_error is None, 'error': error, 'pause_error': pause_error}
        bridge.record(repo, {'stage': 'wait_result', 'result': packet})
        return packet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8765')
    parser.add_argument('--repo', type=Path, default=bridge.find_repo())
    parser.add_argument('command', choices=('observe', 'wait'))
    parser.add_argument('--goal-file', type=Path, required=True)
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--mode', choices=('routine', 'maintenance', 'emergency'), default='routine')
    parser.add_argument('--acknowledge-hostile', action='append', default=[], metavar='ID')
    parser.add_argument('--seconds', type=float, default=None)
    parser.add_argument('--poll', type=float, default=None)
    parser.add_argument('--speed', type=int, default=None)
    parser.add_argument('--max-ticks', type=int, default=None)
    parser.add_argument('--stall-ticks', type=int, default=None)
    args = parser.parse_args()
    parsed = urlparse(args.url)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ('', '/')):
        parser.error('Only a loopback HTTP bridge origin is supported')
    goal = json.loads(args.goal_file.read_text(encoding='utf-8-sig'))
    if args.command == 'wait':
        if not (args.repo / 'agent/rimagent/session_lock.py').is_file():
            parser.error('Use --repo to select the rimagent checkout')
        result = wait(args.url, args.repo, goal, write=args.write, seconds=args.seconds,
                      poll=args.poll, speed=args.speed, max_ticks=args.max_ticks,
                      stall_ticks=args.stall_ticks, mode=args.mode,
                      acknowledged_hostiles=args.acknowledge_hostile)
    else:
        result = observe(args.url, goal)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get('reason') == 'error' else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({'error': str(exc), 'retried': False}), file=sys.stderr)
        sys.exit(1)
