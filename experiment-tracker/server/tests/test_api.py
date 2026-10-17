import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tracker import create_app
from tracker.auth import issue_token, serializer
from tracker.db import get_db


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = str(Path(self.directory.name) / "experiments.sqlite3")
        self.config = {"TESTING": True, "DATABASE_PATH": self.database_path, "ROOT_ACCESS_CODE": "666666"}
        self.app = create_app(self.config)
        self.client = self.app.test_client()
        self.headers = {"X-Access-Code": "666666"}
        self.sample = {
            "project": {"name": "HMSB Document Retrieval Application", "description": "Teacher study"},
            "experiment": {"name": "teacher recall", "description": "NDCG comparison"},
            "metrics": [{
                "metric": "ndcg_at_k",
                "data": [
                    {"k": 1, "cosine_ndcg": 0.5138458002987476, "base_ndcg": 0.5752039526600023, "weighted_ndcg": 0.6070320579110652},
                    {"k": 3, "cosine_ndcg": 0.6768888758703948, "base_ndcg": 0.7250525992014297, "weighted_ndcg": 0.7556947189052264},
                    {"k": 5, "cosine_ndcg": 0.7204136032625917, "base_ndcg": 0.7620344135559811, "weighted_ndcg": 0.7900924993035837},
                ],
                "plot": {
                    "type": "bar", "title": "Teacher NDCG", "description": "Held-out questions.",
                    "x_label": "Retrieval cutoff k", "y_label": "NDCG", "y_min": 0, "y_max": 1,
                    "series_labels": {"weighted_ndcg": "Weighted PPL"}, "colors": {"weighted_ndcg": "#1f77b4"},
                },
            }],
        }

    def tearDown(self):
        self.directory.cleanup()

    def upload(self, payload=None):
        return self.client.post("/api/import", json=payload or self.sample, headers=self.headers)

    def test_every_data_endpoint_requires_authentication(self):
        endpoints = [
            ("get", "/api/projects"), ("post", "/api/projects"),
            ("get", "/api/projects/missing"), ("post", "/api/projects/missing/experiments"),
            ("get", "/api/experiments/missing"), ("get", "/api/metrics"),
            ("post", "/api/metrics"), ("post", "/api/experiments/missing/plots"),
            ("patch", "/api/plots/missing"), ("post", "/api/import"),
        ]
        for method, path in endpoints:
            with self.subTest(method=method, path=path):
                response = getattr(self.client, method)(path, json={})
                self.assertEqual(response.status_code, 401)
                self.assertIn("error", response.json)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.client.get("/api/health").json, {"status": "ok"})

    def test_login_accepts_root_code_and_rejects_wrong_or_numeric_code(self):
        for code in ("incorrect", 666666, None):
            self.assertEqual(self.client.post("/api/auth/session", json={"code": code}).status_code, 401)
        response = self.client.post("/api/auth/session", json={"code": "666666"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["expires_in"], 43200)
        token = response.json["token"]
        auth = {"Authorization": f"Bearer {token}"}
        self.assertEqual(self.client.get("/api/metrics", headers=auth).status_code, 200)
        self.assertEqual(self.client.post("/api/projects", json={"name": "Session project"}, headers=auth).status_code, 201)
        self.assertEqual(self.client.get("/api/projects", headers={"Authorization": f"Bearer {token}bad"}).status_code, 401)

    def test_expired_malformed_and_rotated_tokens_are_rejected(self):
        with self.app.app_context():
            token = issue_token()
            malformed = serializer().dumps(["root"])
            with patch("itsdangerous.timed.TimestampSigner.get_timestamp", return_value=1):
                expired = issue_token()
        response = self.client.get("/api/projects", headers={"Authorization": f"Bearer {expired}"})
        self.assertEqual(response.status_code, 401)
        self.assertIn("expired", response.json["error"])
        self.assertEqual(self.client.get("/api/projects", headers={"Authorization": f"Bearer {malformed}"}).status_code, 401)
        self.app.config["ROOT_ACCESS_CODE"] = "new-secret"
        self.assertEqual(self.client.get("/api/projects", headers={"Authorization": f"Bearer {token}"}).status_code, 401)
        self.assertEqual(self.client.get("/api/projects", headers=self.headers).status_code, 401)
        self.assertEqual(self.client.get("/api/projects", headers={"X-Access-Code": "new-secret"}).status_code, 200)

    def test_failure_limit_covers_login_and_direct_code_across_workers(self):
        for _ in range(3):
            self.assertEqual(self.client.post("/api/auth/session", json={"code": "bad"}).status_code, 401)
        for _ in range(2):
            self.assertEqual(self.client.get("/api/projects", headers={"X-Access-Code": "bad"}).status_code, 401)
        second_worker = create_app(self.config).test_client()
        response = second_worker.post("/api/auth/session", json={"code": "666666"})
        self.assertEqual(response.status_code, 429)
        self.assertGreaterEqual(int(response.headers["Retry-After"]), 1)
        # Caller-controlled forwarding headers cannot reset the source address.
        self.assertEqual(second_worker.get("/api/projects", headers={**self.headers, "X-Forwarded-For": "192.0.2.8"}).status_code, 429)
        with self.app.app_context():
            with get_db():
                get_db().execute("UPDATE auth_failures SET occurred_at = occurred_at - 61")
        self.assertEqual(second_worker.get("/api/projects", headers=self.headers).status_code, 200)

    def test_import_roundtrips_all_series_precision_and_plot_metadata(self):
        response = self.upload()
        self.assertEqual(response.status_code, 201, response.json)
        result = response.json
        project = self.client.get(f"/api/projects/{result['project_id']}", headers=self.headers).json
        self.assertEqual(project["experiment_count"], 1)
        self.assertEqual(project["experiments"][0]["plot_count"], 1)
        experiment = self.client.get(f"/api/experiments/{result['experiment_id']}", headers=self.headers).json
        stored = experiment["plots"][0]
        self.assertEqual(stored["metric_id"], "ndcg_at_k")
        self.assertEqual(stored["data"], self.sample["metrics"][0]["data"])
        for key, value in self.sample["metrics"][0]["plot"].items():
            self.assertEqual(stored["plot"][key], value)
        self.assertEqual(stored["plot"]["x_key"], "k")

    def test_import_reuses_project_and_experiment_and_appends_plots(self):
        first = self.upload().json
        second_payload = copy.deepcopy(self.sample)
        del second_payload["project"]["description"]
        del second_payload["experiment"]["description"]
        second = self.upload(second_payload).json
        self.assertEqual(first["project_id"], second["project_id"])
        self.assertEqual(first["experiment_id"], second["experiment_id"])
        result = self.client.get(f"/api/experiments/{first['experiment_id']}", headers=self.headers).json
        self.assertEqual(result["plot_count"], 2)
        self.assertEqual(result["description"], "NDCG comparison")

    def test_atomic_import_rolls_back_new_records_and_existing_edits(self):
        bad_payload = copy.deepcopy(self.sample)
        bad_payload["metrics"].append({"metric": "unknown", "data": [{"x": 1, "value": 0.5}]})
        self.assertEqual(self.upload(bad_payload).status_code, 400)
        self.assertEqual(self.client.get("/api/projects", headers=self.headers).json["projects"], [])
        first = self.upload().json
        bad_payload["project"]["description"] = "Should roll back"
        bad_payload["experiment"]["description"] = "Should roll back"
        self.assertEqual(self.upload(bad_payload).status_code, 400)
        project = self.client.get(f"/api/projects/{first['project_id']}", headers=self.headers).json
        experiment = self.client.get(f"/api/experiments/{first['experiment_id']}", headers=self.headers).json
        self.assertEqual(project["description"], "Teacher study")
        self.assertEqual(experiment["description"], "NDCG comparison")
        self.assertEqual(experiment["plot_count"], 1)

    def test_ndcg_validation_rejects_invalid_rows_without_mutating_database(self):
        invalid_datasets = [
            [], [{"k": 1}], [{"value": 0.5}], [{"k": 0, "value": 0.5}],
            [{"k": 1.5, "value": 0.5}], [{"k": True, "value": 0.5}],
            [{"k": 1, "value": True}], [{"k": 1, "value": None}],
            [{"k": 1, "value": "0.5"}], [{"k": 1, "value": float("nan")}],
            [{"k": 1, "value": float("inf")}], [{"k": 1, "value": 10 ** 400}],
            [{"k": 10 ** 400, "value": 0.5}], [{"k": 1, "value": 1.1}],
            [{"k": 1, "value": -0.1}], [{"k": 1, "a": 0.1}, {"k": 1, "b": 0.2}],
        ]
        for data in invalid_datasets:
            with self.subTest(data=data):
                payload = copy.deepcopy(self.sample)
                payload["metrics"][0]["data"] = data
                response = self.upload(payload)
                self.assertEqual(response.status_code, 400, response.json)
                self.assertIn("error", response.json)
        self.assertEqual(self.client.get("/api/projects", headers=self.headers).json["projects"], [])

    def test_sparse_dynamic_series_are_preserved_and_plot_defaults_work(self):
        payload = copy.deepcopy(self.sample)
        rows = [{"k": 1, "a": 0.1}, {"k": 3, "a": 0.2, "new_method": 0.4}, {"k": 5, "new_method": 0.6}]
        payload["metrics"][0] = {"metric": "ndcg_at_k", "data": rows}
        result = self.upload(payload)
        self.assertEqual(result.status_code, 201, result.json)
        experiment = self.client.get(f"/api/experiments/{result.json['experiment_id']}", headers=self.headers).json
        self.assertEqual(experiment["plots"][0]["data"], rows)
        self.assertEqual(experiment["plots"][0]["plot"]["type"], "line")

    def test_create_hierarchy_custom_metric_and_unbounded_scores(self):
        project = self.client.post("/api/projects", json={"name": "Training", "description": "Runs"}, headers=self.headers)
        self.assertEqual(project.status_code, 201)
        experiment = self.client.post(f"/api/projects/{project.json['id']}/experiments", json={"name": "Loss"}, headers=self.headers)
        self.assertEqual(experiment.status_code, 201)
        metric = self.client.post("/api/metrics", json={"id": "loss", "name": "Loss", "description": "Training loss", "x_key": "epoch"}, headers=self.headers)
        self.assertEqual(metric.status_code, 201)
        self.assertFalse(metric.json["builtin"])
        result = self.client.post(f"/api/experiments/{experiment.json['id']}/plots", json={"metric": "loss", "data": [{"epoch": "initial", "train": 4.6}, {"epoch": "final", "train": -0.2}], "plot": {"type": "line"}}, headers=self.headers)
        self.assertEqual(result.status_code, 201, result.json)
        self.assertEqual(result.json["plot"]["x_key"], "epoch")
        metrics = self.client.get("/api/metrics", headers=self.headers).json["metrics"]
        self.assertEqual({entry["id"] for entry in metrics}, {"ndcg_at_k", "f1", "precision", "recall", "accuracy", "loss"})
        self.assertEqual(self.client.post("/api/metrics", json={"id": "ndcg_at_k", "name": "Override"}, headers=self.headers).status_code, 409)
        self.assertEqual(self.client.post("/api/projects", json={"name": "training"}, headers=self.headers).status_code, 409)

    def test_plot_updates_preserve_data_and_validate_options(self):
        result = self.upload().json
        plot_id = result["plot_ids"][0]
        updated = self.client.patch(f"/api/plots/{plot_id}", json={"plot": {"type": "line", "description": "Updated caption"}}, headers=self.headers)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json["plot"]["description"], "Updated caption")
        self.assertEqual(updated.json["plot"]["type"], "line")
        self.assertEqual(updated.json["data"], self.sample["metrics"][0]["data"])
        for options in ({"type": "pie"}, {"y_min": 2, "y_max": 1}, {"x_key": "wrong"}, {"colors": {"no_such_series": "red"}}, {"typo": 1}):
            with self.subTest(options=options):
                response = self.client.patch(f"/api/plots/{plot_id}", json={"plot": options}, headers=self.headers)
                self.assertEqual(response.status_code, 400, response.json)

    def test_persistence_and_signing_key_survive_app_restart(self):
        result = self.upload().json
        token = self.client.post("/api/auth/session", json={"code": "666666"}).json["token"]
        restarted = create_app(self.config)
        self.assertEqual(self.app.config["SECRET_KEY"], restarted.config["SECRET_KEY"])
        response = restarted.test_client().get(f"/api/experiments/{result['experiment_id']}", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["plot_count"], 1)
        self.assertEqual(Path(f"{self.database_path}.secret").stat().st_mode & 0o777, 0o600)

    def test_cors_allows_only_exact_trusted_origins_without_credentials(self):
        for origin in ("https://n4feng.github.io", "http://localhost:5173", "http://127.0.0.1:5173"):
            response = self.client.options("/api/projects", headers={"Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"})
            self.assertEqual(response.status_code, 204)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], origin)
            self.assertNotIn("Access-Control-Allow-Credentials", response.headers)
            self.assertIn("Authorization", response.headers["Access-Control-Allow-Headers"])
        for origin in ("https://evil.example", "https://n4feng.github.io.evil.example", "null"):
            response = self.client.get("/api/projects", headers={**self.headers, "Origin": origin})
            self.assertNotIn("Access-Control-Allow-Origin", response.headers)
        with self.assertRaises(ValueError):
            create_app({**self.config, "CORS_ORIGINS": "*"})

    def test_seed_command_is_idempotent(self):
        file_path = Path(self.directory.name) / "seed.json"
        file_path.write_text(json.dumps(self.sample))
        runner = self.app.test_cli_runner()
        first = runner.invoke(args=["seed-example", "--file", str(file_path)])
        second = runner.invoke(args=["seed-example", "--file", str(file_path)])
        self.assertEqual(first.exit_code, 0, first.output)
        self.assertEqual(second.exit_code, 0, second.output)
        self.assertIn("already seeded", second.output)
        project = self.client.get("/api/projects", headers=self.headers).json["projects"][0]
        details = self.client.get(f"/api/projects/{project['id']}", headers=self.headers).json
        self.assertEqual(details["experiments"][0]["plot_count"], 1)

    def test_multiple_workers_initialize_one_signing_key(self):
        config = {**self.config, "DATABASE_PATH": str(Path(self.directory.name) / "concurrent.sqlite3")}
        with ThreadPoolExecutor(max_workers=4) as executor:
            applications = list(executor.map(lambda _: create_app(config), range(4)))
        self.assertEqual(len({application.config["SECRET_KEY"] for application in applications}), 1)

    def test_bad_request_body_and_unknown_records_return_json_errors(self):
        self.assertEqual(self.client.post("/api/projects", data="bad", headers=self.headers).status_code, 415)
        for payload in (None, [], {"name": ""}):
            response = self.client.post("/api/projects", data=json.dumps(payload), content_type="application/json", headers=self.headers)
            self.assertEqual(response.status_code, 400)
            self.assertIn("error", response.json)
        response = self.client.get("/api/projects/missing", headers=self.headers)
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response.json)


if __name__ == "__main__":
    unittest.main()
