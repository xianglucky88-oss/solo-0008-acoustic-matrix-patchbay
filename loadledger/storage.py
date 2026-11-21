from contextlib import closing, contextmanager
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS source_batch (
    batch_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    format_name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ticket_version (
    site TEXT NOT NULL,
    ticket TEXT NOT NULL,
    revision INTEGER NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN ('upsert', 'void')),
    weighed_at TEXT,
    material TEXT,
    gross_g INTEGER,
    tare_g INTEGER,
    PRIMARY KEY(site, ticket, revision)
);
CREATE TABLE IF NOT EXISTS source_row (
    batch_id TEXT NOT NULL REFERENCES source_batch(batch_id),
    line INTEGER NOT NULL,
    site TEXT NOT NULL,
    ticket TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY(batch_id, line),
    FOREIGN KEY(site, ticket, revision) REFERENCES ticket_version(site, ticket, revision)
);
CREATE INDEX IF NOT EXISTS source_row_ticket ON source_row(site, ticket, revision);
CREATE TABLE IF NOT EXISTS ticket_head (
    site TEXT NOT NULL,
    ticket TEXT NOT NULL,
    revision INTEGER NOT NULL,
    day TEXT,
    PRIMARY KEY(site, ticket),
    FOREIGN KEY(site, ticket, revision) REFERENCES ticket_version(site, ticket, revision)
);
CREATE INDEX IF NOT EXISTS ticket_head_partition ON ticket_head(day, site);
CREATE TABLE IF NOT EXISTS daily_rollup (
    day TEXT NOT NULL,
    site TEXT NOT NULL,
    material TEXT NOT NULL,
    ticket_count INTEGER NOT NULL,
    net_g INTEGER NOT NULL,
    PRIMARY KEY(day, site, material)
);
CREATE TABLE IF NOT EXISTS settlement_roster (
    roster_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    roster_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlement_contract (
    roster_id TEXT NOT NULL REFERENCES settlement_roster(roster_id),
    contract_id TEXT NOT NULL,
    min_g INTEGER NOT NULL,
    max_g INTEGER NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    PRIMARY KEY(roster_id, contract_id)
);
CREATE TABLE IF NOT EXISTS contract_allowed_site (
    roster_id TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    site TEXT NOT NULL,
    PRIMARY KEY(roster_id, contract_id, site),
    FOREIGN KEY(roster_id, contract_id)
        REFERENCES settlement_contract(roster_id, contract_id)
);
CREATE TABLE IF NOT EXISTS contract_allowed_material (
    roster_id TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    material TEXT NOT NULL,
    PRIMARY KEY(roster_id, contract_id, material),
    FOREIGN KEY(roster_id, contract_id)
        REFERENCES settlement_contract(roster_id, contract_id)
);
CREATE TABLE IF NOT EXISTS settlement_doc (
    doc_id TEXT PRIMARY KEY,
    roster_id TEXT NOT NULL REFERENCES settlement_roster(roster_id),
    kind TEXT NOT NULL CHECK(kind IN ('proposal', 'reevaluation')),
    parent_doc_id TEXT REFERENCES settlement_doc(doc_id),
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'signed', 'expired')),
    signed_at TEXT,
    selected_plan TEXT NOT NULL DEFAULT 'primary',
    min_shortfall_g INTEGER NOT NULL,
    max_recognized_g INTEGER NOT NULL,
    retained_g INTEGER,
    candidate_fingerprint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS doc_candidate (
    doc_id TEXT NOT NULL REFERENCES settlement_doc(doc_id),
    site TEXT NOT NULL,
    ticket TEXT NOT NULL,
    revision INTEGER NOT NULL,
    net_g INTEGER NOT NULL,
    eligible_for TEXT NOT NULL,
    still_valid INTEGER NOT NULL,
    invalid_reason TEXT,
    PRIMARY KEY(doc_id, site, ticket),
    FOREIGN KEY(site, ticket, revision) REFERENCES ticket_version(site, ticket, revision)
);
CREATE TABLE IF NOT EXISTS doc_invalid_candidate (
    doc_id TEXT NOT NULL REFERENCES settlement_doc(doc_id),
    site TEXT NOT NULL,
    ticket TEXT NOT NULL,
    eligible_for TEXT NOT NULL,
    invalid_reason TEXT NOT NULL,
    old_allocations_json TEXT NOT NULL,
    PRIMARY KEY(doc_id, site, ticket)
);
CREATE TABLE IF NOT EXISTS doc_allocation (
    doc_id TEXT NOT NULL REFERENCES settlement_doc(doc_id),
    plan_name TEXT NOT NULL,
    site TEXT NOT NULL,
    ticket TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    grams INTEGER NOT NULL,
    retained_g INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(doc_id, plan_name, site, ticket, contract_id),
    FOREIGN KEY(doc_id, site, ticket) REFERENCES doc_candidate(doc_id, site, ticket)
);
CREATE INDEX IF NOT EXISTS doc_allocation_contract
    ON doc_allocation(doc_id, plan_name, contract_id);
"""


@contextmanager
def database(path, *, write=False):
    # Reads must not create a warehouse through a mistyped path.
    name = path if write else path.resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(name, uri=not write)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        if write:
            conn.executescript(SCHEMA)
        with conn:
            conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield conn
