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


def ticket(site, ticket_id, ts, material, net_g, revision=1, operation='upsert'):
    row = dict(site=site, ticket=ticket_id, revision=revision, operation=operation)
    if operation == 'upsert':
        row.update(weighed_at=ts, material=material, gross_g=net_g + 1, tare_g=1)
    return row


def rule(rule_id, site, material, valid_from, valid_to, lookback, limit_g,
         operation='upsert'):
    row = dict(rule_id=rule_id, site=site, material=material, operation=operation)
    if operation == 'upsert':
        row.update(valid_from=valid_from, valid_to=valid_to,
                   lookback_seconds=lookback, limit_g=limit_g)
    return row


def capacity_workflow(tmp):
    path = tmp / 'capacity.sqlite'
    base = [sys.executable, '-B', '-m', 'loadledger', '--db', str(path)]

    def cli(*args, error=None):
        result = subprocess.run(base + list(args), cwd=ROOT, capture_output=True,
                                text=True, timeout=10)
        if error is not None:
            assert result.returncode == 2 and not result.stdout and error in result.stderr, result
            return None
        assert result.returncode == 0 and not result.stderr, result
        return json.loads(result.stdout)

    def write(name, *records):
        file = tmp / name
        file.write_text(''.join(json.dumps(row) + '\n' for row in records), encoding='utf-8')
        return file

    def load_rules(name, *records, error=None):
        return cli('rules', str(write(name, *records)), error=error)

    def load_tickets(name, *records):
        return cli('ingest', '--batch', name, '--source', 'station-export',
                   '--format', 'jsonl-g-v2', str(write(name, *records)))

    def overruns(start='2025-06-01T00:00:00Z', end='2025-06-03T00:00:00Z', **kwargs):
        args = ['overruns', '--start', start, '--end', end]
        for key, value in kwargs.items():
            args += ['--' + key, value]
        return cli(*args)['segments']

    def compact(segments):
        return [(s['rule_id'], s['start'][11:19], s['end'][11:19],
                 s['max_excess_g'], s['max_excess_at'][11:19],
                 s['continues_before_query'], s['continues_after_query'])
                 for s in segments]

    # Two rules meet at 01:00, each with a one-hour lookback and a 10 Mg limit.
    rules_file = [
        rule('R1', 'south', 'organics', '2025-06-01T00:00:00Z',
             '2025-06-01T01:00:00Z', 3600, 10_000_000),
        rule('R2', 'south', 'organics', '2025-06-01T01:00:00Z', None,
             3600, 10_000_000),
    ]
    applied = load_rules('rules.jsonl', *rules_file)
    assert applied['status'] == 'applied' and applied['changed_rules'] == 2, applied
    assert load_rules('rules.jsonl', *rules_file)['status'] == 'replay'

    # Overlapping rule ranges are rejected and the transaction leaves no trace.
    load_rules('overlap.jsonl',
               rule('R3', 'south', 'organics', '2025-06-01T00:30:00Z',
                    '2025-06-01T02:00:00Z', 60, 1), error='overlap')

    tickets = [
        ticket('south', 'T1', '2025-06-01T00:20:00Z', 'organics', 6_000_000),
        ticket('south', 'T2', '2025-06-01T00:40:00Z', 'organics', 5_000_000),
    ]
    load_tickets('tickets.jsonl', *tickets)
    # Overrun 00:40 -> 01:20 crosses the handoff and is stored as two pieces,
    # each naming the rule active on that piece.
    assert compact(overruns(site='south')) == [
        ('R1', '00:40:00', '01:00:00', 1_000_000, '00:40:00', False, False),
        ('R2', '01:00:00', '01:20:00', 1_000_000, '01:00:00', False, False),
    ]

    # A query window cutting the interval in half still reports both halves and
    # flags which side continues outside the window.
    clipped = overruns('2025-06-01T00:50:00Z', '2025-06-01T01:10:00Z',
                       site='south')
    assert [(s['rule_id'], s['continues_before_query'], s['continues_after_query'])
            for s in clipped] == [('R1', True, False), ('R2', False, True)]

    # Equality to the limit is not an overrun; a ticket leaving the window can
    # end the segment without any new ticket arriving.
    assert overruns(site='south', material='residual') == []
    load_tickets('equal.jsonl',
                 ticket('south', 'E1', '2025-06-01T00:00:00Z', 'residual', 10_000_000))
    assert overruns(site='south', material='residual') == []

    # First time reaching the segment maximum, with a rule gap in between.
    load_rules('gap.jsonl',
               rule('G1', 'z', 'residual', '2025-06-01T00:00:00Z',
                    '2025-06-01T00:30:00Z', 3600, 10_000_000),
               rule('G2', 'z', 'residual', '2025-06-01T01:00:00Z', None,
                    3600, 10_000_000))
    load_tickets('gap-tickets.jsonl',
                 ticket('z', 'A', '2025-06-01T00:00:00Z', 'residual', 12_000_000),
                 ticket('z', 'B', '2025-06-01T00:10:00Z', 'residual', 5_000_000),
                 ticket('z', 'C', '2025-06-01T00:20:00Z', 'residual', 3_000_000))
    gap_segments = compact(overruns(site='z'))
    assert gap_segments == [
        ('G1', '00:00:00', '00:30:00', 10_000_000, '00:20:00', False, False),
    ], gap_segments

    # Correcting the R2 limit upward removes the second piece only.
    load_rules('limit-fix.jsonl',
               rule('R2', 'south', 'organics', '2025-06-01T01:00:00Z', None,
                    3600, 20_000_000))
    assert compact(overruns(site='south')) == [
        ('R1', '00:40:00', '01:00:00', 1_000_000, '00:40:00', False, False),
    ]

    # Voiding the ticket that pushed the window over the limit removes the rest.
    load_tickets('void.jsonl',
                 ticket('south', 'T2', None, 'organics', 0, revision=2, operation='void'))
    assert overruns(site='south') == []

    # Cross-day reweight: the original overrun used to end only on the next day;
    # moving the heavy ticket to that next day removes it and creates a new
    # segment there, while the daily rollup keeps its own count/net caliber.
    load_rules('crossday.jsonl',
               rule('C1', 'west', 'organics', '2025-06-01T00:00:00Z', None,
                    3600, 10_000_000))
    load_tickets('crossday-tickets.jsonl',
                 ticket('west', 'W1', '2025-06-01T23:30:00Z', 'organics', 12_000_000),
                 ticket('west', 'W2', '2025-06-01T23:45:00Z', 'organics', 3_000_000))
    assert compact(overruns(site='west')) == [
        ('C1', '23:30:00', '00:30:00', 5_000_000, '23:45:00', False, False),
    ]
    load_tickets('crossday-fix.jsonl',
                 ticket('west', 'W1', '2025-06-02T01:00:00Z', 'organics',
                        12_000_000, revision=2))
    assert compact(overruns(site='west')) == [
        ('C1', '01:00:00', '02:00:00', 2_000_000, '01:00:00', False, False),
    ]
    rollup = cli('report', '--start', '2025-06-01', '--end', '2025-06-03',
                 '--site', 'west')['rows']
    assert [(r['day'], r['ticket_count'], r['net_g']) for r in rollup] == [
        ('2025-06-01', 1, 3_000_000), ('2025-06-02', 1, 12_000_000),
    ]

    # Rule validation errors use exit code 2 like every other contract failure.
    cli('overruns', '--start', '2025-06-01T00:00:00Z',
        '--end', '2025-06-01T00:00:00Z', error='start < end')
    cli('overruns', '--start', '2025-06-01', '--end', '2025-06-02',
        error='ISO UTC timestamp')

    # An overrun that is still live when its last ticket leaves the window is
    # stored with the real leave second as its end; a shorter query flags that
    # it continues beyond the (exclusive) query end.
    load_rules('open.jsonl',
               rule('O1', 'o', 'residual', '2025-06-01T00:00:00Z', None,
                    3600, 1_000))
    load_tickets('open-ticket.jsonl',
                 ticket('o', 'O1', '2025-06-01T00:00:00Z', 'residual', 5_000))
    open_segments = overruns('2025-06-01T00:00:00Z', '2025-06-02T00:00:00Z',
                             site='o', material='residual')
    assert [(s['start'], s['end']) for s in open_segments] == [
        ('2025-06-01T00:00:00Z', '2025-06-01T01:00:00Z')], open_segments
    short = overruns('2025-06-01T00:00:00Z', '2025-06-01T00:30:00Z',
                     site='o', material='residual')
    assert short[0]['continues_after_query'] is True
    assert short[0]['continues_before_query'] is False


if __name__ == '__main__':
    with TemporaryDirectory() as directory:
        workflow(Path(directory))
        capacity_workflow(Path(directory))
    print('PASS: import, UTC and weight normalization, historical rollups, replay, '
          'lineage, rollback and capacity overrun segments')
