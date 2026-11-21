from datetime import date, datetime
import re

from .intake import InputError

MAX_PAIR_GAP_SECONDS = 6 * 60 * 60


def rebuild(conn, partitions):
    for day, site in sorted(partitions):
        conn.execute('DELETE FROM daily_rollup WHERE day = ? AND site = ?', (day, site))
        conn.execute('''
            INSERT INTO daily_rollup(day, site, material, ticket_count, net_g)
            SELECT h.day, h.site, v.material, COUNT(*), SUM(v.gross_g - v.tare_g)
            FROM ticket_head h JOIN ticket_version v USING(site, ticket, revision)
            WHERE h.day = ? AND h.site = ?
            GROUP BY h.day, h.site, v.material
        ''', (day, site))
        conn.execute('''
            INSERT INTO daily_rollup(day, site, material, ticket_count, net_g)
            SELECT p.day, p.site, p.material, COUNT(*), SUM(p.net_g) FROM (
                SELECT h.site, substr(g.weighed_at, 1, 10) AS day, g.material,
                       g.weight_g - t.weight_g AS net_g
                FROM slip_round_head h
                JOIN slip_version g
                  ON g.site = h.site AND g.ticket = h.ticket
                 AND g.round = h.max_round AND g.slip = 'gross'
                 AND g.revision = (
                     SELECT MAX(g2.revision) FROM slip_version g2
                     WHERE g2.site = g.site AND g2.ticket = g.ticket
                       AND g2.round = g.round AND g2.slip = 'gross')
                JOIN slip_version t
                  ON t.site = h.site AND t.ticket = h.ticket
                 AND t.round = h.max_round AND t.slip = 'tare'
                 AND t.revision = (
                     SELECT MAX(t2.revision) FROM slip_version t2
                     WHERE t2.site = t.site AND t2.ticket = t.ticket
                       AND t2.round = t.round AND t2.slip = 'tare')
                WHERE g.operation = 'upsert' AND t.operation = 'upsert'
                  AND t.weighed_at >= g.weighed_at
                  AND (julianday(t.weighed_at) - julianday(g.weighed_at)) * 86400.0 <= ?
                  AND g.weight_g >= t.weight_g
            ) p
            WHERE p.day = ? AND p.site = ?
            GROUP BY p.day, p.site, p.material
            ON CONFLICT(day, site, material) DO UPDATE SET
                ticket_count = daily_rollup.ticket_count + excluded.ticket_count,
                net_g = daily_rollup.net_g + excluded.net_g
        ''', (MAX_PAIR_GAP_SECONDS + 0.001, day, site))


def _effective_slips(conn, site, ticket, round_no):
    """Highest revision per slip type for one weighing round."""
    result = {}
    rows = conn.execute('''
        SELECT v.slip, v.revision, v.operation, v.device, v.weighed_at,
               v.material, v.weight_g
        FROM slip_version v
        WHERE v.site = ? AND v.ticket = ? AND v.round = ?
          AND v.revision = (
              SELECT MAX(v2.revision) FROM slip_version v2
              WHERE v2.site = v.site AND v2.ticket = v.ticket
                AND v2.round = v.round AND v2.slip = v.slip)
    ''', (site, ticket, round_no))
    for row in rows:
        result[row['slip']] = dict(row)
    return result


def slip_round_state(conn, site, ticket, round_no):
    """Return (formed, state) where state is the formed load or the reason."""
    slips = _effective_slips(conn, site, ticket, round_no)
    gross = slips.get('gross')
    tare = slips.get('tare')
    state = {'gross': gross, 'tare': tare}
    if gross is None:
        return False, {'reason': 'missing_gross_slip', **state}
    if tare is None:
        return False, {'reason': 'missing_tare_slip', **state}
    if gross['operation'] == 'void':
        return False, {'reason': 'gross_slip_void', **state}
    if tare['operation'] == 'void':
        return False, {'reason': 'tare_slip_void', **state}
    if tare['weighed_at'] < gross['weighed_at']:
        return False, {'reason': 'tare_before_gross', **state}
    gap = (datetime.fromisoformat(tare['weighed_at'].replace('Z', '+00:00'))
           - datetime.fromisoformat(gross['weighed_at'].replace('Z', '+00:00')))
    if gap.total_seconds() > MAX_PAIR_GAP_SECONDS:
        return False, {'reason': 'weighing_gap_over_six_hours', **state}
    if gross['weight_g'] < tare['weight_g']:
        return False, {'reason': 'gross_below_tare', **state}
    state['day'] = gross['weighed_at'][:10]
    state['material'] = gross['material']
    state['net_g'] = gross['weight_g'] - tare['weight_g']
    return True, state


def checked_day(value):
    try:
        if re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
            return date.fromisoformat(value).isoformat()
    except ValueError:
        pass
    raise InputError('expected a valid YYYY-MM-DD date')


def report(conn, start, end, site):
    start, end = checked_day(start), checked_day(end)
    if start >= end:
        raise InputError('report requires start < end (exclusive)')
    sql = 'SELECT * FROM daily_rollup WHERE day >= ? AND day < ?'
    params = (start, end)
    if site is not None:
        sql += ' AND site = ?'
        params += (site,)
    rows = conn.execute(sql + ' ORDER BY day, site, material', params)
    return {'start': start, 'end': end, 'site': site, 'rows': list(map(dict, rows))}
