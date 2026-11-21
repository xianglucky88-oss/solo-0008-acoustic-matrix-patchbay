from hashlib import sha256

from .capacity import refresh_ticket_impacts
from .intake import InputError, identifier, parse
from .mart import rebuild


def _head_payload(conn, site, ticket, revision):
    return conn.execute('''
        SELECT v.material AS material, v.weighed_at AS weighed_at, v.operation AS operation
        FROM ticket_version v WHERE v.site = ? AND v.ticket = ? AND v.revision = ?
    ''', (site, ticket, revision)).fetchone()


def record_version(conn, row, line):
    key = (row.site, row.ticket, row.revision)
    payload = (row.operation, row.weighed_at, row.material, row.gross_g, row.tare_g)
    cursor = conn.execute('''
        INSERT INTO ticket_version VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(site, ticket, revision) DO NOTHING
    ''', key + payload)
    if cursor.rowcount == 0:
        previous = conn.execute('''
            SELECT operation, weighed_at, material, gross_g, tare_g FROM ticket_version
            WHERE site = ? AND ticket = ? AND revision = ?
        ''', key).fetchone()
        if tuple(previous) != payload:
            raise InputError(f'line {line}: conflicting payload for {row.site}/{row.ticket} rev {row.revision}')
    return cursor.rowcount


def move_head(conn, candidate, partitions, overrun_impacts):
    site, ticket = candidate.site, candidate.ticket
    previous = conn.execute('SELECT revision, day FROM ticket_head WHERE site = ? AND ticket = ?',
                            (site, ticket)).fetchone()
    if previous is not None and previous['revision'] >= candidate.revision:
        return 0
    if previous is not None:
        if previous['day'] is not None:
            partitions.add((previous['day'], site))
        old = _head_payload(conn, site, ticket, previous['revision'])
        if old is not None and old['operation'] == 'upsert' and old['weighed_at']:
            overrun_impacts.add((site, old['material']))
    day = candidate.weighed_at[:10] if candidate.weighed_at else None
    if day is not None:
        partitions.add((day, site))
    if candidate.operation == 'upsert' and candidate.weighed_at:
        overrun_impacts.add((site, candidate.material))
    conn.execute('''
        INSERT INTO ticket_head VALUES (?, ?, ?, ?)
        ON CONFLICT(site, ticket) DO UPDATE SET revision = excluded.revision, day = excluded.day
    ''', (site, ticket, candidate.revision, day))
    return 1


def ingest(conn, batch_id, source_id, format_name, data):
    identifier(batch_id, 'batch_id')
    identifier(source_id, 'source_id')
    digest = sha256(data).hexdigest()
    batch_key = (source_id, format_name, digest)
    previous = conn.execute('''
        SELECT source_id, format_name, sha256, row_count FROM source_batch WHERE batch_id = ?
    ''', (batch_id,)).fetchone()
    if previous is not None:
        if tuple(previous)[:3] != batch_key:
            raise InputError(f'batch ID {batch_id} already belongs to different input')
        return {'batch_id': batch_id, 'status': 'replay', 'rows': previous['row_count'],
                'new_versions': 0, 'head_changes': 0, 'refreshed_partitions': []}

    conn.execute('INSERT INTO source_batch VALUES (?, ?, ?, ?, 0)', (batch_id,) + batch_key)
    candidates, partitions, overrun_impacts = {}, set(), set()
    rows = versions = 0
    for line, row in parse(data, format_name):
        versions += record_version(conn, row, line)
        conn.execute('INSERT INTO source_row VALUES (?, ?, ?, ?, ?)',
                     (batch_id, line, row.site, row.ticket, row.revision))
        key = (row.site, row.ticket)
        if key not in candidates or candidates[key].revision < row.revision:
            candidates[key] = row
        rows += 1
    if not rows:
        raise InputError('empty source batch')
    changes = sum(move_head(conn, row, partitions, overrun_impacts)
                  for row in candidates.values())
    rebuild(conn, partitions)
    refresh_ticket_impacts(conn, overrun_impacts)
    conn.execute('UPDATE source_batch SET row_count = ? WHERE batch_id = ?', (rows, batch_id))
    return {'batch_id': batch_id, 'status': 'applied', 'rows': rows, 'new_versions': versions,
            'head_changes': changes, 'refreshed_partitions': [dict(day=day, site=site)
                                                             for day, site in sorted(partitions)]}
