from datetime import date
import re

from .intake import InputError


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
