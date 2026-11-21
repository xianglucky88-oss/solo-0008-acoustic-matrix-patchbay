"""Exercise the settlement allocation workflow end to end."""

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
                                text=True, timeout=20)
        if error is not None:
            assert result.returncode == 2 and error in result.stderr, result
            return None
        assert result.returncode == 0 and not result.stderr, result
        return json.loads(result.stdout)

    def write(name, *records):
        file = tmp / name
        file.write_text(''.join(json.dumps(row) + '\n' for row in records),
                        encoding='utf-8')
        return file

    def totals(doc):
        out = {}
        for line in doc['plans']['primary']:
            out[line['contract_id']] = out.get(line['contract_id'], 0) + line['grams']
        return out

    # Initial tickets: T-1 is the only ticket both contracts can take.
    first = write('first.jsonl',
                  dict(site='south', ticket='T-1', revision=1, operation='upsert',
                       weighed_at='2025-06-10T08:00:00Z', material='organics',
                       gross_g=12000, tare_g=2000))
    cli('ingest', '--batch', 'b1', '--source', 'stn', '--format', 'jsonl-g-v2',
        str(first))
    roster = EXAMPLES / 'settlement-roster.json'
    frozen = cli('roster', str(roster))
    assert frozen['status'] == 'frozen' and frozen['contracts'] == 2
    assert cli('roster', str(roster))['status'] == 'replay'

    proposal = cli('propose', '--doc', 's1', '--roster', 'roster-2025-06')
    assert proposal['status'] == 'open'
    # One 10000 g ticket split between two contracts; both ranges stay feasible.
    for contract in proposal['contracts']:
        assert contract['range_g']['min'] <= contract['recognized_g'] <= contract['range_g']['max']
        for endpoint in contract['endpoint_plans']:
            used = {}
            for line in endpoint['allocations']:
                key = (line['site'], line['ticket'])
                used[key] = used.get(key, 0) + line['grams']
            assert used[('south', 'T-1')] <= 10000
    # Sign the endpoint that gives the mixed contract its maximum. The
    # organics minimum must still be filled, so the split is 8000/2000.
    signed = cli('sign', '--doc', 's1', '--plan', 'C-mixed:max')
    assert signed['status'] == 'signed' and signed['signed_at']
    signed_total = totals(signed)
    assert signed_total == {'C-organics-south': 2000, 'C-mixed': 8000}

    # More tickets arrive, then T-1 is revised at identical net weight. A
    # proposal frozen before the revision must not be signable afterwards.
    write('more.jsonl',
          dict(site='south', ticket='T-2', revision=1, operation='upsert',
               weighed_at='2025-06-11T08:00:00Z', material='organics',
               gross_g=12000, tare_g=2000),
          dict(site='south', ticket='T-3', revision=1, operation='upsert',
               weighed_at='2025-06-12T08:00:00Z', material='organics',
               gross_g=12000, tare_g=2000))
    cli('ingest', '--batch', 'b2', '--source', 'stn', '--format', 'jsonl-g-v2',
        str(tmp / 'more.jsonl'))
    cli('propose', '--doc', 'stale', '--roster', 'roster-2025-06')
    write('revise.jsonl',
          dict(site='south', ticket='T-1', revision=2, operation='upsert',
               weighed_at='2025-06-10T08:00:00Z', material='organics',
               gross_g=12000, tare_g=2000))
    cli('ingest', '--batch', 'b3', '--source', 'stn', '--format', 'jsonl-g-v2',
        str(tmp / 'revise.jsonl'))
    cli('sign', '--doc', 'stale', error='expired')
    expired = cli('mark-expired', '--doc', 'stale')
    assert expired['status'] == 'expired'

    # Reevaluation keeps the old T-1 -> contract grams (8000 organics,
    # 2000 mixed) while the organics minimum is filled by its other tickets.
    reeval = cli('reevaluate', '--doc', 's2', '--parent', 's1')
    assert reeval['objectives']['min_shortfall_g'] == 0
    assert reeval['retained_g'] == 10000
    new_total = totals(reeval)
    assert new_total['C-organics-south'] >= 15000
    # All old T-1 lines survive unchanged.
    t1 = {(line['ticket'], line['contract_id']): line['grams']
          for line in reeval['plans']['primary'] if line['ticket'] == 'T-1'}
    assert t1 == {('T-1', 'C-mixed'): 8000,
                  ('T-1', 'C-organics-south'): 2000}
    diff = reeval['diff']
    assert diff['fully_unretentive'] == [] and diff['reduced'] == []
    cli('sign', '--doc', 's2')

    # A void and a correction trigger a second reevaluation. The voided ticket
    # is fully un-retentive; the correction can only retain its surviving grams.
    write('changes.jsonl',
          dict(site='south', ticket='T-1', revision=3, operation='void'),
          dict(site='south', ticket='T-2', revision=2, operation='upsert',
               weighed_at='2025-06-11T08:00:00Z', material='organics',
               gross_g=11000, tare_g=2000))
    cli('ingest', '--batch', 'b4', '--source', 'stn', '--format', 'jsonl-g-v2',
        str(tmp / 'changes.jsonl'))
    second = cli('reevaluate', '--doc', 's3', '--parent', 's2')
    lost = {(x['ticket'], x['contract_id']): x
            for x in second['diff']['fully_unretentive']}
    assert {key for key in lost if key[0] == 'T-1'} == {
        ('T-1', 'C-mixed'), ('T-1', 'C-organics-south')}
    assert all(item['reason'] == 'ticket-voided' for item in lost.values())
    # Every plan respects ticket net weights and contract maximums.
    roster_doc = json.loads(roster.read_text())
    max_of = {c['contract_id']: c['max_g'] for c in roster_doc['contracts']}
    for line in second['plans']['primary']:
        assert line['grams'] >= 0
    for cid, value in totals(second).items():
        assert value <= max_of[cid]

    # Lineage traces grams to effective revisions and source batch rows, and
    # old signed documents remain queryable.
    lineage = cli('doc-lineage', '--doc', 's3')
    assert [doc['doc_id'] for doc in lineage['doc_chain']] == ['s3', 's2', 's1']
    t2 = [a for a in lineage['allocations']
          if a['ticket'] == 'T-2' and a['plan_name'] == 'primary']
    assert t2 and all(a['revision'] == 2 for a in t2)
    assert all(a['sources'] and all('sha256' in s for s in a['sources'])
               for a in t2)
    assert cli('doc', '--doc', 's1')['status'] == 'signed'
    with sqlite3.connect(path) as conn:
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    print('PASS: roster freeze, optimal allocation ranges, staleness expiry, '
          'reevaluation retention, diffs and lineage')


if __name__ == '__main__':
    with TemporaryDirectory() as directory:
        workflow(Path(directory))
