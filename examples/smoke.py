"""Exercise the public CLI against a fresh SQLite file."""

from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / 'examples'


def workflow(tmp):
    path = tmp / 'ledger.sqlite'
    base = [sys.executable, '-B', '-m', 'loadledger', '--db', str(path)]

    def cli(*args, error=None):
        result = subprocess.run(base + list(args), cwd=ROOT, capture_output=True,
                                text=True, timeout=10)
        if error is not None:
            assert result.returncode == 2 and not result.stdout and error in result.stderr, result
            return None
        assert result.returncode == 0 and not result.stderr, result
        return json.loads(result.stdout)

    def ingest(batch, file, *, csv=False, error=None):
        return cli('ingest', '--batch', batch, '--source', 'station-export' if csv else 'audit-desk', '--format',
                   'csv-kg-v1' if csv else 'jsonl-g-v2', str(file), error=error)

    def write(name, *records):
        file = tmp / name
        file.write_text(''.join(json.dumps(row) + '\n' for row in records), encoding='utf-8')
        return file

    def report(*args, **kwargs):
        return cli('report', '--start', '2025-06-01', '--end', '2025-06-03', *args, **kwargs)

    def track(ticket='T-41', **kwargs):
        return cli('trace', '--site', 'south', '--ticket', ticket, **kwargs)

    report(error='unable to open database file')
    assert not path.exists()
    first = ingest('opening', EXAMPLES / 'first.csv', csv=True)
    assert (first['new_versions'], first['head_changes']) == (3, 3), first
    cli('report', '--start', '2025-06-02', '--end', '2025-06-01', error='start < end')
    cli('report', '--start', '2025-06-31', '--end', '2025-07-02', error='valid YYYY-MM-DD')
    assert report('--site', 'south')['rows'] == [
        dict(day='2025-06-01', site='south', material='organics', ticket_count=1, net_g=2_200_000),
        dict(day='2025-06-01', site='south', material='residual', ticket_count=1, net_g=5_200_125),
    ]

    corrections = EXAMPLES / 'corrections.jsonl'
    revised = ingest('fixes', corrections)
    assert revised['refreshed_partitions'] == [dict(day=day, site='south')
                                              for day in ('2025-06-01', '2025-06-02')]
    revised_report = report()
    assert revised_report['rows'] == [
        dict(day='2025-06-02', site='north', material='recyclables', ticket_count=1, net_g=3_449_500),
        dict(day='2025-06-02', site='south', material='organics', ticket_count=1, net_g=5_200_125),
    ]
    assert track('T-42')['versions'][-1]['operation'] == 'void'
    replay = ingest('fixes', corrections)
    assert (replay['status'], replay['head_changes'], replay['rows']) == ('replay', 0, 2), replay

    duplicate = dict(site='south', ticket='T-41', revision=1, operation='upsert',
                     weighed_at='2025-06-01T16:45:00Z', material='residual',
                     gross_g=10_210_250, tare_g=5_010_125)
    late = dict(duplicate, ticket='T-42', material='organics', gross_g=7_200_000,
                tare_g=5_000_000, weighed_at='2025-06-01T18:00:00Z')
    repeats = write('late.jsonl', duplicate, late)
    stale = ingest('late-copy', repeats)
    assert (stale['new_versions'], stale['head_changes'], stale['refreshed_partitions']) == (0, 0, [])
    evidence = track()
    assert evidence['current_revision'] == 2
    sources = evidence['versions'][0]['sources']
    assert [(s['batch_id'], s['line']) for s in sources] == [('late-copy', 1), ('opening', 2)]
    assert [(s['source_id'], s['sha256']) for s in sources] == [
        ('audit-desk', sha256(repeats.read_bytes()).hexdigest()),
        ('station-export', sha256((EXAMPLES / 'first.csv').read_bytes()).hexdigest()),
    ]

    fresh = dict(duplicate, ticket='T-99')
    conflict = write('conflict.jsonl', fresh, dict(duplicate, gross_g=10_210_251))
    ingest('conflict', conflict, error='line 2: conflicting payload')
    track('T-99', error='unknown ticket')
    ingest('fixes', conflict, error='already belongs to different input')
    invalid = write('bad.jsonl', fresh, dict(duplicate, weighed_at='2025-06-01T12:30:00+00:60'))
    ingest('invalid', invalid, error='line 2: invalid timestamp offset')
    track('T-99', error='unknown ticket')
    assert report() == revised_report
    assert track() == evidence
    with sqlite3.connect(path) as conn:
        batches = conn.execute('SELECT batch_id FROM source_batch ORDER BY batch_id').fetchall()
        assert batches == [('fixes',), ('late-copy',), ('opening',)], batches
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()

    # An unseen lower revision is retained for audit, but cannot revive a void.
    version4 = dict(site='south', ticket='T-54', revision=4, operation='void')
    version2 = dict(duplicate, ticket='T-54', revision=2)
    ingest('future-void', write('void.jsonl', version4))
    imported = ingest('older-version', write('old.jsonl', version2))
    assert (imported['new_versions'], imported['head_changes']) == (1, 0)
    assert report() == revised_report
    assert [v['revision'] for v in track('T-54')['versions']] == [2, 4]


if __name__ == '__main__':
    with TemporaryDirectory() as directory:
        workflow(Path(directory))
    print('PASS: import, UTC and weight normalization, historical rollups, replay, lineage and rollback')
