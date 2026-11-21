import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .intake import InputError, identifier
from .lineage import trace
from .mart import report
from .settlement import (PRIMARY, build_diff, create_roster, doc_lineage,
                         mark_expired, propose, reevaluate, show, sign)
from .storage import database
from .warehouse import ingest


MAX_SOURCE_BYTES = 16 * 1024 * 1024


def _read_file(path):
    with path.open('rb') as incoming:
        data = incoming.read(MAX_SOURCE_BYTES + 1)
    if len(data) > MAX_SOURCE_BYTES:
        raise InputError('input file exceeds 16 MiB')
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description='Load Ledger batch warehouse')
    parser.add_argument('--db', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    upload = commands.add_parser('ingest', help='import a complete source batch atomically')
    upload.add_argument('--batch', required=True)
    upload.add_argument('--source', required=True)
    upload.add_argument('--format', choices=('csv-kg-v1', 'jsonl-g-v2'), required=True)
    upload.add_argument('file', type=Path)
    daily = commands.add_parser('report', help='read aggregated loads by UTC day and site')
    daily.add_argument('--start', required=True)
    daily.add_argument('--end', required=True, help='exclusive end date')
    daily.add_argument('--site')
    lineage = commands.add_parser('trace', help='read a ticket history and source row evidence')
    lineage.add_argument('--site', required=True)
    lineage.add_argument('--ticket', required=True)
    roster = commands.add_parser('roster', help='freeze a settlement contract roster')
    roster.add_argument('file', type=Path)
    proposal = commands.add_parser('propose', help='build a signable allocation proposal')
    proposal.add_argument('--doc', required=True)
    proposal.add_argument('--roster', required=True)
    proposal.add_argument('--plan', default=PRIMARY)
    signing = commands.add_parser('sign', help='sign a frozen proposal or reevaluation')
    signing.add_argument('--doc', required=True)
    signing.add_argument('--plan', default=None)
    expire = commands.add_parser('mark-expired',
                                 help='mark a stale open proposal expired')
    expire.add_argument('--doc', required=True)
    redo = commands.add_parser('reevaluate', help='re-plan against a signed doc')
    redo.add_argument('--doc', required=True, help='new reevaluation doc id')
    redo.add_argument('--parent', required=True, help='signed doc id to reevaluate')
    redo.add_argument('--plan', default=PRIMARY)
    doc = commands.add_parser('doc', help='show a settlement proposal or reevaluation')
    doc.add_argument('--doc', required=True)
    diff = commands.add_parser('doc-diff', help='old vs new allocation details')
    diff.add_argument('--doc', required=True)
    evidence = commands.add_parser('doc-lineage',
                                   help='trace allocations back to revisions and batch rows')
    evidence.add_argument('--doc', required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == 'ingest':
            data = _read_file(args.file)
            with database(args.db, write=True) as conn:
                result = ingest(conn, args.batch, args.source, args.format, data)
        elif args.command == 'roster':
            data = _read_file(args.file)
            with database(args.db, write=True) as conn:
                result = create_roster(conn, data)
        elif args.command == 'propose':
            with database(args.db, write=True) as conn:
                result = propose(conn, args.doc, args.roster,
                                 selected_plan=args.plan)
        elif args.command == 'sign':
            with database(args.db, write=True) as conn:
                result = sign(conn, args.doc, selected_plan=args.plan)
        elif args.command == 'mark-expired':
            with database(args.db, write=True) as conn:
                result = mark_expired(conn, args.doc)
        elif args.command == 'reevaluate':
            with database(args.db, write=True) as conn:
                result = reevaluate(conn, args.doc, args.parent,
                                    selected_plan=args.plan)
        elif args.command == 'doc':
            with database(args.db) as conn:
                result = show(conn, args.doc)
        elif args.command == 'doc-diff':
            with database(args.db) as conn:
                result = build_diff(conn, args.doc)
        elif args.command == 'doc-lineage':
            with database(args.db) as conn:
                result = doc_lineage(conn, args.doc)
        else:
            site = identifier(args.site, 'site') if args.site is not None else None
            with database(args.db) as conn:
                if args.command == 'report':
                    result = report(conn, args.start, args.end, site)
                else:
                    result = trace(conn, site, identifier(args.ticket, 'ticket'))
    except (InputError, OSError, sqlite3.Error) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
