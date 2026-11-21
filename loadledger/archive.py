"""Paper ticket booklet reconciliation and archival certificates.

Positions are identified by ``(site, booklet, print_no)``. A weigh row with
booklet/print number claims a printed position; a ``blank_return`` declaration
claims the same position as an unused voided stub. Declarations are
append-only evidence: later rows never overwrite earlier ones, and any
contradiction surfaces as a dispute instead.
"""

from datetime import datetime, timezone

from .intake import InputError


def _ranges(conn, site, booklet):
    rows = conn.execute('''
        SELECT first_print, last_print FROM allocation_row
        WHERE site = ? AND booklet = ? ORDER BY first_print, last_print
    ''', (site, booklet)).fetchall()
    return [(row['first_print'], row['last_print']) for row in rows]


def merge_intervals(intervals):
    merged = []
    for first, last in sorted(intervals):
        if merged and first <= merged[-1][1] + 1:
            if last > merged[-1][1]:
                merged[-1] = (merged[-1][0], last)
        else:
            merged.append((first, last))
    return merged


def _in_ranges(ranges, number):
    for first, last in ranges:
        if first <= number <= last:
            return True
        if first > number:
            return False
    return False


def _evidence(row):
    return {'batch_id': row['batch_id'], 'line': row['line'],
            'ticket': row['ticket'], 'revision': row['revision'],
            'operation': row['operation']}


def _position_disputes(conn, site, booklet, ranges):
    """Return (dispute_map, positions) for one booklet."""
    declarations = conn.execute('''
        SELECT batch_id, line, ticket, revision, operation, print_no
        FROM print_declaration
        WHERE site = ? AND booklet = ?
        ORDER BY print_no, batch_id, line
    ''', (site, booklet)).fetchall()
    positions = {}
    for row in declarations:
        positions.setdefault(row['print_no'], []).append(row)

    disputes = {}

    def entry(print_no):
        item = disputes.setdefault(print_no, {
            'print_no': print_no,
            'reasons': [],
            'in_allocation': _in_ranges(ranges, print_no),
            'evidence': [_evidence(r) for r in positions[print_no]],
        })
        return item

    for print_no, rows in positions.items():
        tickets = {row['ticket'] for row in rows if row['ticket'] is not None}
        has_blank = any(row['operation'] == 'blank_return' for row in rows)
        if has_blank and tickets:
            entry(print_no)['reasons'].append('used_vs_blank')
        if len(tickets) > 1:
            entry(print_no)['reasons'].append('contested')

    # One ticket occupying more than one position, across any booklet of the site.
    split_rows = conn.execute('''
        SELECT ticket, booklet, print_no FROM print_declaration
        WHERE site = ? AND ticket IS NOT NULL
          AND ticket IN (
              SELECT ticket FROM print_declaration
              WHERE site = ? AND booklet = ? AND ticket IS NOT NULL
          )
        ORDER BY ticket, booklet, print_no
    ''', (site, site, booklet)).fetchall()
    ticket_positions = {}
    for row in split_rows:
        ticket_positions.setdefault(row['ticket'], set()).add(
            (row['booklet'], row['print_no']))
    for ticket, occupied in ticket_positions.items():
        if len(occupied) <= 1:
            continue
        for other_booklet, print_no in sorted(occupied):
            if other_booklet != booklet:
                continue
            item = entry(print_no)
            if 'ticket_split' not in item['reasons']:
                item['reasons'].append('ticket_split')
            item['ticket'] = ticket
            item['positions'] = [dict(booklet=book, print_no=no)
                                  for book, no in sorted(occupied)]
    for item in disputes.values():
        item['reasons'] = sorted(item['reasons'])
    return disputes, positions


def _missing_segments(merged_ranges, positions):
    """Compressed segments of issued print numbers that have no declaration at all."""
    known = sorted(no for no in positions if _in_ranges(merged_ranges, no))
    segments = []
    for first, last in merged_ranges:
        cursor = first
        for number in known:
            if number < first:
                continue
            if number > last:
                break
            if cursor <= number - 1:
                segments.append({'first_print': cursor, 'last_print': number - 1})
            cursor = number + 1
        if cursor <= last:
            segments.append({'first_print': cursor, 'last_print': last})
    return segments


def reconcile(conn, site, booklet):
    ranges = _ranges(conn, site, booklet)
    disputes, positions = _position_disputes(conn, site, booklet, ranges)
    blocked = {print_no for print_no, item in disputes.items() if item['in_allocation']}
    merged = merge_intervals(ranges)
    missing = _missing_segments(merged, positions)

    anomalies = [
        {'print_no': print_no, 'reason': 'position_not_issued',
         'evidence': [_evidence(row) for row in rows]}
        for print_no, rows in sorted(positions.items())
        if not _in_ranges(ranges, print_no) and print_no not in disputes
    ]

    issued_count = sum(last - first + 1 for first, last in ranges)
    missing_count = sum(segment['last_print'] - segment['first_print'] + 1
                        for segment in missing)
    verified_count = issued_count - missing_count - len(blocked)
    cert = conn.execute('''
        SELECT certificate_id, status, issued_at FROM archive_certificate
        WHERE site = ? AND booklet = ?
    ''', (site, booklet)).fetchone()
    return {
        'site': site,
        'booklet': booklet,
        'allocation_present': bool(ranges),
        'issued_ranges': [dict(first_print=first, last_print=last)
                          for first, last in ranges],
        'issued_count': issued_count,
        'verified_count': verified_count,
        'missing_count': missing_count,
        'disputed_count': len(blocked),
        'missing_segments': missing,
        'disputes': [disputes[no] for no in sorted(disputes)],
        'out_of_range': anomalies,
        'fully_verified': bool(ranges) and not missing and not blocked,
        'certificate': dict(cert) if cert is not None else None,
    }


def list_booklets(conn, site=None):
    if site is None:
        pairs = conn.execute('''
            SELECT site, booklet FROM allocation_row
            UNION
            SELECT site, booklet FROM print_declaration
            ORDER BY site, booklet
        ''').fetchall()
    else:
        pairs = conn.execute('''
            SELECT site, booklet FROM allocation_row WHERE site = ?
            UNION
            SELECT site, booklet FROM print_declaration WHERE site = ?
            ORDER BY site, booklet
        ''', (site, site)).fetchall()
    return [_summary(reconcile(conn, row['site'], row['booklet'])) for row in pairs]


def _summary(state):
    return {
        'site': state['site'],
        'booklet': state['booklet'],
        'allocation_present': state['allocation_present'],
        'issued_count': state['issued_count'],
        'verified_count': state['verified_count'],
        'missing_count': state['missing_count'],
        'disputed_count': state['disputed_count'],
        'fully_verified': state['fully_verified'],
        'certificate': state['certificate'],
    }


def _certificate_id(site, booklet):
    return f'AC-{site}-{booklet}'


def apply_certificate(conn, site, booklet):
    state = reconcile(conn, site, booklet)
    existing = conn.execute('''
        SELECT certificate_id, site, booklet, status, issued_at
        FROM archive_certificate WHERE site = ? AND booklet = ?
    ''', (site, booklet)).fetchone()
    if existing is not None:
        if existing['status'] == 'pending_review':
            raise InputError(
                f'certificate {existing["certificate_id"]} is pending_review; '
                'resolve disputes before requesting another')
        result = dict(existing)
        result['request_status'] = 'already_issued'
        return result
    if not state['allocation_present']:
        raise InputError(f'no allocation ranges registered for {site}/{booklet}')
    if not state['fully_verified']:
        reasons = []
        if state['missing_segments']:
            reasons.append(f"{state['missing_count']} issued number(s) not written off")
        if state['disputes']:
            reasons.append(f"{state['disputed_count']} disputed number(s) in allocation")
        raise InputError(f'cannot certify {site}/{booklet}: ' + '; '.join(reasons))

    certificate_id = _certificate_id(site, booklet)
    issued_at = (datetime.now(timezone.utc).isoformat(timespec='seconds')
                 .replace('+00:00', 'Z'))
    conn.execute('INSERT INTO archive_certificate VALUES (?, ?, ?, ?, ?)',
                 (certificate_id, site, booklet, 'issued', issued_at))
    declarations = conn.execute('''
        SELECT print_no, ticket, revision, operation, batch_id, line
        FROM print_declaration WHERE site = ? AND booklet = ?
        ORDER BY print_no, batch_id, line
    ''', (site, booklet)).fetchall()
    chosen = {}
    for row in declarations:
        chosen.setdefault(row['print_no'], row)
    ranges = [
        (segment['first_print'], segment['last_print'])
        for segment in state['issued_ranges']
    ]
    for first, last in merge_intervals(ranges):
        for number in range(first, last + 1):
            row = chosen[number]
            kind = 'blank' if row['operation'] == 'blank_return' else 'used'
            conn.execute('''
                INSERT INTO certificate_evidence
                    (certificate_id, print_no, kind, batch_id, line,
                     ticket, revision, operation)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (certificate_id, number, kind, row['batch_id'], row['line'],
                  row['ticket'], row['revision'], row['operation']))
    evidence_count = sum(last - first + 1 for first, last in ranges)
    return {'certificate_id': certificate_id, 'site': site, 'booklet': booklet,
            'status': 'issued', 'issued_at': issued_at,
            'request_status': 'issued', 'evidence_count': evidence_count}


def review_certificates(conn):
    """Flip issued certificates whose booklet is no longer fully verified."""
    flipped = []
    certificates = conn.execute(
        'SELECT certificate_id, site, booklet FROM archive_certificate '
        "WHERE status = 'issued'").fetchall()
    for cert in certificates:
        state = reconcile(conn, cert['site'], cert['booklet'])
        if not state['fully_verified']:
            conn.execute('''
                UPDATE archive_certificate SET status = 'pending_review'
                WHERE certificate_id = ?
            ''', (cert['certificate_id'],))
            flipped.append(cert['certificate_id'])
    return flipped


def view_certificate(conn, site, booklet):
    cert = conn.execute('''
        SELECT certificate_id, site, booklet, status, issued_at
        FROM archive_certificate WHERE site = ? AND booklet = ?
    ''', (site, booklet)).fetchone()
    if cert is None:
        raise InputError(f'no archival certificate for {site}/{booklet}')
    evidence = conn.execute('''
        SELECT print_no, kind, batch_id, line, ticket, revision, operation
        FROM certificate_evidence WHERE certificate_id = ?
        ORDER BY print_no
    ''', (cert['certificate_id'],)).fetchall()
    result = dict(cert)
    result['evidence'] = [dict(row) for row in evidence]
    return result
