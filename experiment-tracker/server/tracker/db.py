"""SQLite storage and response serialization."""

import json
import sqlite3
import time
from pathlib import Path

from flask import current_app, g

BUILTIN_METRICS = [
    ("ndcg_at_k", "NDCG@k", "Normalized discounted cumulative gain at cutoff k.", "k"),
    ("f1", "F1", "Harmonic mean of precision and recall.", "x"),
    ("precision", "Precision", "Fraction of retrieved or predicted positives that are relevant.", "x"),
    ("recall", "Recall", "Fraction of relevant items that are retrieved or predicted positive.", "x"),
    ("accuracy", "Accuracy", "Fraction of predictions that are correct.", "x"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT '',
    UNIQUE (project_id, name)
);
CREATE TABLE IF NOT EXISTS metrics (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    x_key TEXT NOT NULL,
    builtin INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS plots (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    metric_id TEXT NOT NULL REFERENCES metrics(id),
    data TEXT NOT NULL,
    plot TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS experiments_project ON experiments(project_id);
CREATE INDEX IF NOT EXISTS plots_experiment ON plots(experiment_id);
CREATE TABLE IF NOT EXISTS auth_failures (
    client TEXT NOT NULL,
    occurred_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS auth_failures_client_time ON auth_failures(client, occurred_at);
"""


def get_db():
    if "db" not in g:
        connection = sqlite3.connect(current_app.config["DATABASE_PATH"], timeout=15)
        connection.create_collation("NOCASE", lambda left, right: (left.casefold() > right.casefold()) - (left.casefold() < right.casefold()))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        g.db = connection
    return g.db


def close_db(_error=None):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def init_db():
    Path(current_app.config["DATABASE_PATH"]).parent.mkdir(parents=True, exist_ok=True)
    connection = get_db()
    # SQLite's journal-mode transition can fail immediately during simultaneous
    # first startup, even with the connection busy timeout. Retry that transition.
    for attempt in range(30):
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            break
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or attempt == 29:
                raise
            time.sleep(0.1)
    connection.executescript(SCHEMA)
    connection.executemany(
        "INSERT OR IGNORE INTO metrics (id, name, description, x_key, builtin) VALUES (?, ?, ?, ?, 1)",
        BUILTIN_METRICS,
    )
    connection.commit()


def metric_dict(row):
    result = dict(row)
    result["builtin"] = bool(result["builtin"])
    return result


def plot_dict(row):
    return {
        "id": row["id"],
        "experiment_id": row["experiment_id"],
        "metric_id": row["metric_id"],
        "data": json.loads(row["data"]),
        "plot": json.loads(row["plot"]),
        "created_at": row["created_at"],
    }


def project_dict(row):
    result = dict(row)
    result["experiment_count"] = get_db().execute(
        "SELECT COUNT(*) FROM experiments WHERE project_id = ?", (row["id"],)
    ).fetchone()[0]
    return result


def experiment_dict(row):
    result = dict(row)
    result["plot_count"] = get_db().execute(
        "SELECT COUNT(*) FROM plots WHERE experiment_id = ?", (row["id"],)
    ).fetchone()[0]
    return result
