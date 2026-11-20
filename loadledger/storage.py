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
