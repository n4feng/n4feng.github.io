"""Authenticated experiment API, independent of its persistence backend."""

from flask import Blueprint, current_app, jsonify, request
from werkzeug.exceptions import BadRequest, UnsupportedMediaType

from .auth import check_access_code, issue_token, require_auth
from .storage import builtin_metrics, get_store
from .validation import custom_metric, named_record, object_value, plot_options, validate_data

api = Blueprint("api", __name__, url_prefix="/api")


def request_payload():
    if not request.is_json:
        raise UnsupportedMediaType("Use Content-Type: application/json.")
    return object_value(request.get_json(), "request")


def metric_definition(metric_id):
    if not isinstance(metric_id, str):
        raise BadRequest("metric must be the identifier of a registered metric.")
    metric = builtin_metrics().get(metric_id)
    if metric is None:
        metric = get_store().get_metric(metric_id)
    if metric is None:
        raise BadRequest(f"Unknown metric '{metric_id}'. Define it through /api/metrics first.")
    return metric


def prepare_plot(payload):
    object_value(payload, "metric entry")
    metric = metric_definition(payload.get("metric"))
    data = payload.get("data")
    series = validate_data(data, metric)
    options = plot_options(payload.get("plot", {}), metric, series)
    return {"metric_id": metric["id"], "data": data, "plot": options}


def import_records(payload):
    object_value(payload, "request")
    project = named_record(payload.get("project"), "project")
    experiment = named_record(payload.get("experiment"), "experiment")
    entries = payload.get("metrics")
    if not isinstance(entries, list) or not entries or len(entries) > 100:
        raise BadRequest("metrics must contain between 1 and 100 metric entries.")
    # Validate the whole upload before any data is staged or metadata mutated.
    prepared = [prepare_plot(entry) for entry in entries]
    return get_store().import_records(
        project, experiment, prepared,
        update_project="description" in payload["project"],
        update_experiment="description" in payload["experiment"],
    )


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
    return jsonify({"projects": get_store().list_projects()})


@api.post("/projects")
@require_auth
def create_project():
    values = named_record(request_payload(), "project")
    return jsonify(get_store().create_project(values)), 201


@api.get("/projects/<project_id>")
@require_auth
def get_project(project_id):
    return jsonify(get_store().get_project(project_id))


@api.post("/projects/<project_id>/experiments")
@require_auth
def create_experiment(project_id):
    values = named_record(request_payload(), "experiment")
    return jsonify(get_store().create_experiment(project_id, values)), 201


@api.get("/experiments/<experiment_id>")
@require_auth
def get_experiment(experiment_id):
    if "limit" in request.args or "cursor" in request.args:
        try:
            limit = int(request.args.get("limit", "20"))
            offset = int(request.args.get("cursor", "0"))
            if not 1 <= limit <= 100 or not 0 <= offset <= 2 ** 63 - 1:
                raise ValueError
        except ValueError:
            raise BadRequest("limit must be an integer from 1 to 100 and cursor must be a nonnegative integer.") from None
        return jsonify(get_store().get_experiment(experiment_id, limit=limit, offset=offset))
    return jsonify(get_store().get_experiment(experiment_id))


@api.get("/metrics")
@require_auth
def metrics():
    return jsonify({"metrics": get_store().list_metrics()})


@api.post("/metrics")
@require_auth
def create_metric():
    values = custom_metric(request_payload())
    return jsonify(get_store().create_metric(values)), 201


@api.post("/experiments/<experiment_id>/plots")
@require_auth
def create_plot(experiment_id):
    value = prepare_plot(request_payload())
    return jsonify(get_store().create_plot(experiment_id, value)), 201


@api.patch("/plots/<plot_id>")
@require_auth
def edit_plot(plot_id):
    payload = request_payload()
    if set(payload) != {"plot"}:
        raise BadRequest("A plot update must contain exactly one 'plot' object.")
    record = get_store().get_plot(plot_id)
    metric = metric_definition(record["metric_id"])
    series = validate_data(record["data"], metric)
    options = plot_options(payload["plot"], metric, series, previous=record["plot"])
    return jsonify(get_store().patch_plot(plot_id, options, previous=record["plot"]))


@api.post("/import")
@require_auth
def import_data():
    return jsonify(import_records(request_payload())), 201
