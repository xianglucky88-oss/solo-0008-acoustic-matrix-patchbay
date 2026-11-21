from hashlib import sha256

from .archive import review_certificates
from .intake import (InputError, as_ticket, identifier, parse, parse_allocation,
                     parse_paper, ALLOCATION_FORMAT, PAPER_FORMAT)
from .mart import rebuild


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


def _bind_batch(conn, batch_id, source_id, format_name, data):
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
        return previous
    conn.execute('INSERT INTO source_batch VALUES (?, ?, ?, ?, 0)', (batch_id,) + batch_key)
    return None


def ingest(conn, batch_id, source_id, format_name, data):
    previous = _bind_batch(conn, batch_id, source_id, format_name, data)
    if previous is not None:
        return {'batch_id': batch_id, 'status': 'replay', 'rows': previous['row_count'],
                'new_versions': 0, 'head_changes': 0, 'refreshed_partitions': [],
                'declarations': 0, 'certificates_held': []}

    if format_name == ALLOCATION_FORMAT:
        return ingest_allocation(conn, batch_id, source_id, data, bound=True)
    if format_name not in ('csv-kg-v1', 'jsonl-g-v2', PAPER_FORMAT):
        raise InputError('unknown source format')

    paper_mode = format_name == PAPER_FORMAT
    stream = parse_paper(data) if paper_mode else parse(data, format_name)
    candidates, partitions = {}, set()
    rows = versions = declarations = 0
    for line, record in stream:
        if paper_mode:
            row = as_ticket(record) if record.operation != 'blank_return' else None
            conn.execute('''
                INSERT INTO print_declaration
                    (batch_id, line, site, booklet, print_no, ticket, revision, operation)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (batch_id, line, record.site, record.booklet, record.print_no,
                  record.ticket, record.revision, record.operation))
            declarations += 1
        else:
            row = record
        if row is not None:
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
    held = review_certificates(conn)
    conn.execute('UPDATE source_batch SET row_count = ? WHERE batch_id = ?', (rows, batch_id))
    return {'batch_id': batch_id, 'status': 'applied', 'rows': rows, 'new_versions': versions,
            'head_changes': changes,
            'refreshed_partitions': [dict(day=day, site=site)
                                     for day, site in sorted(partitions)],
            'declarations': declarations, 'certificates_held': held}


def ingest_allocation(conn, batch_id, source_id, data, *, bound=False):
    if not bound:
        previous = _bind_batch(conn, batch_id, source_id, ALLOCATION_FORMAT, data)
        if previous is not None:
            return {'batch_id': batch_id, 'status': 'replay',
                    'rows': previous['row_count'], 'certificates_held': []}

    rows = 0
    in_batch = {}
    for line, item in parse_allocation(data):
        key = (item.site, item.booklet)
        for first, last in in_batch.setdefault(key, []):
            if item.first_print <= last and first <= item.last_print:
                raise InputError(
                    f'line {line}: overlapping allocation in {item.site}/{item.booklet} '
                    f'[{item.first_print},{item.last_print}] vs [{first},{last}]')
        stored = conn.execute('''
            SELECT first_print, last_print FROM allocation_row
            WHERE site = ? AND booklet = ?
        ''', key).fetchall()
        for stored_row in stored:
            first, last = stored_row['first_print'], stored_row['last_print']
            if item.first_print <= last and first <= item.last_print:
                raise InputError(
                    f'line {line}: overlapping allocation in {item.site}/{item.booklet} '
                    f'[{item.first_print},{item.last_print}] vs [{first},{last}]')
        in_batch[key].append((item.first_print, item.last_print))
        conn.execute('''
            INSERT INTO allocation_row(batch_id, line, site, booklet, first_print, last_print)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (batch_id, line, item.site, item.booklet, item.first_print, item.last_print))
        rows += 1
    if not rows:
        raise InputError('empty allocation batch')
    held = review_certificates(conn)
    conn.execute('UPDATE source_batch SET row_count = ? WHERE batch_id = ?', (rows, batch_id))
    return {'batch_id': batch_id, 'status': 'applied', 'rows': rows,
            'certificates_held': held}
