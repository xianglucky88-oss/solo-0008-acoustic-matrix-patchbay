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
    paper_archive(tmp)


def paper_archive(tmp):
    path = tmp / 'paper.sqlite'
    base = [sys.executable, '-B', '-m', 'loadledger', '--db', str(path)]

    def cli(*args, error=None):
        result = subprocess.run(base + list(args), cwd=ROOT, capture_output=True,
                                text=True, timeout=10)
        if error is not None:
            assert result.returncode == 2 and not result.stdout and error in result.stderr, result
            return None
        assert result.returncode == 0 and not result.stderr, result
        return json.loads(result.stdout)

    def ingest(batch, source, fmt, file, error=None):
        return cli('ingest', '--batch', batch, '--source', source, '--format',
                   fmt, str(file), error=error)

    def write_jsonl(name, *records):
        file = tmp / name
        file.write_text(''.join(json.dumps(row) + '\n' for row in records), encoding='utf-8')
        return file

    def write_csv(name, header, *rows):
        file = tmp / name
        text = ','.join(header) + '\n' + '\n'.join(','.join(map(str, row)) for row in rows) + '\n'
        file.write_text(text, encoding='utf-8')
        return file

    def booklet(site, book):
        return cli('booklet', '--site', site, '--booklet', book)

    def certify(site, book, error=None):
        return cli('certify', '--site', site, '--booklet', book, error=error)

    upsert = dict(operation='upsert', revision=1, gross_g=9_000_000, tare_g=4_000_000,
                  material='residual', weighed_at='2025-07-01T08:00:00Z')
    blank = dict(operation='blank_return')

    # Allocation lists allow gaps (7-8 unissued) and reject overlaps.
    alloc = write_csv('alloc.csv', ('site', 'booklet', 'first_print', 'last_print'),
                      ('south', 'B1', 1, 6), ('south', 'B1', 9, 10),
                      ('south', 'B2', 1, 4), ('north', 'N1', 1, 3))
    result = ingest('alloc-1', 'archive-desk', 'allocation-csv-v1', alloc)
    assert result['rows'] == 4 and result['certificates_held'] == []
    replay = ingest('alloc-1', 'archive-desk', 'allocation-csv-v1', alloc)
    assert replay['status'] == 'replay'
    overlap = write_csv('overlap.csv', ('site', 'booklet', 'first_print', 'last_print'),
                        ('south', 'B1', 5, 7))
    ingest('alloc-bad', 'archive-desk', 'allocation-csv-v1', overlap, error='overlapping allocation')
    bad_header = write_csv('badhead.csv', ('site', 'booklet', 'first', 'last'),
                           ('south', 'B3', 1, 2))
    ingest('alloc-head', 'archive-desk', 'allocation-csv-v1', bad_header,
           error='allocation header must be exactly')

    # Before any paper rows, every issued number is missing, compressed into segments.
    state = booklet('south', 'B1')
    assert state['missing_segments'] == [
        dict(first_print=1, last_print=6), dict(first_print=9, last_print=10)]
    certify('south', 'B1', error='not written off')

    paper1 = write_jsonl('paper1.jsonl',
        dict(upsert, site='south', ticket='T1', booklet='B1', print_no=1),
        dict(upsert, site='south', ticket='T2', booklet='B1', print_no=2),
        dict(blank, site='south', booklet='B1', print_no=3),
        dict(upsert, site='south', ticket='T3', booklet='B1', print_no=4),
        dict(upsert, site='south', ticket='T4', booklet='B1', print_no=5),
        dict(upsert, site='south', ticket='T5', booklet='B1', print_no=6),
        dict(blank, site='south', booklet='B1', print_no=9),
        dict(blank, site='south', booklet='B1', print_no=10),
        dict(upsert, site='south', ticket='T6', booklet='B2', print_no=1),
        dict(upsert, site='north', ticket='N-T1', booklet='N1', print_no=1))
    applied = ingest('paper-1', 'station-paper', 'jsonl-paper-v3', paper1)
    assert (applied['rows'], applied['declarations'], applied['new_versions']) == (10, 10, 7), applied

    # Same print number in another booklet/site is legal.
    state_b1 = booklet('south', 'B1')
    assert state_b1['fully_verified'] and state_b1['missing_segments'] == []
    assert state_b1['verified_count'] == 8 and state_b1['disputed_count'] == 0
    state_b2 = booklet('south', 'B2')
    assert state_b2['missing_segments'] == [dict(first_print=2, last_print=4)]
    assert state_b2['verified_count'] == 1

    # Certificate issuance is one-shot per booklet; repeat requests return the same one.
    issued = certify('south', 'B1')
    assert issued['request_status'] == 'issued' and issued['evidence_count'] == 8
    again = certify('south', 'B1')
    assert again['request_status'] == 'already_issued'
    assert again['certificate_id'] == issued['certificate_id']

    # A reweigh (higher revision) and a void still keep the printed position occupied.
    paper2 = write_jsonl('paper2.jsonl',
        dict(upsert, site='south', ticket='T1', revision=2, booklet='B1', print_no=1,
             gross_g=9_100_000),
        dict(operation='void', site='south', ticket='T2', revision=2,
             booklet='B1', print_no=2))
    ingest('paper-2', 'station-paper', 'jsonl-paper-v3', paper2)
    state_b1 = booklet('south', 'B1')
    assert state_b1['fully_verified'] and state_b1['disputes'] == []
    snapshot = cli('trace', '--site', 'south', '--ticket', 'T2')
    assert snapshot['versions'][-1]['operation'] == 'void'

    # Same ticket re-declaring its own original position never creates a dispute.
    paper_same = write_jsonl('paper-same.jsonl',
        dict(upsert, site='south', ticket='T1', revision=3, booklet='B1', print_no=1,
             gross_g=9_200_000))
    ingest('paper-same', 'station-paper', 'jsonl-paper-v3', paper_same)
    assert booklet('south', 'B1')['disputes'] == []

    # Three dispute families: contested, ticket_split, used_vs_blank; plus out-of-range.
    paper3 = write_jsonl('paper3.jsonl',
        dict(upsert, site='south', ticket='T9', booklet='B1', print_no=4),
        dict(upsert, site='south', ticket='T4', revision=2, booklet='B2', print_no=7),
        dict(upsert, site='south', ticket='T8', booklet='B1', print_no=9),
        dict(upsert, site='south', ticket='T7', booklet='B2', print_no=99),
        dict(blank, site='north', booklet='N9', print_no=1))
    held = ingest('paper-3', 'station-paper', 'jsonl-paper-v3', paper3)
    assert held['certificates_held'] == [issued['certificate_id']]

    state_b1 = booklet('south', 'B1')
    reasons = {d['print_no']: d['reasons'] for d in state_b1['disputes']}
    assert reasons == {4: ['contested'], 5: ['ticket_split'], 9: ['used_vs_blank']}
    blocked = {d['print_no'] for d in state_b1['disputes'] if d['in_allocation']}
    assert blocked == {4, 5, 9}
    # Disputed numbers never leak into missing segments.
    assert state_b1['missing_segments'] == []
    assert state_b1['disputed_count'] == 3
    assert state_b1['verified_count'] == 5
    # Each dispute carries the originating batch and physical line of every claim.
    contested = next(d for d in state_b1['disputes'] if d['print_no'] == 4)
    assert {(e['batch_id'], e['line']) for e in contested['evidence']} == {
        ('paper-1', 4), ('paper-3', 1)}
    split = next(d for d in state_b1['disputes'] if d['print_no'] == 5)
    # The split spans two different booklets of the same site.
    assert split['positions'] == [dict(booklet='B1', print_no=5),
                                  dict(booklet='B2', print_no=7)]

    state_b2 = booklet('south', 'B2')
    assert state_b2['disputed_count'] == 0
    split_outside = next(d for d in state_b2['disputes']
                         if d['reasons'] == ['ticket_split'] and not d['in_allocation'])
    assert split_outside['print_no'] == 7
    assert state_b2['out_of_range'] == [
        dict(print_no=99, reason='position_not_issued',
             evidence=[dict(batch_id='paper-3', line=4, ticket='T7', revision=1,
                            operation='upsert')])]
    # Out-of-range claims do not block the booklet's own issued numbers.
    assert state_b2['missing_segments'] == [dict(first_print=2, last_print=4)]
    unknown = booklet('north', 'N9')
    assert unknown['allocation_present'] is False
    assert unknown['out_of_range'][0]['print_no'] == 1

    # The issued certificate is now pending review, but its issuance evidence is frozen.
    cert = state_b1['certificate']
    assert cert['status'] == 'pending_review'
    certify('south', 'B1', error='pending_review')
    frozen = cli('certificate', '--site', 'south', '--booklet', 'B1')
    assert frozen['status'] == 'pending_review'
    assert [(e['print_no'], e['kind'], e['batch_id'], e['line']) for e in frozen['evidence']] == [
        (1, 'used', 'paper-1', 1), (2, 'used', 'paper-1', 2),
        (3, 'blank', 'paper-1', 3), (4, 'used', 'paper-1', 4),
        (5, 'used', 'paper-1', 5), (6, 'used', 'paper-1', 6),
        (9, 'blank', 'paper-1', 7), (10, 'blank', 'paper-1', 8)]
    cli('certificate', '--site', 'south', '--booklet', 'B2', error='no archival certificate')
    with sqlite3.connect(path) as conn:
        evidence = conn.execute(
            "SELECT print_no, kind, batch_id, line, ticket, operation "
            "FROM certificate_evidence WHERE certificate_id = ? ORDER BY print_no",
            (issued['certificate_id'],)).fetchall()
    assert len(evidence) == 8
    assert evidence[3] == (4, 'used', 'paper-1', 4, 'T3', 'upsert')
    assert evidence[6] == (9, 'blank', 'paper-1', 7, None, 'blank_return')

    # Legacy formats without booklet numbers still land in the warehouse...
    legacy = cli('ingest', '--batch', 'legacy', '--source', 'station-export',
                 '--format', 'csv-kg-v1', str(EXAMPLES / 'first.csv'))
    assert legacy['new_versions'] == 3 and legacy['declarations'] == 0
    assert cli('report', '--start', '2025-06-01', '--end', '2025-06-03')['rows']
    # ...but prove nothing about booklet completeness.
    assert booklet('south', 'B1')['certificate']['status'] == 'pending_review'

    # Contract violations are rejected with physical line numbers.
    bad_blank = write_jsonl('badblank.jsonl',
        dict(blank, site='south', ticket='T1', booklet='B1', print_no=2))
    ingest('bad-blank', 'station-paper', 'jsonl-paper-v3', bad_blank,
           error='line 1: blank_return carries only')
    bad_print = write_jsonl('badprint.jsonl',
        dict(upsert, site='south', ticket='T1', booklet='B1', print_no=0))
    ingest('bad-print', 'station-paper', 'jsonl-paper-v3', bad_print,
           error='line 1: print_no must be')
    bad_op = write_jsonl('badop.jsonl',
        dict(operation='retire', site='south', booklet='B1', print_no=2))
    ingest('bad-op', 'station-paper', 'jsonl-paper-v3', bad_op,
           error='line 1: operation must be')
    bad_bounds = write_csv('badbounds.csv', ('site', 'booklet', 'first_print', 'last_print'),
                           ('south', 'B8', 5, 2))
    ingest('alloc-bounds', 'archive-desk', 'allocation-csv-v1', bad_bounds,
           error='1 <= first_print <= last_print')

    # A booklet with one huge continuous gap stays a single segment (no per-number fan-out).
    big = write_csv('big.csv', ('site', 'booklet', 'first_print', 'last_print'),
                    ('south', 'BIG', 1, 1000))
    ingest('alloc-big', 'archive-desk', 'allocation-csv-v1', big)
    big_state = booklet('south', 'BIG')
    assert big_state['missing_segments'] == [dict(first_print=1, last_print=1000)]
    assert big_state['missing_count'] == 1000

    # A conflicting paper batch rolls back entirely: no partial declarations remain.
    conflict = write_jsonl('conflict.jsonl',
        dict(upsert, site='south', ticket='TX', booklet='B2', print_no=2),
        dict(upsert, site='south', ticket='TX', booklet='B2', print_no=2,
             gross_g=9_000_001))
    ingest('paper-conflict', 'station-paper', 'jsonl-paper-v3', conflict,
           error='conflicting payload')
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM print_declaration WHERE batch_id = 'paper-conflict'").fetchone()
        assert rows[0] == 0
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()

    # Summary listing covers allocation-only, declaration-only and certified booklets.
    listing = cli('booklets', '--site', 'south')['booklets']
    by_book = {item['booklet']: item for item in listing}
    assert set(by_book) == {'B1', 'B2', 'BIG'}
    assert by_book['B1']['certificate']['status'] == 'pending_review'
    assert by_book['BIG']['missing_count'] == 1000
    assert all(item['site'] == 'south' for item in listing)
    assert len(cli('booklets')['booklets']) >= 5


if __name__ == '__main__':
    with TemporaryDirectory() as directory:
        workflow(Path(directory))
    print('PASS: import, UTC and weight normalization, historical rollups, replay, lineage and rollback')
    print('PASS: booklet allocation, paper reconciliation, disputes, certificates and review holds')
