"""Read-only 24-hour profile routing measurement; no inference or mutations."""
import argparse
import datetime as dt
import json
from pathlib import Path
import statistics
import time


def measure(paths: list[Path], now: float, hours: int = 24) -> dict:
    decisions, outcomes = [], []
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
                if not now-hours*3600 <= float(row['ts']) <= now:
                    continue
                fields = row.get('routing') or row
                profile = fields.get('agent_profile')
                if not profile:
                    continue
                entry = {**fields, **row, 'agent_profile': profile}
                if row.get('event') == 'decision':
                    decisions.append(entry)
                elif row.get('event') in ('completed','escalation_outcome'):
                    outcomes.append(entry)
            except (ValueError, TypeError, KeyError):
                continue
    profiles = {}
    for profile in ('bossman','jarvis','odin','hunter','alfred'):
        requested = [r for r in decisions if r['agent_profile'] == profile]
        finished = [r for r in outcomes if r['agent_profile'] == profile]
        failures = [r for r in finished if r.get('status') != 200 or r.get('error')]
        latency = [r['elapsed_ms'] for r in finished if isinstance(r.get('elapsed_ms'), (int,float))]
        escalated = [r for r in requested if r.get('initial_mode') != r.get('escalated_mode')]
        profiles[profile] = {'requests': len(requested), 'escalations': len(escalated),
                             'escalation_rate': round(len(escalated)/len(requested),4) if requested else None,
                             'outcomes':len(finished), 'failures':len(failures),
                             'unresolved_requests': len({r['request_id'] for r in requested}
                                                        - {r['request_id'] for r in finished}),
                             'mean_latency_ms':round(statistics.mean(latency),2) if latency else None,
                             'max_latency_ms':max(latency) if latency else None,
                             'state':'MEASURED' if requested or finished else 'NO_DATA'}
    return {'measured_at': dt.datetime.fromtimestamp(now,dt.timezone.utc).isoformat(),
            'hours':hours, 'source_logs':[str(p) for p in paths], 'profiles':profiles,
            'note':'NO_DATA is not a successful escalation test; latency includes queue/load time.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    report = measure([Path('/srv/logs/gx-auto-routing.jsonl.1'),Path('/srv/logs/gx-auto-routing.jsonl')],time.time())
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(report,indent=2))
    args.out.chmod(0o600)
    print(json.dumps(report))
