import csv
import io
import json
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Optional


class InputError(ValueError):
    pass


@dataclass(frozen=True)
class Ticket:
    site: str
    ticket: str
    revision: int
    operation: str
    weighed_at: Optional[str] = None
    material: Optional[str] = None
    gross_g: Optional[int] = None
    tare_g: Optional[int] = None


@dataclass(frozen=True)
class PaperRecord:
    site: str
    operation: str  # upsert, void or blank_return
    ticket: Optional[str]
    revision: Optional[int]
    booklet: Optional[str]
    print_no: Optional[int]
    weighed_at: Optional[str] = None
    material: Optional[str] = None
    gross_g: Optional[int] = None
    tare_g: Optional[int] = None


@dataclass(frozen=True)
class AllocationRange:
    site: str
    booklet: str
    first_print: int
    last_print: int


BASE = ('site', 'ticket', 'revision', 'operation')
CSV_FIELDS = BASE + ('weighed_at', 'material', 'gross_kg', 'tare_kg')
JSON_FIELDS = ('weighed_at', 'material', 'gross_g', 'tare_g')
PAPER_POSITION_FIELDS = ('ticket', 'revision', 'booklet', 'print_no')
ALLOC_FIELDS = ('site', 'booklet', 'first_print', 'last_print')
PAPER_FORMAT = 'jsonl-paper-v3'
ALLOCATION_FORMAT = 'allocation-csv-v1'
MATERIALS = {'organics', 'recyclables', 'residual'}
MAX_GRAMS = 200_000_000
STAMP = r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:[0-9]{2})'


def identifier(value, field):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value):
        raise InputError(f'invalid {field} identifier')
    return value


def utc_stamp(value):
    if not isinstance(value, str) or not re.fullmatch(STAMP, value):
        raise InputError('weighed_at needs an ISO timestamp with explicit UTC offset')
    if not value.endswith('Z') and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
        raise InputError('invalid timestamp offset')
    try:
        dt = datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
        return dt.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    except (ValueError, OverflowError) as exc:
        raise InputError('invalid weighed_at timestamp') from exc


def kg_as_grams(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,6}(?:\.[0-9]{1,3})?', value):
        raise InputError('CSV weights need decimal kilograms with at most 3 decimal places')
    whole, _, fraction = value.partition('.')
    return 1000 * int(whole) + int(fraction.ljust(3, '0'))


def normalize(row, is_csv):
    if not isinstance(row, dict) or not set(BASE) <= row.keys():
        raise InputError('expected site, ticket, revision and operation')
    site, ticket = (identifier(row[field], field) for field in BASE[:2])
    revision = row['revision']
    if is_csv and isinstance(revision, str) and re.fullmatch(r'[0-9]{1,10}', revision):
        revision = int(revision)
    if type(revision) is not int or not 1 <= revision <= 2_147_483_647:
        raise InputError('revision must be a positive 32-bit integer')
    operation = row['operation']
    if operation == 'void':
        if (is_csv and any(row[field] != '' for field in CSV_FIELDS[4:])) or (
            not is_csv and row.keys() != set(BASE)
        ):
            raise InputError('void has no timestamp, material or weights')
        return Ticket(site, ticket, revision, operation)
    if operation != 'upsert':
        raise InputError('operation must be upsert or void')
    if not is_csv and row.keys() != set(BASE + JSON_FIELDS):
        raise InputError('JSONL upsert needs exactly the declared fields')
    weighed_at = utc_stamp(row['weighed_at'])
    material = row['material']
    if not isinstance(material, str) or material not in MATERIALS:
        raise InputError('unknown material')
    if is_csv:
        gross, tare = (kg_as_grams(row[field]) for field in CSV_FIELDS[-2:])
    else:
        gross, tare = (row[field] for field in JSON_FIELDS[-2:])
    if type(gross) is not int or type(tare) is not int or not 0 <= tare < gross <= MAX_GRAMS:
        raise InputError('weights must satisfy 0 <= tare < gross <= 200000000 grams')
    return Ticket(site, ticket, revision, operation, weighed_at, material, gross, tare)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f'duplicate JSON key: {key}')
        result[key] = value
    return result


def invalid_constant(value):
    raise InputError(f'invalid JSON constant: {value}')


def bounded_int(value, field):
    if isinstance(value, bool) or type(value) is not int or not 1 <= value <= 2_147_483_647:
        raise InputError(f'{field} must be a positive 32-bit integer')
    return value


def normalize_paper(row):
    if not isinstance(row, dict) or not {'site', 'operation'} <= row.keys():
        raise InputError('paper records need site and operation')
    site = identifier(row['site'], 'site')
    operation = row['operation']
    if not isinstance(operation, str) or operation not in ('upsert', 'void', 'blank_return'):
        raise InputError('operation must be upsert, void or blank_return')
    keys = set(row)
    if operation == 'blank_return':
        if keys != {'site', 'operation', 'booklet', 'print_no'}:
            raise InputError('blank_return carries only site, operation, booklet and print_no')
        booklet = identifier(row['booklet'], 'booklet')
        print_no = bounded_int(row['print_no'], 'print_no')
        return PaperRecord(site, operation, None, None, booklet, print_no)
    if not {'ticket', 'revision', 'booklet', 'print_no'} <= keys:
        raise InputError('paper weigh records need ticket, revision, booklet and print_no')
    ticket = identifier(row['ticket'], 'ticket')
    revision = bounded_int(row['revision'], 'revision')
    booklet = identifier(row['booklet'], 'booklet')
    print_no = bounded_int(row['print_no'], 'print_no')
    if operation == 'void':
        if keys != {'site', 'operation', *PAPER_POSITION_FIELDS}:
            raise InputError('void carries no timestamp, material or weights')
        return PaperRecord(site, operation, ticket, revision, booklet, print_no)
    if keys != {'site', 'operation', *PAPER_POSITION_FIELDS, *JSON_FIELDS}:
        raise InputError('paper upsert needs exactly the declared fields')
    weighed_at = utc_stamp(row['weighed_at'])
    material = row['material']
    if not isinstance(material, str) or material not in MATERIALS:
        raise InputError('unknown material')
    gross, tare = row['gross_g'], row['tare_g']
    if (isinstance(gross, bool) or type(gross) is not int or isinstance(tare, bool)
            or type(tare) is not int or not 0 <= tare < gross <= MAX_GRAMS):
        raise InputError('weights must satisfy 0 <= tare < gross <= 200000000 grams')
    return PaperRecord(site, operation, ticket, revision, booklet, print_no,
                       weighed_at, material, gross, tare)


def as_ticket(paper):
    return Ticket(paper.site, paper.ticket, paper.revision, paper.operation,
                  paper.weighed_at, paper.material, paper.gross_g, paper.tare_g)


def parse_paper(data):
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise InputError('source file must be UTF-8') from exc
    for line_number, line in enumerate(io.StringIO(text), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line, object_pairs_hook=unique_object, parse_constant=invalid_constant)
            yield line_number, normalize_paper(row)
        except (ValueError, RecursionError) as exc:
            raise InputError(f'line {line_number}: {exc}') from exc


def parse_allocation(data):
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise InputError('allocation file must be UTF-8') from exc
    reader = csv.reader(io.StringIO(text, newline=''), strict=True)
    try:
        fields = next(reader, [])
        if fields != list(ALLOC_FIELDS):
            raise InputError('allocation header must be exactly: site,booklet,first_print,last_print')
        for values in reader:
            if len(values) != len(ALLOC_FIELDS):
                raise InputError('wrong number of allocation columns')
            site = identifier(values[0], 'site')
            booklet = identifier(values[1], 'booklet')
            try:
                first, last = int(values[2]), int(values[3])
            except ValueError:
                raise InputError('allocation bounds must be integers')
            if not 1 <= first <= last <= 2_147_483_647:
                raise InputError('allocation bounds must satisfy 1 <= first_print <= last_print')
            yield reader.line_num, AllocationRange(site, booklet, first, last)
    except (InputError, csv.Error) as exc:
        raise InputError(f'line {max(1, reader.line_num)}: {exc}') from exc


def parse(data, format_name):
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise InputError('source file must be UTF-8') from exc

    if format_name == 'csv-kg-v1':
        reader = csv.reader(io.StringIO(text, newline=''), strict=True)
        try:
            fields = next(reader, [])
            if len(fields) != len(CSV_FIELDS) or set(fields) != set(CSV_FIELDS):
                raise InputError('CSV header must contain each declared field exactly once')
            for values in reader:
                if len(values) != len(fields):
                    raise InputError('wrong number of CSV columns')
                yield reader.line_num, normalize(dict(zip(fields, values)), True)
        except (InputError, csv.Error) as exc:
            raise InputError(f'line {max(1, reader.line_num)}: {exc}') from exc
    elif format_name == 'jsonl-g-v2':
        for line_number, line in enumerate(io.StringIO(text), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line, object_pairs_hook=unique_object, parse_constant=invalid_constant)
                yield line_number, normalize(row, False)
            except (ValueError, RecursionError) as exc:
                raise InputError(f'line {line_number}: {exc}') from exc
    else:
        raise InputError('unknown source format')
