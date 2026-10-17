"""Experiment tracker application factory."""

import json
import os
import secrets
import tempfile
from pathlib import Path

import click
from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from .db import close_db, get_db, init_db
from .routes import api, import_records
from .validation import plot_options, validate_data


def persistent_secret(database_path):
    """Keep sessions valid across restarts and multiple Gunicorn workers."""
    secret_path = Path(f"{database_path}.secret")
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    if not secret_path.exists():
        # Publish only a completely written file so simultaneous workers cannot
        # observe an empty key or choose different signing keys.
        with tempfile.NamedTemporaryFile(mode="w", dir=secret_path.parent, delete=False) as handle:
            temporary_path = Path(handle.name)
            handle.write(secrets.token_hex(32))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, secret_path)
        except FileExistsError:
            pass
        finally:
            temporary_path.unlink(missing_ok=True)
    secret = secret_path.read_text().strip()
    if not secret:
        raise RuntimeError(f"Empty signing-key file: {secret_path}. Configure SECRET_KEY or recreate the file.")
    return secret


def create_app(config=None):
    app = Flask(__name__, static_folder=None)
    server_root = Path(__file__).resolve().parents[1]
    app.config.from_mapping(
        DATABASE_PATH=os.environ.get("DATABASE_PATH", str(server_root / "instance" / "experiments.sqlite3")),
        ROOT_ACCESS_CODE=os.environ.get("ROOT_ACCESS_CODE", "666666"),
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        CORS_ORIGINS=os.environ.get("CORS_ORIGINS", "https://n4feng.github.io,http://localhost:5173,http://127.0.0.1:5173"),
        SESSION_TTL_SECONDS=int(os.environ.get("SESSION_TTL_SECONDS", "43200")),
        AUTH_RATE_MAX_FAILURES=int(os.environ.get("AUTH_RATE_MAX_FAILURES", "5")),
        AUTH_RATE_WINDOW_SECONDS=int(os.environ.get("AUTH_RATE_WINDOW_SECONDS", "60")),
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,
    )
    if config:
        app.config.update(config)
    if not isinstance(app.config["ROOT_ACCESS_CODE"], str) or not app.config["ROOT_ACCESS_CODE"]:
        raise ValueError("ROOT_ACCESS_CODE must be a nonempty string.")
    if app.config["DATABASE_PATH"] == ":memory:":
        raise ValueError("DATABASE_PATH must name a persistent SQLite file.")
    app.config["DATABASE_PATH"] = str(Path(app.config["DATABASE_PATH"]).expanduser().resolve())
    if not app.config["SECRET_KEY"]:
        app.config["SECRET_KEY"] = persistent_secret(app.config["DATABASE_PATH"])
    if not isinstance(app.config["SECRET_KEY"], str):
        raise ValueError("SECRET_KEY must be a string.")
    for key in ("SESSION_TTL_SECONDS", "AUTH_RATE_MAX_FAILURES", "AUTH_RATE_WINDOW_SECONDS"):
        if app.config[key] < 1:
            raise ValueError(f"{key} must be positive.")
    origins = app.config["CORS_ORIGINS"]
    allowed_origins = set(part.strip() for part in origins.split(",") if part.strip()) if isinstance(origins, str) else set(origins)
    if "*" in allowed_origins or "null" in allowed_origins:
        raise ValueError("CORS_ORIGINS must list exact trusted origins, without '*' or 'null'.")

    @app.before_request
    def preflight():
        if request.method == "OPTIONS" and request.path.startswith("/api/"):
            return "", 204

    @app.after_request
    def response_headers(response):
        origin = request.headers.get("Origin")
        if origin in allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-Access-Code"
            response.headers["Access-Control-Max-Age"] = "600"
        response.vary.add("Origin")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        response = error.get_response()
        response.data = app.json.dumps({"error": error.description})
        response.content_type = "application/json"
        return response

    @app.errorhandler(Exception)
    def unexpected_error(error):
        if app.testing:
            raise error
        app.logger.exception("Unhandled API error")
        return jsonify({"error": "An internal server error occurred."}), 500

    app.register_blueprint(api)
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()

    @app.cli.command("init-db")
    def initialize_command():
        """Create tables and built-in metrics without deleting existing records."""
        init_db()
        click.echo("Database initialized.")

    @app.cli.command("seed-example")
    @click.option("--file", "file_path", type=click.Path(exists=True, path_type=Path), default=server_root.parent / "examples" / "teacher-recall-bar.json", show_default=True)
    def seed_example(file_path):
        """Import the example once; matching seeded plots are not duplicated."""
        payload = json.loads(file_path.read_text())
        db = get_db()
        existing = db.execute(
            "SELECT experiments.id FROM experiments JOIN projects ON projects.id = experiments.project_id WHERE projects.name = ? AND experiments.name = ?",
            (payload["project"]["name"], payload["experiment"]["name"]),
        ).fetchone()
        remaining = []
        for entry in payload["metrics"]:
            metric = db.execute("SELECT * FROM metrics WHERE id = ?", (entry["metric"],)).fetchone()
            if metric is None:
                raise click.ClickException(f"Unknown metric: {entry['metric']}")
            options = plot_options(entry.get("plot", {}), metric, validate_data(entry["data"], metric))
            candidates = db.execute(
                "SELECT data, plot FROM plots WHERE experiment_id = ? AND metric_id = ?",
                (existing["id"], entry["metric"]),
            ).fetchall() if existing else []
            if not any(json.loads(row["data"]) == entry["data"] and json.loads(row["plot"]) == options for row in candidates):
                remaining.append(entry)
        if not remaining:
            click.echo("Example already seeded; no records changed.")
            return
        result = import_records({**payload, "metrics": remaining})
        click.echo(json.dumps(result))

    return app
