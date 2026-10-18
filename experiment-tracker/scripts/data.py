#!/usr/bin/env python3
"""Seed, export and merge-restore tracker data through its authenticated API."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import getpass
import gzip
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


class Api:
    def __init__(self, url, code):
        self.url = url.rstrip("/")
        parsed = urlsplit(self.url)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("Use an HTTPS API origin (HTTP is allowed only on localhost).")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("Supply the API origin without /api, credentials, or query parameters.")
        self.token = None
        self.token = self.request("POST", "/auth/session", {"code": code})["token"]

    def request(self, method, path, payload=None):
        headers = {"Accept": "application/json", "Accept-Encoding": "gzip"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, allow_nan=False).encode("utf-8")
        request = Request(self.url + "/api" + path, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=90) as response:
                return response_json(response)
        except HTTPError as error:
            try:
                message = response_json(error).get("error", error.reason)
            except (ValueError, AttributeError):
                message = error.reason
            raise RuntimeError(f"API {method} {path}: HTTP {error.code}: {message}") from None


def record_path(kind, record_id):
    return "/" + kind + "/" + quote(record_id, safe="")


def response_json(response):
    content = response.read()
    if response.headers.get("Content-Encoding", "").lower() == "gzip":
        content = gzip.decompress(content)
    return json.loads(content)


def read_experiment(api, experiment_id):
    """Fetch one plot per response to fit Lambda's buffered response limit."""
    path = record_path("experiments", experiment_id)
    result = api.request("GET", path + "?limit=1")
    cursor = result.pop("next_cursor", None)
    seen = set()
    while cursor is not None:
        if cursor in seen:
            raise RuntimeError("API returned a repeated pagination cursor.")
        seen.add(cursor)
        page = api.request("GET", path + "?limit=1&cursor=" + quote(str(cursor), safe=""))
        result["plots"].extend(page["plots"])
        cursor = page.get("next_cursor")
    return result


def export_data(api):
    """Return portable data, excluding credentials, tokens and internal DB items."""
    result = {
        "format": "experiment-tracker",
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "metrics": api.request("GET", "/metrics")["metrics"],
        "projects": [],
    }
    for project in api.request("GET", "/projects")["projects"]:
        detail = api.request("GET", record_path("projects", project["id"]))
        detail["experiments"] = [
            read_experiment(api, item["id"])
            for item in detail["experiments"]
        ]
        result["projects"].append(detail)
    return result


def identity(record):
    return {"name": record["name"], "description": record.get("description", "")}


def fingerprint(plot):
    return json.dumps(
        [plot["metric_id"], plot["data"], plot["plot"]],
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    )


def validate_backup(data):
    if not isinstance(data, dict) or data.get("format") != "experiment-tracker" or data.get("version") != 1:
        raise ValueError("Not an experiment-tracker version 1 export.")
    if not isinstance(data.get("metrics"), list) or not isinstance(data.get("projects"), list):
        raise ValueError("Export must contain metrics and projects arrays.")
    for metric in data["metrics"]:
        if not isinstance(metric, dict) or not all(isinstance(metric.get(key), str) for key in ("id", "name", "x_key", "description")):
            raise ValueError("Invalid metric in export.")
    for project in data["projects"]:
        if not isinstance(project, dict) or not isinstance(project.get("name"), str) or not isinstance(project.get("experiments"), list):
            raise ValueError("Invalid project in export.")
        for experiment in project["experiments"]:
            if not isinstance(experiment, dict) or not isinstance(experiment.get("name"), str) or not isinstance(experiment.get("plots"), list):
                raise ValueError("Invalid experiment in export.")
            for plot in experiment["plots"]:
                if not isinstance(plot, dict) or not isinstance(plot.get("metric_id"), str) or not isinstance(plot.get("data"), list) or not isinstance(plot.get("plot"), dict):
                    raise ValueError("Invalid plot in export.")
                fingerprint(plot)


def restore_data(api, data):
    """Merge by names; preserve existing records and intentional duplicate plots."""
    validate_backup(data)
    metrics = {item["id"]: item for item in api.request("GET", "/metrics")["metrics"]}
    # Resolve incompatible metric definitions before writing any records.
    for metric in data["metrics"]:
        existing = metrics.get(metric["id"])
        if existing and any(existing.get(key) != metric.get(key) for key in ("name", "description", "x_key", "builtin")):
            raise ValueError("Conflicting metric definition: " + metric["id"])
        if not existing and metric.get("builtin"):
            raise ValueError("Target is missing built-in metric: " + metric["id"])
    for metric in data["metrics"]:
        if metric["id"] not in metrics:
            api.request("POST", "/metrics", {key: metric[key] for key in ("id", "name", "description", "x_key")})
    projects = {item["name"].casefold(): item for item in api.request("GET", "/projects")["projects"]}
    counts = {"projects_created": 0, "experiments_created": 0, "plots_created": 0, "plots_already_present": 0}
    for saved_project in data["projects"]:
        key = saved_project["name"].casefold()
        project = projects.get(key)
        if project is None:
            project = api.request("POST", "/projects", identity(saved_project))
            projects[key] = project
            counts["projects_created"] += 1
        project_url = record_path("projects", project["id"])
        experiments = {item["name"].casefold(): item for item in api.request("GET", project_url)["experiments"]}
        for saved_experiment in saved_project["experiments"]:
            key = saved_experiment["name"].casefold()
            experiment = experiments.get(key)
            if experiment is None:
                experiment = api.request("POST", project_url + "/experiments", identity(saved_experiment))
                experiments[key] = experiment
                counts["experiments_created"] += 1
            experiment_url = record_path("experiments", experiment["id"])
            existing_plots = read_experiment(api, experiment["id"])["plots"]
            available = Counter(fingerprint(plot) for plot in existing_plots)
            for plot in saved_experiment["plots"]:
                signature = fingerprint(plot)
                if available[signature]:
                    available[signature] -= 1
                    counts["plots_already_present"] += 1
                    continue
                api.request("POST", experiment_url + "/plots", {
                    "metric": plot["metric_id"], "data": plot["data"], "plot": plot["plot"],
                })
                counts["plots_created"] += 1
    return counts


def seed_data(api):
    sample = Path(__file__).resolve().parents[1] / "examples" / "teacher-recall-bar.json"
    payload = json.loads(sample.read_text())
    all_metrics = api.request("GET", "/metrics")["metrics"]
    used = {entry["metric"] for entry in payload["metrics"]}
    portable = {
        "format": "experiment-tracker", "version": 1,
        "metrics": [metric for metric in all_metrics if metric["id"] in used],
        "projects": [{**payload["project"], "experiments": [{
            **payload["experiment"],
            "plots": [{"metric_id": entry["metric"], "data": entry["data"], "plot": entry["plot"]} for entry in payload["metrics"]],
        }]}],
    }
    return restore_data(api, portable)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("API_BASE_URL"), help="HTTPS API origin; or set API_BASE_URL")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("seed", help="Add the teacher recall example without duplicates")
    export = commands.add_parser("export", help="Download all data to a new private JSON file")
    export.add_argument("--output", type=Path, required=True)
    restore = commands.add_parser("restore", help="Merge an export into the target API; existing data is preserved")
    restore.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    if not args.url:
        parser.error("Supply --url or set API_BASE_URL.")
    try:
        backup = None
        if args.command == "restore":
            backup = json.loads(args.input.read_text(encoding="utf-8"))
            validate_backup(backup)
        if args.command == "export" and args.output.exists():
            raise ValueError("Output already exists; choose a new filename to preserve the previous backup.")
        code = os.environ.get("ROOT_ACCESS_CODE") or getpass.getpass("Access code: ")
        api = Api(args.url, code)
        if args.command == "seed":
            print(json.dumps(seed_data(api), indent=2))
        elif args.command == "restore":
            print(json.dumps(restore_data(api, backup), indent=2))
        else:
            data = export_data(api)
            content = json.dumps(data, indent=2, allow_nan=False) + "\n"
            descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(content)
            print("Export saved: " + str(args.output))
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
