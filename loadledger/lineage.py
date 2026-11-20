from .intake import InputError


def trace(conn, site, ticket):
    head = conn.execute('SELECT revision FROM ticket_head WHERE site = ? AND ticket = ?',
                        (site, ticket)).fetchone()
    if head is None:
        raise InputError(f'unknown ticket: {site}/{ticket}')
    records = conn.execute('''
        SELECT revision, operation, weighed_at, material, gross_g, tare_g
        FROM ticket_version WHERE site = ? AND ticket = ? ORDER BY revision
    ''', (site, ticket))
    versions = {row['revision']: {**dict(row), 'sources': []} for row in records}
    sources = conn.execute('''
        SELECT r.revision, r.line, r.batch_id, b.source_id, b.format_name, b.sha256
        FROM source_row r JOIN source_batch b USING(batch_id)
        WHERE r.site = ? AND r.ticket = ? ORDER BY r.revision, r.batch_id, r.line
    ''', (site, ticket))
    for row in sources:
        source = dict(row)
        versions[source.pop('revision')]['sources'].append(source)
    return {'site': site, 'ticket': ticket, 'current_revision': head['revision'],
            'versions': list(versions.values())}
