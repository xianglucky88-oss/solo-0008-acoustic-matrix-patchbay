from .intake import InputError
from .mart import slip_round_state


def trace(conn, site, ticket):
    head = conn.execute('SELECT revision FROM ticket_head WHERE site = ? AND ticket = ?',
                        (site, ticket)).fetchone()
    if head is not None:
        return _whole_trace(conn, site, ticket)
    if conn.execute('SELECT 1 FROM slip_round_head WHERE site = ? AND ticket = ?',
                    (site, ticket)).fetchone() is not None:
        return _slip_trace(conn, site, ticket)
    raise InputError(f'unknown ticket: {site}/{ticket}')


def _whole_trace(conn, site, ticket):
    head = conn.execute('SELECT revision FROM ticket_head WHERE site = ? AND ticket = ?',
                        (site, ticket)).fetchone()
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
    return {'site': site, 'ticket': ticket, 'mode': 'whole-row',
            'current_revision': head['revision'],
            'versions': list(versions.values())}


def _slip_sources(conn, site, ticket, round_no, slip):
    rows = conn.execute('''
        SELECT r.revision, r.line, r.batch_id, b.source_id, b.format_name, b.sha256
        FROM slip_source_row r JOIN source_batch b USING(batch_id)
        WHERE r.site = ? AND r.ticket = ? AND r.round = ? AND r.slip = ?
        ORDER BY r.revision, r.batch_id, r.line
    ''', (site, ticket, round_no, slip))
    versions = {}
    for row in rows:
        source = dict(row)
        revision = source.pop('revision')
        versions.setdefault(revision, []).append(source)
    return versions


def _slip_trace(conn, site, ticket):
    head = conn.execute('SELECT max_round FROM slip_round_head WHERE site = ? AND ticket = ?',
                        (site, ticket)).fetchone()
    max_round = head['max_round']
    rounds = []
    round_rows = conn.execute('''
        SELECT DISTINCT round FROM slip_version
        WHERE site = ? AND ticket = ? ORDER BY round
    ''', (site, ticket)).fetchall()
    for round_row in round_rows:
        round_no = round_row['round']
        slips_out = {}
        for slip in ('gross', 'tare'):
            history = []
            versions = conn.execute('''
                SELECT revision, operation, device, weighed_at, material, weight_g
                FROM slip_version
                WHERE site = ? AND ticket = ? AND round = ? AND slip = ?
                ORDER BY revision
            ''', (site, ticket, round_no, slip)).fetchall()
            source_map = _slip_sources(conn, site, ticket, round_no, slip)
            for version in versions:
                entry = dict(version)
                entry['sources'] = source_map.get(version['revision'], [])
                history.append(entry)
            slips_out[slip] = history
        formed, state = slip_round_state(conn, site, ticket, round_no)
        if formed:
            current = {'formed': True, 'day': state['day'], 'material': state['material'],
                       'net_g': state['net_g'], 'gross_revision': state['gross']['revision'],
                       'tare_revision': state['tare']['revision']}
        else:
            reason = state['reason']
            current = {
                'formed': False, 'reason': reason,
                'gross_revision': state['gross']['revision'] if state.get('gross') else None,
                'tare_revision': state['tare']['revision'] if state.get('tare') else None}
        rounds.append({'round': round_no, 'is_current': round_no == max_round,
                       'current': current, 'slips': slips_out})
    return {'site': site, 'ticket': ticket, 'mode': 'slip',
            'current_round': max_round, 'rounds': rounds}
