import copy
import gzip
import json
import random
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import boto3
from moto import mock_aws

from tracker import create_app
from tracker.auth import issue_token, serializer
from tracker.auth import require_auth


class TrackerContract:
    """The same HTTP contract must work with both persistent stores."""

    storage_backend = "sqlite"

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database_path = str(Path(self.directory.name) / "experiments.sqlite3")
        self.config = {
            "TESTING": True, "STORAGE_BACKEND": self.storage_backend,
            "DATABASE_PATH": self.database_path, "ROOT_ACCESS_CODE": "666666",
        }
        if self.storage_backend == "dynamodb":
            mocked_aws = mock_aws()
            mocked_aws.start()
            self.addCleanup(mocked_aws.stop)
            self.dynamo = boto3.client("dynamodb", region_name="us-east-1")
            self.dynamo.create_table(
                TableName="experiment-tests",
                KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
                AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            self.config.update({
                "DYNAMODB_TABLE": "experiment-tests", "DYNAMODB_CLIENT": self.dynamo,
                "AWS_REGION": "us-east-1", "SECRET_KEY": "stable-signing-key-for-isolated-tests-only",
            })
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

    def test_local_failure_limit_covers_login_and_direct_code_and_expires(self):
        # Authentication deliberately uses no persistent store. Limits apply to
        # one execution environment, not globally across Lambda instances.
        with patch("tracker.auth.time.time", return_value=1_000_000):
            for _ in range(3):
                self.assertEqual(self.client.post("/api/auth/session", json={"code": "bad"}).status_code, 401)
            for _ in range(2):
                self.assertEqual(self.client.get("/api/projects", headers={"X-Access-Code": "bad"}).status_code, 401)
            response = self.client.post("/api/auth/session", json={"code": "666666"})
            self.assertEqual(response.status_code, 429)
            self.assertGreaterEqual(int(response.headers["Retry-After"]), 1)
            # Caller-controlled forwarding headers cannot reset source identity.
            self.assertEqual(self.client.get("/api/projects", headers={**self.headers, "X-Forwarded-For": "192.0.2.8"}).status_code, 429)
            second_worker = create_app(self.config).test_client()
            self.assertEqual(second_worker.post("/api/auth/session", json={"code": "666666"}).status_code, 200)
        with patch("tracker.auth.time.time", return_value=1_000_061):
            self.assertEqual(self.client.get("/api/projects", headers=self.headers).status_code, 200)

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

    def test_full_plot_snapshot_replaces_options_and_survives_app_restart(self):
        result = self.upload().json
        experiment_path = f"/api/experiments/{result['experiment_id']}"
        original = self.client.get(experiment_path, headers=self.headers).json["plots"][0]
        plot_path = f"/api/plots/{original['id']}"
        replacement = {
            "type": "line", "title": "Revised teacher comparison",
            "description": "Options supplied by the JSON editor.", "x_key": "k",
            "x_label": "Cutoff", "y_label": "Normalized gain",
            "y_min": 0.4, "y_max": 0.95,
            "series_labels": {"cosine_ndcg": "Cosine", "base_ndcg": "Base model"},
            "colors": {"cosine_ndcg": "#ab1234", "base_ndcg": "#12ab34"},
        }
        # A complete snapshot replaces existing maps, including removal of the
        # original weighted_ndcg label/color rather than merging those back in.
        saved = self.client.patch(plot_path, json={"plot": replacement}, headers=self.headers)
        self.assertEqual(saved.status_code, 200, saved.json)
        self.assertEqual(saved.json, {**original, "plot": replacement})
        restarted_client = create_app(self.config).test_client()
        restored = restarted_client.get(experiment_path, headers=self.headers).json["plots"][0]
        self.assertEqual(restored, {**original, "plot": replacement})

        cleared = {
            "type": "bar", "title": "", "description": "", "x_key": "k",
            "x_label": "", "y_label": "", "y_min": None, "y_max": None,
            "series_labels": {}, "colors": {},
        }
        saved = restarted_client.patch(plot_path, json={"plot": cleared}, headers=self.headers)
        self.assertEqual(saved.status_code, 200, saved.json)
        self.assertEqual(saved.json, {**original, "plot": cleared})
        # New application/store instances prove this is persistent replacement,
        # not only an in-memory chart update or the PATCH response body.
        restored = create_app(self.config).test_client().get(experiment_path, headers=self.headers).json["plots"][0]
        self.assertEqual(restored, {**original, "plot": cleared})

    def test_invalid_plot_json_never_writes_or_changes_saved_options(self):
        from tracker.storage import get_store

        result = self.upload().json
        experiment_path = f"/api/experiments/{result['experiment_id']}"
        original = self.client.get(experiment_path, headers=self.headers).json["plots"][0]
        plot_path = f"/api/plots/{original['id']}"
        with self.app.app_context():
            store = get_store()

        invalid_bodies = [
            '{"plot":{"type":"line",}}',
            '{"plot":',
            json.dumps({"plot": None}),
            json.dumps({"plot": []}),
            json.dumps({"plot": {"title": 42}}),
            json.dumps({"plot": {"type": "pie"}}),
            json.dumps({"plot": {"x_key": "epoch"}}),
            json.dumps({"plot": {"y_min": 0.9, "y_max": 0.1}}),
            json.dumps({"plot": {"y_min": "0.1"}}),
            json.dumps({"plot": {"series_labels": None}}),
            json.dumps({"plot": {"colors": []}}),
            json.dumps({"plot": {"series_labels": {"unknown_series": "Other"}}}),
            json.dumps({"plot": {"colors": {"base_ndcg": "invalid;color"}}}),
            json.dumps({"plot": {"unsupported_option": True}}),
            json.dumps({"plot": {"title": "Should not save"}, "data": []}),
        ]
        with patch.object(store, "patch_plot", side_effect=AssertionError("Invalid options reached a storage write")) as write:
            for body in invalid_bodies:
                with self.subTest(body=body):
                    response = self.client.patch(plot_path, data=body, content_type="application/json", headers=self.headers)
                    self.assertEqual(response.status_code, 400, response.json)
                    self.assertIn("error", response.json)
            write.assert_not_called()
        restored = create_app(self.config).test_client().get(experiment_path, headers=self.headers).json["plots"][0]
        self.assertEqual(restored, original)

    def test_persistence_and_signing_key_survive_app_restart(self):
        result = self.upload().json
        token = self.client.post("/api/auth/session", json={"code": "666666"}).json["token"]
        restarted = create_app(self.config)
        self.assertEqual(self.app.config["SECRET_KEY"], restarted.config["SECRET_KEY"])
        response = restarted.test_client().get(f"/api/experiments/{result['experiment_id']}", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["plot_count"], 1)
        if self.storage_backend == "sqlite":
            self.assertEqual(Path(f"{self.database_path}.secret").stat().st_mode & 0o777, 0o600)
        else:
            self.assertFalse(Path(f"{self.database_path}.secret").exists())

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

    def test_concurrent_case_insensitive_project_creation_has_one_winner(self):
        workers = [create_app(self.config) for _ in range(4)]

        def create(index):
            return workers[index].test_client().post(
                "/api/projects", json={"name": "Concurrent" if index % 2 else "concurrent"}, headers=self.headers,
            ).status_code

        statuses, transactions = self.competing_requests(create)
        self.assertEqual(sorted(statuses), [201, 409, 409, 409])
        if transactions is not None:
            self.assertEqual(transactions.count(False), 3)
        projects = self.client.get("/api/projects", headers=self.headers).json["projects"]
        self.assertEqual(len(projects), 1)

    def test_concurrent_imports_never_split_project_or_experiment_identity(self):
        workers = [create_app(self.config) for _ in range(4)]

        def upload(index):
            return workers[index].test_client().post("/api/import", json=self.sample, headers=self.headers)

        responses, transactions = self.competing_requests(upload)
        for response in responses:
            self.assertIn(response.status_code, (201, 409), response.json)
        successful = [response.json for response in responses if response.status_code == 201]
        self.assertTrue(successful)
        if transactions is not None:
            self.assertEqual(len(successful), 4)
            self.assertGreaterEqual(transactions.count(False), 3)
        self.assertEqual(len({item["project_id"] for item in successful}), 1)
        self.assertEqual(len({item["experiment_id"] for item in successful}), 1)
        projects = self.client.get("/api/projects", headers=self.headers).json["projects"]
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["experiment_count"], 1)
        experiment = self.client.get(f"/api/experiments/{successful[0]['experiment_id']}", headers=self.headers).json
        self.assertEqual(experiment["plot_count"], len(successful))
        self.assertEqual({plot["id"] for plot in experiment["plots"]}, {plot_id for result in successful for plot_id in result["plot_ids"]})

    def competing_requests(self, operation):
        if self.storage_backend == "sqlite":
            with ThreadPoolExecutor(max_workers=4) as executor:
                return list(executor.map(operation, range(4))), None

        from tracker.dynamo import DynamoStore

        # Moto explicitly does not support multithreaded use:
        # https://docs.getmoto.org/en/latest/docs/faq.html
        # Its transaction rollback restores a whole-table snapshot, which can
        # erase another thread's already committed writes. Interleave requests
        # at the transaction boundary instead: each request prepares its reads
        # and writes before the next request commits, then submits its stale
        # conditions to the real Moto transaction implementation. This tests
        # CAS failures and application retries without overlapping simulator
        # transactions or serializing entire application requests.
        transact = DynamoStore._transact
        waiting = iter(range(1, 4))
        responses = []
        transactions = []

        def interleave(store, actions):
            index = next(waiting, None)
            if index is not None:
                responses.append(operation(index))
            committed = transact(store, actions)
            transactions.append(committed)
            return committed

        with patch.object(DynamoStore, "_transact", interleave):
            responses.append(operation(0))
        return responses, transactions

    def test_experiment_pages_cross_batch_boundaries_without_losing_plots(self):
        first_payload = copy.deepcopy(self.sample)
        first_payload["metrics"] = [copy.deepcopy(self.sample["metrics"][0]) for _ in range(3)]
        for index, metric in enumerate(first_payload["metrics"]):
            metric["plot"]["title"] = f"First batch {index}"
        first = self.upload(first_payload).json
        self.assertEqual(self.upload().status_code, 201)
        path = f"/api/experiments/{first['experiment_id']}"
        complete = self.client.get(path, headers=self.headers).json
        page_one = self.client.get(f"{path}?limit=2", headers=self.headers).json
        page_two = self.client.get(f"{path}?limit=2&cursor={page_one['next_cursor']}", headers=self.headers).json
        self.assertEqual(page_one["plot_count"], 4)
        self.assertEqual(page_two["plot_count"], 4)
        self.assertEqual(page_one["next_cursor"], "2")
        self.assertIsNone(page_two["next_cursor"])
        self.assertEqual(page_one["plots"] + page_two["plots"], complete["plots"])
        past_end = self.client.get(f"{path}?limit=1&cursor=50", headers=self.headers).json
        self.assertEqual(past_end["plots"], [])
        self.assertEqual(past_end["plot_count"], 4)
        self.assertIsNone(past_end["next_cursor"])

    def test_empty_experiment_pagination_and_invalid_page_arguments(self):
        project = self.client.post("/api/projects", json={"name": "Empty project"}, headers=self.headers).json
        experiment = self.client.post(f"/api/projects/{project['id']}/experiments", json={"name": "Empty experiment"}, headers=self.headers).json
        path = f"/api/experiments/{experiment['id']}"
        result = self.client.get(f"{path}?limit=1", headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["plots"], [])
        self.assertEqual(result.json["plot_count"], 0)
        self.assertIsNone(result.json["next_cursor"])
        for query in ("limit=0", "limit=101", "limit=1.5", "limit=no", "cursor=-1", "cursor=no", f"cursor={2 ** 63}"):
            with self.subTest(query=query):
                invalid = self.client.get(f"{path}?{query}", headers=self.headers)
                self.assertEqual(invalid.status_code, 400)
                self.assertIn("error", invalid.json)


class SQLiteTrackerTests(TrackerContract, unittest.TestCase):
    storage_backend = "sqlite"


class DynamoDBTrackerTests(TrackerContract, unittest.TestCase):
    storage_backend = "dynamodb"

    def test_cold_start_and_authentication_never_call_dynamodb(self):
        with patch("botocore.client.BaseClient._make_api_call", side_effect=AssertionError("Authentication must not call DynamoDB")) as operations:
            application = create_app(self.config)

            @application.get("/auth-only-test")
            @require_auth
            def auth_only():
                return {"authenticated": True}

            client = application.test_client()
            login = client.post("/api/auth/session", json={"code": "666666"})
            self.assertEqual(login.status_code, 200)
            token = login.json["token"]
            self.assertEqual(client.get("/auth-only-test", headers={"Authorization": f"Bearer {token}"}).status_code, 200)
            self.assertEqual(client.get("/auth-only-test", headers=self.headers).status_code, 200)
            self.assertEqual(client.get("/api/health").status_code, 200)
            self.assertEqual(client.options("/api/projects", headers={"Origin": "https://n4feng.github.io"}).status_code, 204)
            self.assertEqual(client.get("/api/projects").status_code, 401)
            self.assertEqual(client.get("/api/projects", headers={"Authorization": "Bearer invalid"}).status_code, 401)
            self.assertEqual(client.get("/api/projects", headers={"X-Access-Code": "bad"}).status_code, 401)
            self.assertEqual(client.post("/api/auth/session", json={"code": "bad"}).status_code, 401)
            for _ in range(3):
                self.assertEqual(client.post("/api/auth/session", json={"code": "bad"}).status_code, 401)
            self.assertEqual(client.post("/api/auth/session", json={"code": "bad"}).status_code, 429)
            operations.assert_not_called()

    def test_dynamodb_requires_a_stable_configured_signing_secret(self):
        with patch.dict("os.environ", {"SECRET_KEY": ""}):
            with self.assertRaises(ValueError):
                create_app({**self.config, "SECRET_KEY": None})

    def test_lambda_cannot_silently_fall_back_to_sqlite(self):
        with patch.dict("os.environ", {"AWS_LAMBDA_FUNCTION_NAME": "experiment-api"}):
            with self.assertRaises(ValueError):
                create_app({**self.config, "STORAGE_BACKEND": "sqlite"})

    def test_query_pagination_preserves_all_projects_experiments_and_plots(self):
        result = self.upload().json
        for index in range(4):
            self.assertEqual(self.client.post("/api/projects", json={"name": f"Extra project {index}"}, headers=self.headers).status_code, 201)
            self.assertEqual(self.client.post(f"/api/projects/{result['project_id']}/experiments", json={"name": f"Extra experiment {index}"}, headers=self.headers).status_code, 201)
            self.assertEqual(self.client.post(f"/api/experiments/{result['experiment_id']}/plots", json=self.sample["metrics"][0], headers=self.headers).status_code, 201)

        actual_query = self.dynamo.query
        pages_with_continuation = []

        def short_page(**kwargs):
            kwargs["Limit"] = 2
            page = actual_query(**kwargs)
            if page.get("LastEvaluatedKey"):
                pages_with_continuation.append(page["LastEvaluatedKey"])
            return page

        with patch.object(self.dynamo, "query", side_effect=short_page):
            projects = self.client.get("/api/projects", headers=self.headers).json["projects"]
            project = self.client.get(f"/api/projects/{result['project_id']}", headers=self.headers).json
            experiment = self.client.get(f"/api/experiments/{result['experiment_id']}", headers=self.headers).json
        self.assertEqual(len(projects), 5)
        self.assertEqual(project["experiment_count"], 5)
        self.assertEqual(len(project["experiments"]), 5)
        self.assertEqual(experiment["plot_count"], 5)
        self.assertEqual(len(experiment["plots"]), 5)
        self.assertGreaterEqual(len(pages_with_continuation), 3)

    def test_large_plot_crosses_dynamodb_item_limit_and_roundtrips_exactly(self):
        generator = random.Random(8426)
        rows = [{"k": index + 1, **{f"method_{series}": generator.random() for series in range(8)}} for index in range(6000)]
        raw = json.dumps(rows).encode()
        self.assertGreater(len(raw), 400 * 1024)
        # This input also crosses the chunk boundary after compression.
        self.assertGreater(len(gzip.compress(raw)), 250 * 1024)
        payload = copy.deepcopy(self.sample)
        payload["metrics"][0] = {"metric": "ndcg_at_k", "data": rows, "plot": {"type": "line"}}
        result = self.upload(payload)
        self.assertEqual(result.status_code, 201, result.json)
        experiment = self.client.get(f"/api/experiments/{result.json['experiment_id']}", headers=self.headers).json
        self.assertEqual(experiment["plots"][0]["data"], rows)
        batch_id = result.json["plot_ids"][0].rsplit("-", 1)[0]
        chunks = self.dynamo.query(
            TableName=self.config["DYNAMODB_TABLE"], KeyConditionExpression="PK = :pk AND begins_with(SK, :chunk)",
            ExpressionAttributeValues={":pk": {"S": f"BATCH#{batch_id}"}, ":chunk": {"S": "CHUNK#"}},
        )["Items"]
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all("expires_at" not in chunk for chunk in chunks), "Published chunks must never expire")
        # Editing metadata must not truncate or rewrite only the first chunk.
        updated = self.client.patch(f"/api/plots/{result.json['plot_ids'][0]}", json={"plot": {"description": "Large plot updated"}}, headers=self.headers)
        self.assertEqual(updated.status_code, 200, updated.json)
        self.assertEqual(updated.json["data"], rows)

    def test_oversized_request_fails_without_partial_business_records(self):
        response = self.client.post("/api/import", data=json.dumps({"padding": "x" * (4 * 1024 * 1024)}), content_type="application/json", headers=self.headers)
        self.assertEqual(response.status_code, 413)
        self.assertIn("error", response.json)
        self.assertEqual(self.client.get("/api/projects", headers=self.headers).json["projects"], [])

    def test_failed_publication_keeps_staged_chunks_invisible_and_expiring(self):
        with patch("tracker.dynamo.DynamoStore._transact", return_value=False), patch("tracker.dynamo.time.sleep"):
            response = self.upload()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.client.get("/api/projects", headers=self.headers).json["projects"], [])
        staged = self.dynamo.scan(TableName=self.config["DYNAMODB_TABLE"])["Items"]
        self.assertTrue(staged)
        self.assertTrue(all(item["SK"]["S"].startswith("CHUNK#") and "expires_at" in item for item in staged))
        # A later retry publishes only its complete batch; abandoned staging
        # records never appear in counts or experiment data.
        result = self.upload()
        self.assertEqual(result.status_code, 201, result.json)
        experiment = self.client.get(f"/api/experiments/{result.json['experiment_id']}", headers=self.headers).json
        self.assertEqual(experiment["plot_count"], 1)
        self.assertEqual(experiment["plots"][0]["data"], self.sample["metrics"][0]["data"])


if __name__ == "__main__":
    unittest.main()
