"""Shared configuration, SQLite helpers and schema for the derivative service.

The API process and the worker process both import this module.  All writes
go through short ``BEGIN IMMEDIATE`` transactions so that concurrent claims,
cancels and publishes are serialised by SQLite itself (WAL journal).
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "app.db")
MEDIA_DIR = os.path.join(DATA_DIR, "media")
ORIGINALS_DIR = os.path.join(MEDIA_DIR, "originals")
DERIVATIVES_DIR = os.path.join(MEDIA_DIR, "derivatives")
TMP_DIR = os.path.join(MEDIA_DIR, "tmp")
FLAGS_DIR = os.path.join(DATA_DIR, "flags")

MAX_UPLOAD_BYTES = 4 * 1024 * 1024  # 4 MiB hard limit for originals
ALLOWED_FORMATS = {"PNG", "JPEG"}
ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg"}
DERIVATIVE_SIZES = (256, 512)

# Job lifecycle: queued -> processing -> succeeded | failed | canceled
# "succeeded", "failed" and "canceled" are terminal.  Only one terminal
# transition may win; every transition is a conditional UPDATE guarded on
# the current status.
TERMINAL_STATUSES = ("succeeded", "failed", "canceled")
ACTIVE_STATUSES = ("queued", "processing")

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256            TEXT NOT NULL UNIQUE,
    width             INTEGER NOT NULL,
    height            INTEGER NOT NULL,
    path              TEXT NOT NULL,
    version_counter   INTEGER NOT NULL DEFAULT 0,
    published_job_id  INTEGER,
    published_version INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id    INTEGER NOT NULL REFERENCES images(id),
    crop_x      INTEGER NOT NULL,
    crop_y      INTEGER NOT NULL,
    crop_w      INTEGER NOT NULL,
    crop_h      INTEGER NOT NULL,
    version     INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    out_256     TEXT,
    out_512     TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    finished_at TEXT
);

-- One row per (image, crop) recipe: identical requests reuse the same job.
CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_recipe
    ON jobs(image_id, crop_x, crop_y, crop_w, crop_h);

CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status, id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dirs() -> None:
    for path in (DATA_DIR, ORIGINALS_DIR, DERIVATIVES_DIR, TMP_DIR, FLAGS_DIR):
        os.makedirs(path, exist_ok=True)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def tx():
    """A write transaction (BEGIN IMMEDIATE) that commits on success."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    ensure_dirs()
    with tx() as conn:
        conn.executescript(SCHEMA)
