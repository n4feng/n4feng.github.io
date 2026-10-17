"""Shared request validation for website and API imports."""

import math
import re

from werkzeug.exceptions import BadRequest

IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
COLOR = re.compile(r"^(#[0-9a-fA-F]{3,8}|[a-zA-Z]{1,30}|rgba?\([0-9.,% ]+\)|hsla?\([0-9.,% ]+\))$")
PLOT_FIELDS = {
    "type", "title", "description", "x_key", "x_label", "y_label",
    "y_min", "y_max", "series_labels", "colors",
}


def fail(message):
    raise BadRequest(message)


def object_value(value, label):
    if not isinstance(value, dict):
        fail(f"{label} must be a JSON object.")
    return value


def text_value(value, label, *, required=False, limit=5000):
    if not isinstance(value, str):
        fail(f"{label} must be text.")
    value = value.strip()
    if required and not value:
        fail(f"{label} must not be empty.")
    if len(value) > limit:
        fail(f"{label} must be at most {limit} characters.")
    return value


def named_record(payload, label):
    object_value(payload, label)
    return {
        "name": text_value(payload.get("name"), f"{label}.name", required=True, limit=200),
        "description": text_value(payload.get("description", ""), f"{label}.description"),
    }


def custom_metric(payload):
    result = named_record(payload, "metric")
    metric_id = payload.get("id")
    if not isinstance(metric_id, str) or not IDENTIFIER.fullmatch(metric_id):
        fail("metric.id must start with a lowercase letter and contain only lowercase letters, digits, or underscores (maximum 64 characters).")
    x_key = payload.get("x_key", "x")
    if not isinstance(x_key, str) or not IDENTIFIER.fullmatch(x_key):
        fail("metric.x_key must be a lowercase identifier (maximum 64 characters).")
    return {"id": metric_id, **result, "x_key": x_key}


def finite_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_data(data, metric):
    if not isinstance(data, list) or not data or len(data) > 10000:
        fail("data must contain between 1 and 10000 rows.")
    x_key = metric["x_key"]
    seen_x = set()
    series = set()
    for index, row in enumerate(data):
        object_value(row, f"data[{index}]")
        if x_key not in row:
            fail(f"data[{index}] is missing the x-axis field '{x_key}'.")
        x = row[x_key]
        if metric["id"] == "ndcg_at_k":
            if not isinstance(x, int) or not finite_number(x) or x < 1:
                fail(f"data[{index}].k must be a positive integer.")
        elif not (finite_number(x) or isinstance(x, str) and x.strip()):
            fail(f"data[{index}].{x_key} must be a finite number or nonempty text.")
        if isinstance(x, str) and len(x) > 200:
            fail(f"data[{index}].{x_key} must be at most 200 characters.")
        # Numeric 1 and 1.0 are the same coordinate; string '1' remains distinct.
        coordinate = ("text" if isinstance(x, str) else "number", x)
        if coordinate in seen_x:
            fail(f"Duplicate x-axis value '{x}' in data.")
        seen_x.add(coordinate)
        row_series = set(row) - {x_key}
        if not row_series:
            fail(f"data[{index}] must contain at least one numeric score in addition to '{x_key}'.")
        for key in row_series:
            if not key.strip() or len(key) > 100:
                fail("Series names must contain between 1 and 100 characters.")
            value = row[key]
            if not finite_number(value):
                fail(f"data[{index}].{key} must be a finite numeric score, not a boolean or null.")
            if metric["builtin"] and not 0 <= value <= 1:
                fail(f"data[{index}].{key} must be between 0 and 1 for {metric['id']}.")
        series.update(row_series)
        if len(series) > 64:
            fail("A plot may contain at most 64 score series.")
    return series


def plot_options(value, metric, series, previous=None):
    value = object_value(value, "plot")
    unknown = set(value) - PLOT_FIELDS
    if unknown:
        fail(f"Unknown plot options: {', '.join(sorted(unknown))}.")
    result = {
        "type": "line",
        "title": metric["name"],
        "description": "",
        "x_key": metric["x_key"],
        "x_label": "k" if metric["id"] == "ndcg_at_k" else metric["x_key"],
        "y_label": metric["name"],
        "series_labels": {},
        "colors": {},
        **(previous or {}),
        **value,
    }
    if result["type"] not in ("bar", "line"):
        fail("plot.type must be 'bar' or 'line'.")
    if result["x_key"] != metric["x_key"]:
        fail(f"plot.x_key must match metric.x_key ('{metric['x_key']}').")
    for key in ("title", "description", "x_label", "y_label"):
        result[key] = text_value(result[key], f"plot.{key}", limit=5000 if key == "description" else 300)
    for key in ("y_min", "y_max"):
        if key in result and result[key] is not None and not finite_number(result[key]):
            fail(f"plot.{key} must be a finite number or null.")
    if result.get("y_min") is not None and result.get("y_max") is not None and result["y_min"] >= result["y_max"]:
        fail("plot.y_max must be greater than plot.y_min.")
    for key in ("series_labels", "colors"):
        mapping = object_value(result[key], f"plot.{key}")
        if set(mapping) - series:
            fail(f"plot.{key} contains unknown series names.")
        cleaned = {}
        for series_key, label in mapping.items():
            cleaned[series_key] = text_value(label, f"plot.{key}.{series_key}", required=True, limit=100)
            if key == "colors" and not COLOR.fullmatch(cleaned[series_key]):
                fail("Plot colors must be CSS color names, hex, rgb(), or hsl() values.")
        result[key] = cleaned
    return result
