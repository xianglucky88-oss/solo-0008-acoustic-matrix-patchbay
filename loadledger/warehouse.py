from hashlib import sha256

from .intake import InputError, identifier, parse
from .mart import rebuild, slip_round_state


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


def move_head(conn, candidate, partitions):
    site, ticket = candidate.site, candidate.ticket
    previous = conn.execute('SELECT revision, day FROM ticket_head WHERE site = ? AND ticket = ?',
                            (site, ticket)).fetchone()
    if previous is not None and previous['revision'] >= candidate.revision:
        return 0
    if previous is not None and previous['day'] is not None:
        partitions.add((previous['day'], site))
    day = candidate.weighed_at[:10] if candidate.weighed_at else None
    if day is not None:
        partitions.add((day, site))
    conn.execute('''
        INSERT INTO ticket_head VALUES (?, ?, ?, ?)
        ON CONFLICT(site, ticket) DO UPDATE SET revision = excluded.revision, day = excluded.day
    ''', (site, ticket, candidate.revision, day))
    return 1


def _open_batch(conn, batch_id, source_id, format_name, data):
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
        return digest, previous['row_count'], True
    conn.execute('INSERT INTO source_batch VALUES (?, ?, ?, ?, 0)', (batch_id,) + batch_key)
    return digest, 0, False


def _mode_locked_to_slips(conn, site, ticket):
    return conn.execute('SELECT 1 FROM slip_round_head WHERE site = ? AND ticket = ?',
                        (site, ticket)).fetchone() is not None


def ingest(conn, batch_id, source_id, format_name, data):
    digest, previous_rows, replay = _open_batch(conn, batch_id, source_id, format_name, data)
    if replay:
        return {'batch_id': batch_id, 'status': 'replay', 'rows': previous_rows,
                'new_versions': 0, 'head_changes': 0, 'refreshed_partitions': []}

    candidates, partitions = {}, set()
    rows = versions = 0
    for line, row in parse(data, format_name):
        if _mode_locked_to_slips(conn, row.site, row.ticket):
            raise InputError(
                f'line {line}: {row.site}/{row.ticket} already uses single-slip mode; modes cannot be mixed')
        versions += record_version(conn, row, line)
        conn.execute('INSERT INTO source_row VALUES (?, ?, ?, ?, ?)',
                     (batch_id, line, row.site, row.ticket, row.revision))
        key = (row.site, row.ticket)
        if key not in candidates or candidates[key].revision < row.revision:
            candidates[key] = row
        rows += 1
    if not rows:
        raise InputError('empty source batch')
    changes = sum(move_head(conn, row, partitions) for row in candidates.values())
    rebuild(conn, partitions)
    conn.execute('UPDATE source_batch SET row_count = ? WHERE batch_id = ?', (rows, batch_id))
    return {'batch_id': batch_id, 'status': 'applied', 'rows': rows, 'new_versions': versions,
            'head_changes': changes, 'refreshed_partitions': [dict(day=day, site=site)
                                                             for day, site in sorted(partitions)]}


def record_slip_version(conn, row, line):
    key = (row.site, row.ticket, row.round, row.slip, row.revision)
    payload = (row.operation, row.device, row.weighed_at, row.material, row.weight_g)
    cursor = conn.execute('''
        INSERT INTO slip_version VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(site, ticket, round, slip, revision) DO NOTHING
    ''', key + payload)
    if cursor.rowcount == 0:
        previous = conn.execute('''
            SELECT operation, device, weighed_at, material, weight_g FROM slip_version
            WHERE site = ? AND ticket = ? AND round = ? AND slip = ? AND revision = ?
        ''', key).fetchone()
        if tuple(previous) != payload:
            raise InputError(
                f'line {line}: conflicting slip payload for {row.site}/{row.ticket} '
                f'round {row.round} {row.slip} rev {row.revision}')
    return cursor.rowcount


def _advance_round_head(conn, site, ticket, round_no, partitions):
    previous = conn.execute('SELECT max_round FROM slip_round_head WHERE site = ? AND ticket = ?',
                            (site, ticket)).fetchone()
    if previous is not None and previous['max_round'] >= round_no:
        return 0
    if previous is not None:
        formed, state = slip_round_state(conn, site, ticket, previous['max_round'])
        if formed:
            partitions.add((state['day'], site))
    conn.execute('''
        INSERT INTO slip_round_head VALUES (?, ?, ?)
        ON CONFLICT(site, ticket) DO UPDATE SET max_round = excluded.max_round
    ''', (site, ticket, round_no))
    return 1


def _slip_mode_locked_to_whole(conn, site, ticket):
    return conn.execute('SELECT 1 FROM ticket_head WHERE site = ? AND ticket = ?',
                        (site, ticket)).fetchone() is not None


def ingest_slip(conn, batch_id, source_id, format_name, data):
    _, previous_rows, replay = _open_batch(conn, batch_id, source_id, format_name, data)
    if replay:
        return {'batch_id': batch_id, 'status': 'replay', 'rows': previous_rows,
                'new_versions': 0, 'head_changes': 0, 'refreshed_partitions': []}

    touched, partitions = set(), set()
    rows = versions = 0
    for line, row in parse(data, format_name):
        if _slip_mode_locked_to_whole(conn, row.site, row.ticket):
            raise InputError(
                f'line {line}: {row.site}/{row.ticket} already uses whole-row mode; modes cannot be mixed')
        key = (row.site, row.ticket)
        if key not in touched:
            head = conn.execute('SELECT max_round FROM slip_round_head WHERE site = ? AND ticket = ?',
                                key).fetchone()
            if head is not None:
                formed, state = slip_round_state(conn, row.site, row.ticket, head['max_round'])
                if formed:
                    partitions.add((state['day'], row.site))
            touched.add(key)
        versions += record_slip_version(conn, row, line)
        conn.execute('''
            INSERT INTO slip_source_row VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (batch_id, line, row.site, row.ticket, row.round, row.slip, row.revision))
        rows += 1
    if not rows:
        raise InputError('empty source batch')

    changes = 0
    for site, ticket in sorted(touched):
        latest = conn.execute('SELECT MAX(round) AS round FROM slip_version WHERE site = ? AND ticket = ?',
                              (site, ticket)).fetchone()['round']
        changes += _advance_round_head(conn, site, ticket, latest, partitions)
        head = conn.execute('SELECT max_round FROM slip_round_head WHERE site = ? AND ticket = ?',
                            (site, ticket)).fetchone()
        formed, state = slip_round_state(conn, site, ticket, head['max_round'])
        if formed:
            partitions.add((state['day'], site))

    rebuild(conn, partitions)
    conn.execute('UPDATE source_batch SET row_count = ? WHERE batch_id = ?', (rows, batch_id))
    return {'batch_id': batch_id, 'status': 'applied', 'rows': rows, 'new_versions': versions,
            'head_changes': changes, 'refreshed_partitions': [dict(day=day, site=site)
                                                             for day, site in sorted(partitions)]}
