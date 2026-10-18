"""Portable backup/restore round trips through the public API contract."""

import importlib.util
from pathlib import Path
import tempfile
import unittest

from app import create_app


spec = importlib.util.spec_from_file_location(
    "tracker_data_tools", Path(__file__).resolve().parents[2] / "scripts" / "data.py"
)
data_tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(data_tools)


class ClientApi:
    def __init__(self, app):
        self.client = app.test_client()

    def request(self, method, path, payload=None):
        response = self.client.open(
            "/api" + path, method=method, json=payload,
            headers={"X-Access-Code": "test-code"},
        )
        if response.status_code >= 400:
            raise RuntimeError(response.get_json())
        return response.get_json()


class DataToolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = self.api("source")
        self.target = self.api("target")

    def api(self, name):
        return ClientApi(create_app({
            "TESTING": True, "STORAGE_BACKEND": "sqlite",
            "DATABASE_PATH": str(Path(self.directory.name) / (name + ".sqlite3")),
            "SECRET_KEY": "test-key", "ROOT_ACCESS_CODE": "test-code",
        }))

    def test_seed_is_idempotent_and_restores_full_sample(self):
        first = data_tools.seed_data(self.source)
        self.assertEqual(first["plots_created"], 1)
        self.assertEqual(data_tools.seed_data(self.source)["plots_created"], 0)
        exported = data_tools.export_data(self.source)
        self.assertNotIn("test-code", str(exported))
        self.assertEqual(exported["projects"][0]["experiments"][0]["plots"][0]["data"][0]["k"], 1)
        self.assertEqual(data_tools.restore_data(self.target, exported)["plots_created"], 1)
        restored = data_tools.export_data(self.target)
        before = exported["projects"][0]["experiments"][0]["plots"][0]
        after = restored["projects"][0]["experiments"][0]["plots"][0]
        self.assertEqual(before["data"], after["data"])
        self.assertEqual(before["plot"], after["plot"])

    def test_restore_keeps_empty_records_custom_metrics_and_duplicate_multiplicity(self):
        self.source.request("POST", "/metrics", {
            "id": "latency", "name": "Latency", "description": "Time in ms", "x_key": "batch",
        })
        empty = self.source.request("POST", "/projects", {"name": "Empty project"})
        self.source.request("POST", f"/projects/{empty['id']}/experiments", {"name": "Empty experiment"})
        data_tools.seed_data(self.source)
        exported = data_tools.export_data(self.source)
        filled = next(project for project in exported["projects"] if project["name"].startswith("HMSB"))
        experiment = filled["experiments"][0]
        plot = experiment["plots"][0]
        self.source.request("POST", f"/experiments/{experiment['id']}/plots", {
            "metric": plot["metric_id"], "data": plot["data"], "plot": plot["plot"],
        })
        backup = data_tools.export_data(self.source)
        result = data_tools.restore_data(self.target, backup)
        self.assertEqual(result["plots_created"], 2)
        self.assertEqual(result["projects_created"], 2)
        self.assertEqual(result["experiments_created"], 2)
        second = data_tools.restore_data(self.target, backup)
        self.assertEqual(second["plots_created"], 0)
        self.assertEqual(second["plots_already_present"], 2)
        self.assertIn("latency", [metric["id"] for metric in data_tools.export_data(self.target)["metrics"]])

    def test_conflicting_metric_stops_before_creating_projects(self):
        data_tools.seed_data(self.source)
        backup = data_tools.export_data(self.source)
        backup["metrics"][0]["x_key"] = "incompatible"
        with self.assertRaisesRegex(ValueError, "Conflicting metric"):
            data_tools.restore_data(self.target, backup)
        self.assertEqual(self.target.request("GET", "/projects")["projects"], [])


if __name__ == "__main__":
    unittest.main()
