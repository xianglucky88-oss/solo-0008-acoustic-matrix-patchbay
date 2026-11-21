"""Capacity rules and continuous overrun segments.

At any UTC second ``t`` the active rule for a site/material is the rule whose
half-open effective interval ``[valid_from, valid_to)`` contains ``t``.  The
windowed net weight is the sum of the *valid* (current, non-void) ticket net
weights for that site/material whose weighing time lies in the half-open
lookback window ``(t - lookback_seconds, t]``.  An overrun second is one where
that sum strictly exceeds the rule's limit; equality is within capacity.

Rules stay effective across day boundaries and their effective intervals never
overlap for the same site/material.  A rule handoff may land inside one
continuous overrun: the overrun is then stored as one segment per rule so every
part shows which rule was active.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Optional

from .intake import InputError, MATERIALS, STAMP, identifier, utc_stamp


RULE_FIELDS = frozenset(('rule_id', 'site', 'material', 'operation', 'valid_from',
                         'valid_to', 'lookback_seconds', 'limit_g'))
BASE_FIELDS = ('rule_id', 'site', 'material', 'operation')


@dataclass(frozen=True)
class Rule:
    rule_id: str
    site: str
    material: str
    operation: str
    valid_from: Optional[int] = None
    valid_to: Optional[int] = None
    lookback_seconds: Optional[int] = None
    limit_g: Optional[int] = None


def epoch(utc_stamp_text):
    """Convert a normalized ...Z UTC stamp to an integer epoch second."""
    return int(datetime.fromisoformat(utc_stamp_text[:-1]).replace(
        tzinfo=timezone.utc).timestamp())


def stamp(epoch_second):
    return datetime.fromtimestamp(epoch_second, timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%SZ')


def _nonneg_int(value, field):
    if type(value) is not int or value < 0:
        raise InputError(f'{field} must be a non-negative integer')
    return value


def _positive_int(value, field):
    if type(value) is not int or value <= 0:
        raise InputError(f'{field} must be a positive integer')
    return value


def normalize_rule(row):
    if not isinstance(row, dict) or not all(field in row for field in BASE_FIELDS):
        raise InputError('expected rule_id, site, material and operation')
    rule_id = identifier(row['rule_id'], 'rule_id')
    site = identifier(row['site'], 'site')
    material = row['material']
    if not isinstance(material, str) or material not in MATERIALS:
        raise InputError('unknown material')
    operation = row['operation']
    if operation == 'void':
        if set(row) != set(BASE_FIELDS):
            raise InputError('rule void only carries rule_id, site, material and operation')
        return Rule(rule_id, site, material, operation)
    if operation != 'upsert':
        raise InputError('operation must be upsert or void')
    if set(row) != RULE_FIELDS:
        raise InputError('rule upsert needs exactly the declared fields')
    valid_from = epoch(utc_stamp(row['valid_from']))
    valid_to_raw = row['valid_to']
    if valid_to_raw is None:
        valid_to = None
    else:
        valid_to = epoch(utc_stamp(valid_to_raw))
        if valid_to <= valid_from:
            raise InputError('valid_to must be strictly after valid_from')
    lookback = _positive_int(row['lookback_seconds'], 'lookback_seconds')
    limit_g = _nonneg_int(row['limit_g'], 'limit_g')
    return Rule(rule_id, site, material, operation, valid_from, valid_to,
                lookback, limit_g)


def parse_rules(data):
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise InputError('rule file must be UTF-8') from exc
    seen = set()
    rules = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise InputError(f'line {line_number}: {exc}') from exc
        try:
            rule = normalize_rule(row)
        except InputError as exc:
            raise InputError(f'line {line_number}: {exc}') from exc
        if rule.rule_id in seen:
            raise InputError(f'line {line_number}: duplicate rule_id {rule.rule_id}')
        seen.add(rule.rule_id)
        rules.append(rule)
    if not rules:
        raise InputError('empty capacity rule batch')
    return rules


def _active_rules(conn, site, material):
    return conn.execute('''
        SELECT rule_id, valid_from, valid_to, lookback_seconds, limit_g
        FROM capacity_rule
        WHERE site = ? AND material = ? AND operation = 'upsert'
        ORDER BY valid_from, rule_id
    ''', (site, material)).fetchall()


def validate_no_overlap(conn):
    """Effective intervals of one site/material may only touch, never overlap."""
    scopes = conn.execute('''
        SELECT DISTINCT site, material FROM capacity_rule WHERE operation = 'upsert'
    ''').fetchall()
    for scope in scopes:
        rules = _active_rules(conn, scope['site'], scope['material'])
        previous_to = None
        for index, rule in enumerate(rules):
            if previous_to is not None and rule['valid_from'] < previous_to:
                raise InputError(
                    f"rules for {scope['site']}/{scope['material']} overlap at "
                    f"{stamp(rule['valid_from'])}")
            # An open-ended rule covers everything after valid_from, so no
            # further rule may follow it in the same scope.
            if rule['valid_to'] is None and index != len(rules) - 1:
                raise InputError(
                    f"rules for {scope['site']}/{scope['material']} overlap at "
                    f"{stamp(rules[index + 1]['valid_from'])}")
            previous_to = rule['valid_to']


def apply_rules(conn, data):
    """Apply rule corrections; every touched scope is rebuilt over its domain."""
    incoming = parse_rules(data)

    changed = 0
    for rule in incoming:
        previous = conn.execute('SELECT * FROM capacity_rule WHERE rule_id = ?',
                                (rule.rule_id,)).fetchone()
        if previous is not None and (previous['site'] != rule.site
                                     or previous['material'] != rule.material):
            raise InputError(
                f'rule_id {rule.rule_id} is bound to {previous["site"]}/'
                f'{previous["material"]} and cannot move')
        if rule.operation == 'void':
            if previous is None:
                raise InputError(f'unknown rule_id for void: {rule.rule_id}')
            conn.execute('DELETE FROM capacity_rule WHERE rule_id = ?', (rule.rule_id,))
            changed += 1
            continue
        payload = (rule.site, rule.material, 'upsert', rule.valid_from,
                   rule.valid_to, rule.lookback_seconds, rule.limit_g)
        if previous is not None and previous['operation'] == 'upsert':
            old_payload = (previous['site'], previous['material'], previous['operation'],
                           previous['valid_from'], previous['valid_to'],
                           previous['lookback_seconds'], previous['limit_g'])
            if old_payload == payload:
                continue
        conn.execute('''
            INSERT INTO capacity_rule VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rule_id) DO UPDATE SET
                site = excluded.site, material = excluded.material,
                operation = excluded.operation, valid_from = excluded.valid_from,
                valid_to = excluded.valid_to, lookback_seconds = excluded.lookback_seconds,
                limit_g = excluded.limit_g
        ''', (rule.rule_id,) + payload)
        changed += 1

    validate_no_overlap(conn)
    # Rule changes can move overrun state anywhere a rule is effective,
    # including regions no ticket reaches directly, so each touched scope is
    # rebuilt over its whole event domain rather than a guessed time span.
    refreshed = []
    for site, material in sorted({(rule.site, rule.material) for rule in incoming}):
        refresh_scope(conn, site, material)
        refreshed.append(dict(site=site, material=material))
    return {'status': 'applied' if changed else 'replay', 'rules': len(incoming),
            'changed_rules': changed, 'refreshed_scopes': refreshed}


def _valid_tickets(conn, site, material):
    rows = conn.execute('''
        SELECT v.weighed_at AS weighed_at,
               v.gross_g - v.tare_g AS net_g
        FROM ticket_head h JOIN ticket_version v USING(site, ticket, revision)
        WHERE h.site = ? AND v.material = ? AND v.operation = 'upsert'
        ORDER BY v.weighed_at, h.ticket
    ''', (site, material)).fetchall()
    return [(epoch(row['weighed_at']), row['net_g']) for row in rows]


def _rule_at(rules, t):
    for rule in rules:
        if rule['valid_from'] <= t and (rule['valid_to'] is None
                                        or t < rule['valid_to']):
            return rule
    return None


def simulate(rules, tickets, lo, hi):
    """Return overrun segments (half-open [start, end)) covering [lo, hi).

    Rules and tickets belong to one site/material.  Tickets are
    ``(epoch_second, net_g)`` pairs.  Segments split whenever the active rule
    changes, so each segment names exactly one rule.
    """
    if lo >= hi:
        return []
    events = {lo}
    # Ticket arrivals and their window-exit times are state-change candidates.
    # A leave can land inside the horizon even when its ticket is before it, so
    # exits are collected for every ticket rather than only in-range ones.
    lookbacks = {rule['lookback_seconds'] for rule in rules}
    for ts, _ in tickets:
        if lo <= ts < hi:
            events.add(ts)
        for lookback in lookbacks:
            leave = ts + lookback
            if lo <= leave < hi:
                events.add(leave)
    for rule in rules:
        if lo <= rule['valid_from'] < hi:
            events.add(rule['valid_from'])
        if rule['valid_to'] is not None and lo <= rule['valid_to'] < hi:
            events.add(rule['valid_to'])

    open_segment = None
    segments = []

    def close(at):
        if open_segment is not None and open_segment['start'] < at:
            segments.append((open_segment['start'], at, open_segment['rule_id'],
                             open_segment['max_excess'], open_segment['max_ts']))

    for t in sorted(events):
        rule = _rule_at(rules, t)
        if rule is None:
            close(t)
            open_segment = None
            continue
        total = sum(net for ts, net in tickets
                    if t - rule['lookback_seconds'] < ts <= t)
        excess = total - rule['limit_g']
        if excess <= 0:
            close(t)
            open_segment = None
            continue
        if open_segment is None:
            open_segment = dict(start=t, rule_id=rule['rule_id'],
                                max_excess=excess, max_ts=t)
        else:
            if open_segment['rule_id'] != rule['rule_id']:
                # The overrun continues through the handoff second, but the
                # active rule changes there: settle both pieces separately.
                close(t)
                open_segment = dict(start=t, rule_id=rule['rule_id'],
                                    max_excess=excess, max_ts=t)
            elif excess > open_segment['max_excess']:
                open_segment['max_excess'] = excess
                open_segment['max_ts'] = t

    if open_segment is not None:
        # No event closed the overrun inside [lo, hi): it is still live at
        # hi - 1, so the stored end is hi. A later refresh with a wider horizon
        # re-evaluates the same scope and moves the end when the state changes.
        close(hi)
    return segments


def refresh_scope(conn, site, material):
    """Recompute every stored overrun segment for one site/material.

    Only scopes touched by a ticket batch or a rule correction ever get here;
    other site/material scopes and the daily rollup partitions stay untouched.
    """
    rules = _active_rules(conn, site, material)
    tickets = _valid_tickets(conn, site, material)
    domain = _domain(rules, tickets)
    conn.execute('DELETE FROM overrun_segment WHERE site = ? AND material = ?',
                 (site, material))
    if domain is None:
        return
    low, high = domain
    for start, end, rule_id, max_excess, max_ts in simulate(rules, tickets, low, high):
        conn.execute('INSERT INTO overrun_segment VALUES (?, ?, ?, ?, ?, ?, ?)',
                     (site, material, start, end, rule_id, max_excess, max_ts))


def _domain(rules, tickets):
    """Full integer-second range in which any rule or ticket could matter."""
    starts = [rule['valid_from'] for rule in rules]
    max_lookback = max((rule['lookback_seconds'] for rule in rules), default=0)
    # Finite rule ends bound their own tail; with an open-ended rule there is no
    # rule end, so the last possible state change is whichever ticket leaves its
    # window latest - not necessarily the latest ticket by weighing time, since
    # a later under-limit ticket can coexist with an earlier still-open overrun.
    boundary_highs = [rule['valid_to'] for rule in rules
                      if rule['valid_to'] is not None]
    if tickets:
        starts.append(min(ts for ts, _ in tickets))
        boundary_highs.append(max(ts + max_lookback for ts, _ in tickets) + 1)
    if not starts:
        return None
    if boundary_highs:
        return min(starts), max(boundary_highs) + 1
    return min(starts), max(starts) + 1


def refresh_ticket_impacts(conn, impacts):
    """Rebuild overrun state for every site/material touched by a ticket batch.

    Only affected scopes are recomputed; within a scope the full event domain
    is used so segments can appear, merge or disappear anywhere a revised,
    voided or expired ticket could still contribute.
    """
    for (site, material) in sorted(impacts):
        refresh_scope(conn, site, material)


def checked_epoch(value, field):
    if not isinstance(value, str) or not re.fullmatch(STAMP, value):
        raise InputError(f'{field} needs an ISO UTC timestamp')
    try:
        return epoch(utc_stamp(value))
    except InputError as exc:
        raise InputError(f'{field}: {exc}') from exc


def overruns(conn, start, end, site=None, material=None):
    start_ts, end_ts = checked_epoch(start, 'start'), checked_epoch(end, 'end')
    if start_ts >= end_ts:
        raise InputError('overrun query requires start < end')
    sql = ('SELECT site, material, start_ts, end_ts, rule_id, max_excess_g, '
           'max_excess_ts FROM overrun_segment '
           'WHERE start_ts < ? AND end_ts > ?')
    params = [end_ts, start_ts]
    if site is not None:
        sql += ' AND site = ?'
        params.append(site)
    if material is not None:
        if material not in MATERIALS:
            raise InputError('unknown material')
        sql += ' AND material = ?'
        params.append(material)
    sql += ' ORDER BY site, material, start_ts, rule_id'
    rows = []
    for row in conn.execute(sql, params):
        rows.append({
            'site': row['site'],
            'material': row['material'],
            'rule_id': row['rule_id'],
            'start': stamp(row['start_ts']),
            'end': stamp(row['end_ts']),
            'max_excess_g': row['max_excess_g'],
            'max_excess_at': stamp(row['max_excess_ts']),
            'continues_before_query': row['start_ts'] < start_ts,
            'continues_after_query': row['end_ts'] >= end_ts,
        })
    return {'start': utc_stamp(start), 'end': utc_stamp(end),
            'site': site, 'material': material, 'segments': rows}
