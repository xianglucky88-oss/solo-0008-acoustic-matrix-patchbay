"""Exercise split-slip ingestion, pairing, rollups and trace on a fresh warehouse."""

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / 'examples'


def workflow(tmp):
    path = tmp / 'slips.sqlite'
    base = [sys.executable, '-B', '-m', 'loadledger', '--db', str(path)]

    def cli(*args, error=None):
        result = subprocess.run(base + list(args), cwd=ROOT, capture_output=True,
                                text=True, timeout=10)
        if error is not None:
            assert result.returncode == 2 and not result.stdout and error in result.stderr, result
            return None
        assert result.returncode == 0 and not result.stderr, result
        return json.loads(result.stdout)

    def write(name, records):
        file = tmp / name
        if isinstance(records, str):
            file.write_text(records, encoding='utf-8')
        else:
            file.write_text(''.join(json.dumps(row) + '\n' for row in records), encoding='utf-8')
        return file

    def slip(batch, file, *, fmt='jsonl-slip-v1', source='slip-export', error=None):
        return cli('ingest', '--batch', batch, '--source', source, '--format', fmt,
                   str(file), error=error)

    def whole(batch, file, *, error=None):
        return cli('ingest', '--batch', batch, '--source', 'audit-desk', '--format', 'jsonl-g-v2',
                   str(file), error=error)

    def report():
        return cli('report', '--start', '2025-06-01', '--end', '2025-06-12')

    def track(ticket, **kwargs):
        return cli('trace', '--site', 'south', '--ticket', ticket, **kwargs)

    def slip_row(ticket, round_no, kind, revision, *, op='upsert', device='scale-g-01',
                 weighed_at=None, material='organics', weight=10_000_000):
        row = {'site': 'south', 'ticket': ticket, 'round': round_no, 'slip': kind,
               'revision': revision, 'operation': op}
        if op == 'void':
            return row
        row.update(device=device, weighed_at=weighed_at, weight_g=weight)
        if kind == 'gross':
            row['material'] = material
        else:
            row['material'] = None
        return row

    # --- gross slips arrive first; the empty (tare) slip is a shift late ---
    gross_batch = slip('gross-1', EXAMPLES / 'slips_gross.jsonl')
    assert (gross_batch['new_versions'], gross_batch['head_changes'],
            gross_batch['refreshed_partitions']) == (2, 2, []), gross_batch
    assert report()['rows'] == []
    t102 = track('S-102')
    assert t102['mode'] == 'slip' and t102['current_round'] == 1
    assert t102['rounds'][0]['current'] == {'formed': False, 'reason': 'missing_tare_slip',
                                            'gross_revision': 1, 'tare_revision': None}
    assert track('S-101')['rounds'][0]['current']['reason'] == 'missing_tare_slip'

    tare_batch = slip('tare-1', EXAMPLES / 'slips_tare.jsonl', source='slip-export-tare')
    assert (tare_batch['new_versions'], tare_batch['head_changes']) == (1, 0)
    assert tare_batch['refreshed_partitions'] == [dict(day='2025-06-01', site='south')]
    rows = report()['rows']
    assert rows == [dict(day='2025-06-01', site='south', material='organics',
                         ticket_count=1, net_g=12_050_000)], rows

    # identical batch replay changes nothing
    replay = slip('tare-1', EXAMPLES / 'slips_tare.jsonl', source='slip-export-tare')
    assert (replay['status'], replay['head_changes'], replay['refreshed_partitions']) == (
        'replay', 0, []), replay

    # CSV slip contract, timezone normalization and both modes sharing one partition
    csv_batch = slip('csv-1', EXAMPLES / 'slips.csv', fmt='csv-slip-v1')
    assert csv_batch['head_changes'] == 1
    rows = report()['rows']
    assert dict(day='2025-06-01', site='north', material='recyclables',
                ticket_count=1, net_g=8_020_250) in rows

    # --- pairing rule boundaries ---
    boundary_gross = write('boundary-gross.jsonl', [
        slip_row('B-1', 1, 'gross', 1, weighed_at='2025-06-10T10:00:00Z', weight=9_000_000),
        slip_row('B-2', 1, 'gross', 1, weighed_at='2025-06-10T10:00:00Z', weight=9_000_000),
    ])
    slip('b-gross', boundary_gross)
    boundary_tare = write('boundary-tare.jsonl', [
        slip_row('B-1', 1, 'tare', 1, device='scale-t-02', weighed_at='2025-06-10T16:00:00Z',
                 weight=4_000_000),
        slip_row('B-2', 1, 'tare', 1, device='scale-t-02', weighed_at='2025-06-10T16:00:01Z',
                 weight=4_000_000),
    ])
    slip('b-tare', boundary_tare)
    assert track('B-1')['rounds'][0]['current']['formed'] is True
    assert track('B-2')['rounds'][0]['current'] == {
        'formed': False, 'reason': 'weighing_gap_over_six_hours',
        'gross_revision': 1, 'tare_revision': 1}
    rows = report()['rows']
    assert dict(day='2025-06-10', site='south', material='organics',
                ticket_count=1, net_g=5_000_000) in rows  # only B-1 forms

    failures = write('failures.jsonl', [
        slip_row('F-1', 1, 'gross', 1, weighed_at='2025-06-10T12:00:00Z', weight=9_000_000),
        slip_row('F-1', 1, 'tare', 1, device='scale-t-02', weighed_at='2025-06-10T11:00:00Z',
                 weight=4_000_000),
        slip_row('F-2', 1, 'gross', 1, weighed_at='2025-06-10T10:00:00Z', weight=9_000_000),
        slip_row('F-2', 1, 'tare', 1, device='scale-t-02', weighed_at='2025-06-10T16:01:00Z',
                 weight=4_000_000),
        slip_row('F-3', 1, 'gross', 1, weighed_at='2025-06-10T10:00:00Z', weight=5_000_000),
        slip_row('F-3', 1, 'tare', 1, device='scale-t-02', weighed_at='2025-06-10T11:00:00Z',
                 weight=6_000_000),
    ])
    slip('failures', failures)
    assert track('F-1')['rounds'][0]['current']['reason'] == 'tare_before_gross'
    assert track('F-2')['rounds'][0]['current']['reason'] == 'weighing_gap_over_six_hours'
    assert track('F-3')['rounds'][0]['current']['reason'] == 'gross_below_tare'
    rows = report()['rows']
    # B-1 remains the only south organics ticket on 2025-06-10; none of F-* form.
    assert [r for r in rows if r['day'] == '2025-06-10'] == [
        dict(day='2025-06-10', site='south', material='organics',
             ticket_count=1, net_g=5_000_000)]

    # --- modes cannot be mixed on one site/ticket, either direction ---
    whole_row = write('whole.jsonl', [{
        'site': 'south', 'ticket': 'MM-1', 'revision': 1, 'operation': 'upsert',
        'weighed_at': '2025-06-05T08:00:00Z', 'material': 'residual',
        'gross_g': 9_000_000, 'tare_g': 4_000_000}])
    whole('whole-1', whole_row)
    slip('mix-1', write('mix1.jsonl', [slip_row('MM-1', 1, 'gross', 1,
                                                weighed_at='2025-06-05T08:00:00Z')]),
         error='whole-row mode')
    slip('mix-2', write('mix2.jsonl', [slip_row('MM-2', 1, 'gross', 1,
                                                weighed_at='2025-06-05T08:00:00Z')]))
    whole('whole-2', write('whole2.jsonl', [{
        'site': 'south', 'ticket': 'MM-2', 'revision': 1, 'operation': 'upsert',
        'weighed_at': '2025-06-05T08:00:00Z', 'material': 'residual',
        'gross_g': 9_000_000, 'tare_g': 4_000_000}]), error='single-slip mode')

    # whole-row and slip tickets merge in the same rollup partition/material
    whole_peer = write('peer.jsonl', [{
        'site': 'south', 'ticket': 'W-2', 'revision': 1, 'operation': 'upsert',
        'weighed_at': '2025-06-03T07:00:00Z', 'material': 'residual',
        'gross_g': 5_000_000, 'tare_g': 4_000_000}])
    whole('peer-1', whole_peer)

    # --- new round with only one slip removes the old net from the daily report ---
    round2_gross = slip('r2-gross', write('r2g.jsonl', [
        slip_row('S-101', 2, 'gross', 1, weighed_at='2025-06-03T01:00:00Z',
                 material='residual', weight=20_000_000)]))
    assert round2_gross['head_changes'] == 1
    assert round2_gross['refreshed_partitions'] == [dict(day='2025-06-01', site='south')]
    rows = report()['rows']
    assert not any(r['day'] == '2025-06-01' and r['site'] == 'south' for r in rows)
    current = track('S-101')
    assert current['current_round'] == 2
    assert current['rounds'][0]['is_current'] is False
    assert current['rounds'][0]['current']['formed'] is True
    assert current['rounds'][1]['current']['reason'] == 'missing_tare_slip'

    # late backfill of the superseded round keeps evidence but cannot reclaim the ticket
    backfill = slip('r1-backfill', write('r1b.jsonl', [
        slip_row('S-101', 1, 'gross', 1, device='scale-g-01',
                 weighed_at='2025-06-01T08:10:00Z', material='organics', weight=18_250_000)]),
                    source='slip-export-backup')
    assert (backfill['new_versions'], backfill['head_changes'],
            backfill['refreshed_partitions']) == (0, 0, [])
    assert track('S-101')['current_round'] == 2

    # second slip completes round 2; material/day come from the gross slip
    slip('r2-tare', write('r2t.jsonl', [
        slip_row('S-101', 2, 'tare', 1, device='scale-t-02',
                 weighed_at='2025-06-03T04:00:00Z', weight=7_000_000)]))
    rows = report()['rows']
    assert dict(day='2025-06-03', site='south', material='residual',
                ticket_count=2, net_g=14_000_000) in rows  # S-101 13,000,000 + W-2 1,000,000

    # gross correction moves the load to another UTC day but the net stays the same
    slip('r2-fix', write('r2fix.jsonl', [
        slip_row('S-101', 2, 'gross', 2, device='scale-g-01',
                 weighed_at='2025-06-02T23:30:00Z', material='residual', weight=20_000_000)]))
    rows = report()['rows']
    assert dict(day='2025-06-02', site='south', material='residual',
                ticket_count=1, net_g=13_000_000) in rows
    assert dict(day='2025-06-03', site='south', material='residual',
                ticket_count=1, net_g=1_000_000) in rows
    formed = track('S-101')['rounds'][1]['current']
    assert formed['formed'] and formed['day'] == '2025-06-02' and formed['net_g'] == 13_000_000
    assert formed['gross_revision'] == 2

    # voiding the tare while the gross remains removes the ticket from the report
    slip('r2-void', write('r2void.jsonl', [slip_row('S-101', 2, 'tare', 2, op='void')]))
    rows = report()['rows']
    assert not any(r['day'] == '2025-06-02' and r['site'] == 'south' for r in rows)
    current = track('S-101')['rounds'][1]['current']
    assert current == {'formed': False, 'reason': 'tare_slip_void',
                       'gross_revision': 2, 'tare_revision': 2}

    # --- revision conflict rejects the whole batch and leaves no partial evidence ---
    evidence_before = track('S-101')
    slip('conflict', write('conflict.jsonl', [
        slip_row('S-101', 1, 'gross', 1, weighed_at='2025-06-01T08:10:00Z',
                 material='organics', weight=18_250_001)]), error='conflicting slip payload')
    assert track('S-101') == evidence_before

    # trace exposes every source row for both slips and the unformed reason
    traced = track('S-101')
    gross_history = traced['rounds'][0]['slips']['gross']
    assert [v['revision'] for v in gross_history] == [1]
    sources = gross_history[0]['sources']
    assert [(s['batch_id'], s['line']) for s in sources] == [
        ('gross-1', 1), ('r1-backfill', 1)]
    assert all('sha256' in s and s['format_name'] == 'jsonl-slip-v1' for s in sources)
    tare_history = traced['rounds'][0]['slips']['tare']
    assert [(s['batch_id'], s['line']) for s in tare_history[0]['sources']] == [('tare-1', 1)]
    assert traced['rounds'][1]['slips']['tare'][-1]['operation'] == 'void'

    # --- contract validation ---
    bad_round = write('bad-round.jsonl', [dict(slip_row('X-1', 0, 'gross', 1,
                                                        weighed_at='2025-06-05T08:00:00Z'))])
    slip('bad-round', bad_round, error='round must be a positive 32-bit integer')
    bad_kind = write('bad-kind.jsonl', [dict(slip_row('X-1', 1, 'heavy', 1,
                                                      weighed_at='2025-06-05T08:00:00Z'))])
    slip('bad-kind', bad_kind, error="slip must be 'gross'")
    no_material = write('no-material.jsonl', [
        slip_row('X-1', 1, 'gross', 1, weighed_at='2025-06-05T08:00:00Z', material=None)])
    slip('no-material', no_material, error='unknown material')
    tare_material = write('tare-material.jsonl', [
        dict(slip_row('X-1', 1, 'tare', 1, device='scale-t-02',
                      weighed_at='2025-06-05T09:00:00Z', weight=4_000_000),
             material='organics')])
    slip('tare-material', tare_material, error='tare slip carries no material')
    negative = write('negative.jsonl', [
        slip_row('X-1', 1, 'gross', 1, weighed_at='2025-06-05T08:00:00Z', weight=-1)])
    slip('negative', negative, error='weight must satisfy')
    void_extra = write('void-extra.jsonl', [
        dict(slip_row('X-1', 1, 'gross', 1, op='void'), device='scale-g-01')])
    slip('void-extra', void_extra, error='slip void has no device')
    bad_csv = write('bad.csv',
                    'site,ticket,round,slip,revision,operation,device,weighed_at,material,weight_kg\n'
                    'south,X-1,1,gross,1,upsert,scale-g-01,2025-06-05T08:00:00Z,residual\n')
    slip('bad-csv', bad_csv, fmt='csv-slip-v1', error='wrong number of CSV columns')
    track('X-1', error='unknown ticket')


if __name__ == '__main__':
    with TemporaryDirectory() as directory:
        workflow(Path(directory))
    print('PASS: split-slip pairing, round head, rollup refresh, trace evidence and mode lock')
