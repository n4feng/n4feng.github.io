"""Storage selection and SQLite support for local development."""

import json
import sqlite3
from uuid import uuid4

from flask import current_app
from werkzeug.exceptions import Conflict, NotFound

from .db import BUILTIN_METRICS, experiment_dict, get_db, plot_dict, project_dict


def builtin_metrics():
    return {
        key: {"id": key, "name": name, "description": description, "x_key": x_key, "builtin": True}
        for key, name, description, x_key in BUILTIN_METRICS
    }


def get_store():
    if "tracker_store" not in current_app.extensions:
        if current_app.config["STORAGE_BACKEND"] == "dynamodb":
            from .dynamo import DynamoStore
            current_app.extensions["tracker_store"] = DynamoStore(current_app.config)
        else:
            current_app.extensions["tracker_store"] = SQLiteStore()
    return current_app.extensions["tracker_store"]


class SQLiteStore:
    def _find(self, table, record_id):
        row = get_db().execute(f"SELECT * FROM {table} WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            raise NotFound(f"{table.rstrip('s').capitalize()} not found.")
        return row

    def list_projects(self):
        return [project_dict(row) for row in get_db().execute("SELECT * FROM projects ORDER BY name COLLATE NOCASE, id")]

    def get_project(self, project_id):
        result = project_dict(self._find("projects", project_id))
        result["experiments"] = [experiment_dict(row) for row in get_db().execute(
            "SELECT * FROM experiments WHERE project_id = ? ORDER BY name COLLATE NOCASE, id", (project_id,)
        )]
        return result

    def create_project(self, values):
        project_id = uuid4().hex
        try:
            with get_db():
                get_db().execute("INSERT INTO projects VALUES (?, ?, ?)", (project_id, values["name"], values["description"]))
        except sqlite3.IntegrityError:
            raise Conflict("A project with this name already exists.") from None
        return project_dict(self._find("projects", project_id))

    def get_experiment(self, experiment_id, limit=None, offset=0):
        result = experiment_dict(self._find("experiments", experiment_id))
        query = "SELECT * FROM plots WHERE experiment_id = ? ORDER BY created_at, rowid"
        parameters = (experiment_id,)
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            parameters = (experiment_id, limit, offset)
        result["plots"] = [plot_dict(row) for row in get_db().execute(query, parameters)]
        if limit is not None:
            following = offset + len(result["plots"])
            result["next_cursor"] = str(following) if following < result["plot_count"] else None
        return result

    def create_experiment(self, project_id, values):
        self._find("projects", project_id)
        experiment_id = uuid4().hex
        try:
            with get_db():
                get_db().execute("INSERT INTO experiments VALUES (?, ?, ?, ?)", (experiment_id, project_id, values["name"], values["description"]))
        except sqlite3.IntegrityError:
            raise Conflict("An experiment with this name already exists in this project.") from None
        return experiment_dict(self._find("experiments", experiment_id))

    def list_metrics(self):
        return [{**dict(row), "builtin": bool(row["builtin"])} for row in get_db().execute("SELECT * FROM metrics ORDER BY builtin DESC, name COLLATE NOCASE")]

    def get_metric(self, metric_id):
        row = get_db().execute("SELECT * FROM metrics WHERE id = ?", (metric_id,)).fetchone()
        return {**dict(row), "builtin": bool(row["builtin"])} if row else None

    def create_metric(self, values):
        try:
            with get_db():
                get_db().execute("INSERT INTO metrics (id, name, description, x_key) VALUES (?, ?, ?, ?)", (values["id"], values["name"], values["description"], values["x_key"]))
        except sqlite3.IntegrityError:
            raise Conflict("A metric with this identifier already exists.") from None
        return self.get_metric(values["id"])

    def _insert_plot(self, experiment_id, value):
        plot_id = uuid4().hex
        get_db().execute("INSERT INTO plots (id, experiment_id, metric_id, data, plot) VALUES (?, ?, ?, ?, ?)", (
            plot_id, experiment_id, value["metric_id"], json.dumps(value["data"], allow_nan=False), json.dumps(value["plot"], allow_nan=False),
        ))
        return plot_id

    def create_plot(self, experiment_id, value):
        self._find("experiments", experiment_id)
        with get_db():
            plot_id = self._insert_plot(experiment_id, value)
        return self.get_plot(plot_id)

    def get_plot(self, plot_id):
        return plot_dict(self._find("plots", plot_id))

    def patch_plot(self, plot_id, options, previous=None):
        with get_db():
            get_db().execute("UPDATE plots SET plot = ? WHERE id = ?", (json.dumps(options, allow_nan=False), plot_id))
        return self.get_plot(plot_id)

    def find_experiment(self, project_name, experiment_name):
        row = get_db().execute(
            "SELECT experiments.id FROM experiments JOIN projects ON projects.id = experiments.project_id WHERE projects.name = ? AND experiments.name = ?",
            (project_name, experiment_name),
        ).fetchone()
        return self.get_experiment(row["id"]) if row else None

    def import_records(self, project, experiment, entries, update_project=False, update_experiment=False):
        db = get_db()
        with db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM projects WHERE name = ?", (project["name"],)).fetchone()
            project_id = row["id"] if row else uuid4().hex
            if row is None:
                db.execute("INSERT INTO projects VALUES (?, ?, ?)", (project_id, project["name"], project["description"]))
            row = db.execute("SELECT id FROM experiments WHERE project_id = ? AND name = ?", (project_id, experiment["name"])).fetchone()
            experiment_id = row["id"] if row else uuid4().hex
            if row is None:
                db.execute("INSERT INTO experiments VALUES (?, ?, ?, ?)", (experiment_id, project_id, experiment["name"], experiment["description"]))
            if update_project:
                db.execute("UPDATE projects SET description = ? WHERE id = ?", (project["description"], project_id))
            if update_experiment:
                db.execute("UPDATE experiments SET description = ? WHERE id = ?", (experiment["description"], experiment_id))
            plot_ids = [self._insert_plot(experiment_id, entry) for entry in entries]
        return {"project_id": project_id, "experiment_id": experiment_id, "plot_ids": plot_ids}
