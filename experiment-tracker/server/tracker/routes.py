"""Authenticated experiment API and atomic JSON import."""

import json
import sqlite3
from uuid import uuid4

from flask import Blueprint, current_app, jsonify, request
from werkzeug.exceptions import BadRequest, Conflict, NotFound, UnsupportedMediaType

from .auth import check_access_code, issue_token, require_auth
from .db import experiment_dict, get_db, metric_dict, plot_dict, project_dict
from .validation import custom_metric, named_record, object_value, plot_options, validate_data

api = Blueprint("api", __name__, url_prefix="/api")


def request_payload():
    if not request.is_json:
        raise UnsupportedMediaType("Use Content-Type: application/json.")
    return object_value(request.get_json(), "request")


def find(table, record_id):
    # Table names come only from internal constants, never from requests.
    row = get_db().execute(f"SELECT * FROM {table} WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        raise NotFound(f"{table.rstrip('s').capitalize()} not found.")
    return row


def insert_project(payload):
    values = named_record(payload, "project")
    project_id = uuid4().hex
    get_db().execute(
        "INSERT INTO projects (id, name, description) VALUES (?, ?, ?)",
        (project_id, values["name"], values["description"]),
    )
    return project_id


def insert_experiment(project_id, payload):
    values = named_record(payload, "experiment")
    experiment_id = uuid4().hex
    get_db().execute(
        "INSERT INTO experiments (id, project_id, name, description) VALUES (?, ?, ?, ?)",
        (experiment_id, project_id, values["name"], values["description"]),
    )
    return experiment_id


def insert_plot(experiment_id, payload):
    object_value(payload, "metric entry")
    metric_id = payload.get("metric")
    if not isinstance(metric_id, str):
        raise BadRequest("metric must be the identifier of a registered metric.")
    metric = get_db().execute("SELECT * FROM metrics WHERE id = ?", (metric_id,)).fetchone()
    if metric is None:
        raise BadRequest(f"Unknown metric '{metric_id}'. Define it through /api/metrics first.")
    data = payload.get("data")
    series = validate_data(data, metric)
    options = plot_options(payload.get("plot", {}), metric, series)
    plot_id = uuid4().hex
    get_db().execute(
        "INSERT INTO plots (id, experiment_id, metric_id, data, plot) VALUES (?, ?, ?, ?, ?)",
        (plot_id, experiment_id, metric_id, json.dumps(data, allow_nan=False), json.dumps(options, allow_nan=False)),
    )
    return plot_id


def import_records(payload):
    """Reuse names, append plots, and roll back the entire batch on failure."""
    object_value(payload, "request")
    project = named_record(payload.get("project"), "project")
    experiment = named_record(payload.get("experiment"), "experiment")
    entries = payload.get("metrics")
    if not isinstance(entries, list) or not entries or len(entries) > 100:
        raise BadRequest("metrics must contain between 1 and 100 metric entries.")
    db = get_db()
    with db:
        # Lock before name lookups to make concurrent import upserts predictable.
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT id FROM projects WHERE name = ?", (project["name"],)).fetchone()
        project_id = row["id"] if row else insert_project(project)
        row = db.execute(
            "SELECT id FROM experiments WHERE project_id = ? AND name = ?", (project_id, experiment["name"])
        ).fetchone()
        experiment_id = row["id"] if row else insert_experiment(project_id, experiment)
        # Explicit descriptions update existing records; omitted ones are preserved.
        if "description" in payload["project"]:
            db.execute("UPDATE projects SET description = ? WHERE id = ?", (project["description"], project_id))
        if "description" in payload["experiment"]:
            db.execute("UPDATE experiments SET description = ? WHERE id = ?", (experiment["description"], experiment_id))
        plot_ids = [insert_plot(experiment_id, entry) for entry in entries]
    return {"project_id": project_id, "experiment_id": experiment_id, "plot_ids": plot_ids}


@api.get("/health")
def health():
    return jsonify({"status": "ok"})


@api.post("/auth/session")
def login():
    check_access_code(request_payload().get("code"))
    return jsonify({"token": issue_token(), "expires_in": current_app.config["SESSION_TTL_SECONDS"], "role": "root"})


@api.get("/projects")
@require_auth
def projects():
    rows = get_db().execute("SELECT * FROM projects ORDER BY name COLLATE NOCASE, id").fetchall()
    return jsonify({"projects": [project_dict(row) for row in rows]})


@api.post("/projects")
@require_auth
def create_project():
    payload = request_payload()
    try:
        with get_db():
            project_id = insert_project(payload)
    except sqlite3.IntegrityError:
        raise Conflict("A project with this name already exists.") from None
    return jsonify(project_dict(find("projects", project_id))), 201


@api.get("/projects/<project_id>")
@require_auth
def get_project(project_id):
    result = project_dict(find("projects", project_id))
    result["experiments"] = [
        experiment_dict(row) for row in get_db().execute(
            "SELECT * FROM experiments WHERE project_id = ? ORDER BY name COLLATE NOCASE, id", (project_id,)
        ).fetchall()
    ]
    return jsonify(result)


@api.post("/projects/<project_id>/experiments")
@require_auth
def create_experiment(project_id):
    find("projects", project_id)
    payload = request_payload()
    try:
        with get_db():
            experiment_id = insert_experiment(project_id, payload)
    except sqlite3.IntegrityError:
        raise Conflict("An experiment with this name already exists in this project.") from None
    return jsonify(experiment_dict(find("experiments", experiment_id))), 201


@api.get("/experiments/<experiment_id>")
@require_auth
def get_experiment(experiment_id):
    result = experiment_dict(find("experiments", experiment_id))
    result["plots"] = [
        plot_dict(row) for row in get_db().execute(
            "SELECT * FROM plots WHERE experiment_id = ? ORDER BY created_at, rowid", (experiment_id,)
        ).fetchall()
    ]
    return jsonify(result)


@api.get("/metrics")
@require_auth
def metrics():
    rows = get_db().execute("SELECT * FROM metrics ORDER BY builtin DESC, name COLLATE NOCASE").fetchall()
    return jsonify({"metrics": [metric_dict(row) for row in rows]})


@api.post("/metrics")
@require_auth
def create_metric():
    metric = custom_metric(request_payload())
    try:
        with get_db():
            get_db().execute(
                "INSERT INTO metrics (id, name, description, x_key) VALUES (?, ?, ?, ?)",
                (metric["id"], metric["name"], metric["description"], metric["x_key"]),
            )
    except sqlite3.IntegrityError:
        raise Conflict("A metric with this identifier already exists.") from None
    return jsonify(metric_dict(find("metrics", metric["id"]))), 201


@api.post("/experiments/<experiment_id>/plots")
@require_auth
def create_plot(experiment_id):
    find("experiments", experiment_id)
    payload = request_payload()
    with get_db():
        plot_id = insert_plot(experiment_id, payload)
    return jsonify(plot_dict(find("plots", plot_id))), 201


@api.patch("/plots/<plot_id>")
@require_auth
def edit_plot(plot_id):
    record = find("plots", plot_id)
    payload = request_payload()
    if set(payload) != {"plot"}:
        raise BadRequest("A plot update must contain exactly one 'plot' object.")
    metric = find("metrics", record["metric_id"])
    series = validate_data(json.loads(record["data"]), metric)
    options = plot_options(payload["plot"], metric, series, previous=json.loads(record["plot"]))
    with get_db():
        get_db().execute("UPDATE plots SET plot = ? WHERE id = ?", (json.dumps(options, allow_nan=False), plot_id))
    return jsonify(plot_dict(find("plots", plot_id)))


@api.post("/import")
@require_auth
def import_data():
    return jsonify(import_records(request_payload())), 201
