import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .intake import InputError, identifier, SLIP_FORMATS
from .lineage import trace
from .mart import report
from .storage import database
from .warehouse import ingest, ingest_slip


MAX_SOURCE_BYTES = 16 * 1024 * 1024


def main(argv=None):
    parser = argparse.ArgumentParser(description='Load Ledger batch warehouse')
    parser.add_argument('--db', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    upload = commands.add_parser('ingest', help='import a complete source batch atomically')
    upload.add_argument('--batch', required=True)
    upload.add_argument('--source', required=True)
    upload.add_argument('--format',
                        choices=('csv-kg-v1', 'jsonl-g-v2') + SLIP_FORMATS, required=True)
    upload.add_argument('file', type=Path)
    daily = commands.add_parser('report', help='read aggregated loads by UTC day and site')
    daily.add_argument('--start', required=True)
    daily.add_argument('--end', required=True, help='exclusive end date')
    daily.add_argument('--site')
    lineage = commands.add_parser('trace', help='read a ticket history and source row evidence')
    lineage.add_argument('--site', required=True)
    lineage.add_argument('--ticket', required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == 'ingest':
            with args.file.open('rb') as incoming:
                data = incoming.read(MAX_SOURCE_BYTES + 1)
            if len(data) > MAX_SOURCE_BYTES:
                raise InputError('source file exceeds 16 MiB')
            with database(args.db, write=True) as conn:
                if args.format in SLIP_FORMATS:
                    result = ingest_slip(conn, args.batch, args.source, args.format, data)
                else:
                    result = ingest(conn, args.batch, args.source, args.format, data)
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
