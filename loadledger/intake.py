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


BASE = ('site', 'ticket', 'revision', 'operation')
CSV_FIELDS = BASE + ('weighed_at', 'material', 'gross_kg', 'tare_kg')
JSON_FIELDS = ('weighed_at', 'material', 'gross_g', 'tare_g')
MATERIALS = {'organics', 'recyclables', 'residual'}
MAX_GRAMS = 200_000_000
STAMP = r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:[0-9]{2})'

# Split-slip contracts: the heavy (gross) and empty (tare) weighings arrive as
# separate single-slip records from two devices after the scale replacement.
SLIP_BASE = ('site', 'ticket', 'round', 'slip', 'revision', 'operation')
SLIP_CSV_FIELDS = SLIP_BASE + ('device', 'weighed_at', 'material', 'weight_kg')
SLIP_JSON_FIELDS = ('device', 'weighed_at', 'material', 'weight_g')
SLIP_TYPES = ('gross', 'tare')
SLIP_FORMATS = ('jsonl-slip-v1', 'csv-slip-v1')


@dataclass(frozen=True)
class Slip:
    site: str
    ticket: str
    round: int
    slip: str
    revision: int
    operation: str
    device: Optional[str] = None
    weighed_at: Optional[str] = None
    material: Optional[str] = None
    weight_g: Optional[int] = None


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


def positive_int(value, field, is_csv):
    if is_csv and isinstance(value, str) and re.fullmatch(r'[0-9]{1,10}', value):
        value = int(value)
    if type(value) is not int or not 1 <= value <= 2_147_483_647:
        raise InputError(f'{field} must be a positive 32-bit integer')
    return value


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


def normalize_slip(row, is_csv):
    if not isinstance(row, dict) or not set(SLIP_BASE) <= row.keys():
        raise InputError('expected site, ticket, round, slip, revision and operation')
    site = identifier(row['site'], 'site')
    ticket = identifier(row['ticket'], 'ticket')
    round_no = positive_int(row['round'], 'round', is_csv)
    slip = row['slip']
    if not isinstance(slip, str) or slip not in SLIP_TYPES:
        raise InputError("slip must be 'gross' (heavy) or 'tare' (empty)")
    revision = positive_int(row['revision'], 'revision', is_csv)
    operation = row['operation']
    if operation == 'void':
        if (is_csv and any(row[field] != '' for field in SLIP_CSV_FIELDS[6:])) or (
            not is_csv and row.keys() != set(SLIP_BASE)
        ):
            raise InputError('slip void has no device, timestamp, material or weight')
        return Slip(site, ticket, round_no, slip, revision, operation)
    if operation != 'upsert':
        raise InputError('operation must be upsert or void')
    if not is_csv and row.keys() != set(SLIP_BASE + SLIP_JSON_FIELDS):
        raise InputError('JSONL slip upsert needs exactly the declared fields')
    device = identifier(row['device'], 'device')
    weighed_at = utc_stamp(row['weighed_at'])
    material = row['material']
    if slip == 'gross':
        if not isinstance(material, str) or material not in MATERIALS:
            raise InputError('unknown material')
    elif (is_csv and material != '') or (not is_csv and material is not None):
        raise InputError('tare slip carries no material')
    if is_csv:
        weight = kg_as_grams(row['weight_kg'])
    else:
        weight = row['weight_g']
        if type(weight) is not int or not 0 <= weight <= MAX_GRAMS:
            raise InputError('weight must satisfy 0 <= weight_g <= 200000000 grams')
    if is_csv and not 0 <= weight <= MAX_GRAMS:
        raise InputError('weight must satisfy 0 <= weight <= 200000000 grams')
    return Slip(site, ticket, round_no, slip, revision, operation,
                device, weighed_at, material if slip == 'gross' else None, weight)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f'duplicate JSON key: {key}')
        result[key] = value
    return result


def invalid_constant(value):
    raise InputError(f'invalid JSON constant: {value}')


def parse(data, format_name):
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise InputError('source file must be UTF-8') from exc

    if format_name in ('csv-kg-v1', 'csv-slip-v1'):
        slip = format_name == 'csv-slip-v1'
        fields_declared = SLIP_CSV_FIELDS if slip else CSV_FIELDS
        reader = csv.reader(io.StringIO(text, newline=''), strict=True)
        try:
            fields = next(reader, [])
            if len(fields) != len(fields_declared) or set(fields) != set(fields_declared):
                raise InputError('CSV header must contain each declared field exactly once')
            normalize_row = normalize_slip if slip else normalize
            for values in reader:
                if len(values) != len(fields):
                    raise InputError('wrong number of CSV columns')
                yield reader.line_num, normalize_row(dict(zip(fields, values)), True)
        except (InputError, csv.Error) as exc:
            raise InputError(f'line {max(1, reader.line_num)}: {exc}') from exc
    elif format_name in ('jsonl-g-v2', 'jsonl-slip-v1'):
        slip = format_name == 'jsonl-slip-v1'
        normalize_row = normalize_slip if slip else normalize
        for line_number, line in enumerate(io.StringIO(text), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line, object_pairs_hook=unique_object, parse_constant=invalid_constant)
                yield line_number, normalize_row(row, False)
            except (ValueError, RecursionError) as exc:
                raise InputError(f'line {line_number}: {exc}') from exc
    else:
        raise InputError('unknown source format')
