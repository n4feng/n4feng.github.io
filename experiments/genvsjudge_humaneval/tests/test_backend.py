"""Exercise real DynamoDB expressions and transactions with moto, without AWS."""

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import boto3
from moto import mock_aws


SPEC = importlib.util.spec_from_file_location("audit_lambda_handler", Path(__file__).parents[1] / "server" / "lambda_handler.py")
api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(api)


def manifest(per_dataset=2):
    return {
        "schema_version": 1, "study_id": "test-audit-v1", "seed": 42, "target_reviews": 3,
        "questions": [
            {
                "id": f"{dataset}-{index}", "dataset": dataset, "qid": str(index),
                "pcid": f"positive-{index}", "ncid": f"negative-{index}",
                "question": f"Question {index}?", "answer": "Reference answer",
                "positive_document": f"A supporting passage {index}.",
                "negative_document": f"Another passage {index}.",
            }
            for dataset in api.DATASETS for index in range(per_dataset)
        ],
    }


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.aws = mock_aws()
        self.aws.start()
        self.client = boto3.client("dynamodb", region_name="us-east-1")
        self.client.create_table(
            TableName="audit-tests", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"} for key in ("PK", "SK")],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        )
        self.now = 1_800_000_000
        self.app = api.AuditApp(manifest(), "audit-tests", "s" * 40, client=self.client, strict=False, clock=lambda: self.now)

    def tearDown(self):
        self.aws.stop()

    def session(self, name="Alice"):
        response = self.app.session({"name": name})
        return self.app.authenticate({"authorization": "Bearer " + response["token"]})

    def submission(self, session, question=None):
        question = question or self.app.next_question(session)["question"]
        return {
            "question_id": question["id"], "assignment_token": question["assignment_token"],
            "ratings": {"A": "supports", "B": "does_not_support"}, "notes": "Evidence in sentence 1.",
        }

    @contextmanager
    def serializable_mock(self):
        # Moto mutates its in-memory records in stages and is not a transactional
        # database under threads. Serialize its raw operations to model DynamoDB's
        # atomic transaction visibility while keeping application requests concurrent.
        lock = threading.RLock()
        with ExitStack() as stack:
            for method in ("transact_write_items", "query", "get_item"):
                original = getattr(self.client, method)
                def locked(*args, _original=original, **kwargs):
                    with lock:
                        return _original(*args, **kwargs)
                stack.enter_context(patch.object(self.client, method, side_effect=locked))
            yield

    def test_zero_count_questions_are_in_minimum_and_remaining(self):
        stats = self.app.stats()
        self.assertEqual(stats["total_questions"], 6)
        self.assertEqual(stats["min_reviews"], 0)
        self.assertEqual(stats["remaining_reviews"], 18)
        self.app.submit(self.session(), self.submission(self.session()))
        self.assertEqual(self.app.stats()["min_reviews"], 0)
        self.assertEqual(self.app.stats()["remaining_reviews"], 17)

    def test_blinded_payload_and_token_do_not_disclose_original_labels(self):
        question = self.app.next_question(self.session())["question"]
        self.assertEqual(set(question), {"id", "dataset", "question", "answer", "documents", "assignment_token"})
        self.assertEqual(set(question["documents"]), {"A", "B"})
        payload = json.loads(api._unb64(question["assignment_token"].split(".")[0]))
        self.assertEqual(set(payload), {"kind", "study_id", "question_id", "reviewer_id", "exp"})

    def test_same_normalized_name_gets_same_identity_and_reviewed_set(self):
        first = self.session("  Alice   Smith  ")
        second = self.session("ＡＬＩＣＥ smith")
        self.assertEqual(first["reviewer_id"], second["reviewer_id"])
        body = self.submission(first)
        self.app.submit(first, body)
        self.assertEqual(self.app.next_question(second)["reviewer_completed"], 1)
        self.assertNotEqual(self.app.next_question(second)["question"]["id"], body["question_id"])

    def test_displayed_choices_map_back_to_original_roles(self):
        session = self.session()
        body = self.submission(session)
        self.app.submit(session, body)
        saved = self.app.store.response(body["question_id"], session["reviewer_id"])
        order = saved["document_order"]
        self.assertEqual(saved["ratings"][order["A"]], "supports")
        self.assertEqual(saved["ratings"][order["B"]], "does_not_support")
        self.assertEqual(saved["name"], "Alice")
        self.assertEqual(saved["notes"], body["notes"])

    def test_retry_is_idempotent_and_conflicting_vote_cannot_overwrite(self):
        session = self.session()
        body = self.submission(session)
        self.assertFalse(self.app.submit(session, body)["duplicate"])
        self.now += 1
        self.assertTrue(self.app.submit(session, body)["duplicate"])
        changed = {**body, "ratings": {"A": "unsure", "B": "partial"}}
        with self.assertRaises(api.ApiError) as caught:
            self.app.submit(session, changed)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.app.store.counts()[body["question_id"]], 1)

    def test_least_reviewed_questions_are_prioritized(self):
        first = self.session()
        body = self.submission(first)
        self.app.submit(first, body)
        for _ in range(12):
            self.assertNotEqual(self.app.next_question(self.session("Bob"))["question"]["id"], body["question_id"])

    def test_skip_does_not_count_or_claim_completion(self):
        session = self.session()
        current = self.app.next_question(session)["question"]["id"]
        skipped = self.app.next_question(session, exclude=current)
        self.assertNotEqual(skipped["question"]["id"], current)
        self.assertEqual(skipped["reviewer_completed"], 0)
        self.assertEqual(skipped["stats"]["total_reviews"], 0)
        for _ in range(5):
            question = self.app.next_question(session, exclude=current)["question"]
            self.app.submit(session, self.submission(session, question))
        fallback = self.app.next_question(session, exclude=current)
        self.assertEqual(fallback["question"]["id"], current)

    def test_three_rounds_need_three_distinct_names_and_can_continue(self):
        for reviewer in ("Alice", "Bob", "Chen"):
            session = self.session(reviewer)
            for _ in range(6):
                self.app.submit(session, self.submission(session))
            end = self.app.next_question(session)
            self.assertIsNone(end["question"])
            self.assertEqual(end["reason"], "all_reviewed")
        stats = self.app.stats()
        self.assertEqual(stats["min_reviews"], 3)
        self.assertEqual(stats["total_reviews"], 18)
        self.assertEqual(stats["questions_at_target"], 6)
        self.assertEqual(stats["remaining_reviews"], 0)
        self.assertIsNotNone(self.app.next_question(self.session("Dana"))["question"])

    def test_same_question_from_different_reviewers_counts_separately(self):
        question_id = "nq-0"
        def submit_name(name):
            session = self.session(name)
            token = self.app._sign({"kind": "assignment", "study_id": self.app.study_id,
                "question_id": question_id, "reviewer_id": session["reviewer_id"], "exp": self.now + 60})
            body = {"question_id": question_id, "assignment_token": token,
                "ratings": {"A": "partial", "B": "unsure"}}
            for _ in range(10):
                try:
                    return self.app.submit(session, body)
                except api.ApiError as error:
                    if error.status != 503:
                        raise
            self.fail("Concurrent transaction retries exhausted")
        with self.serializable_mock(), ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(submit_name, ["Alice", "Bob", "Chen"]))
        self.assertTrue(all(result["saved"] for result in results))
        self.assertEqual(self.app.store.counts()[question_id], 3)
        self.assertEqual(self.app.stats()["min_reviews"], 0)

    def test_study_isolation(self):
        self.app.submit(self.session(), self.submission(self.session()))
        another_manifest = manifest()
        another_manifest["study_id"] = "another-study"
        another = api.AuditApp(another_manifest, "audit-tests", "s" * 40, client=self.client, strict=False)
        self.assertEqual(another.stats()["total_reviews"], 0)

    def test_parallel_retries_of_one_vote_increment_only_once(self):
        session = self.session()
        body = self.submission(session)
        with self.serializable_mock(), ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(lambda _: self.app.submit(session, body), range(3)))
        self.assertEqual(sum(not result["duplicate"] for result in results), 1)
        self.assertEqual(self.app.store.counts()[body["question_id"]], 1)

    def test_expired_assignment_reissues_same_order_for_retry(self):
        session = self.session()
        first = self.app.next_question(session)["question"]
        body = self.submission(session, first)
        self.app.submit(session, body)
        self.now += api.ASSIGNMENT_SECONDS + 1
        fresh_session = self.session("ALICE")
        fresh = self.app.next_question(fresh_session, question_id=first["id"])["question"]
        self.assertEqual(first["documents"], fresh["documents"])
        self.assertNotEqual(first["assignment_token"], fresh["assignment_token"])
        body["assignment_token"] = fresh["assignment_token"]
        self.assertTrue(self.app.submit(fresh_session, body)["duplicate"])
        self.assertEqual(self.app.store.counts()[first["id"]], 1)
        with self.assertRaises(api.ApiError):
            self.app.next_question(fresh_session, question_id="missing")

    def test_pagination_is_used_for_counts_and_responses(self):
        session = self.session()
        for _ in range(4):
            self.app.submit(session, self.submission(session))
        original = self.client.query
        seen_keys = []
        def limited(**kwargs):
            seen_keys.append(kwargs.get("ExclusiveStartKey"))
            return original(**kwargs, Limit=1)
        with patch.object(self.client, "query", side_effect=limited):
            self.assertEqual(self.app.stats()["total_reviews"], 4)
            self.assertEqual(len(self.app.store.reviewed(session["reviewer_id"])), 4)
        self.assertTrue(any(seen_keys))

    def test_assignment_cannot_move_between_reviewers_or_questions(self):
        alice, bob = self.session(), self.session("Bob")
        body = self.submission(alice)
        with self.assertRaises(api.ApiError) as caught:
            self.app.submit(bob, body)
        self.assertEqual(caught.exception.status, 400)
        body["question_id"] = next(key for key in self.app.questions if key != body["question_id"])
        with self.assertRaises(api.ApiError):
            self.app.submit(alice, body)
        self.assertEqual(self.app.stats()["total_reviews"], 0)

    def test_tampered_and_expired_tokens_are_rejected(self):
        session = self.session()
        body = self.submission(session)
        encoded, signature = body["assignment_token"].split(".")
        body["assignment_token"] = encoded + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
        with self.assertRaises(api.ApiError) as caught:
            self.app.submit(session, body)
        self.assertEqual(caught.exception.status, 401)
        body = self.submission(session)
        self.now += api.ASSIGNMENT_SECONDS + 1
        with self.assertRaises(api.ApiError):
            self.app.submit(session, body)

    def test_validation_rejects_invalid_names_ratings_and_notes(self):
        for name in (None, "", "   ", "a" * 81, "Ali\u200bce"):
            with self.assertRaises(api.ApiError):
                self.app.session({"name": name})
        session = self.session()
        body = self.submission(session)
        for ratings in ({"A": "supports"}, {"A": "yes", "B": "supports"}, {"A": [], "B": "unsure"}):
            with self.assertRaises(api.ApiError):
                self.app.submit(session, {**body, "ratings": ratings})
        with self.assertRaises(api.ApiError):
            self.app.submit(session, {**body, "notes": "a" * 2001})
        self.assertEqual(self.app.stats()["total_reviews"], 0)

    def test_production_manifest_checks_balance_duplicates_and_documents(self):
        valid = manifest(100)
        self.assertEqual(len(api.validate_manifest(valid)), 300)
        with self.assertRaises(ValueError):
            api.validate_manifest(manifest())
        for field, value in (("qid", "1"), ("positive_document", ""), ("ncid", "positive-0")):
            broken = deepcopy(valid)
            broken["questions"][0][field] = value
            with self.assertRaises(ValueError):
                api.validate_manifest(broken)

    def request(self, method="GET", path="/api/stats", body=None, token=None, origin="https://n4feng.github.io", **extra):
        headers = {"content-type": "application/json"}
        if origin is not None:
            headers["origin"] = origin
        if token:
            headers["authorization"] = "Bearer " + token
        event = {"rawPath": path, "requestContext": {"http": {"method": method}}, "headers": headers,
            "body": json.dumps(body) if body is not None else None, **extra}
        with patch.object(api, "_app", self.app), patch.dict(os.environ, {"ALLOWED_ORIGINS": "https://n4feng.github.io"}):
            return api.handler(event, None)

    def test_http_auth_cors_and_private_results(self):
        stats = self.request()
        self.assertEqual(stats["statusCode"], 200)
        self.assertEqual(stats["headers"]["Access-Control-Allow-Origin"], "https://n4feng.github.io")
        self.assertEqual(self.request(path="/api/next")["statusCode"], 401)
        forbidden = self.request(origin="https://unrelated.example")
        self.assertEqual(forbidden["statusCode"], 403)
        self.assertNotIn("Access-Control-Allow-Origin", forbidden["headers"])
        self.assertEqual(self.request(method="OPTIONS")["statusCode"], 204)
        self.assertEqual(self.request(path="/api/export")["statusCode"], 404)
        session = self.session("Private Participant")
        self.app.submit(session, self.submission(session))
        self.assertNotIn("Private Participant", self.request()["body"])
        self.assertNotIn("ratings", self.request()["body"])

    def test_http_malformed_and_base64_json(self):
        with patch.object(api, "_app", self.app):
            event = {"rawPath": "/api/session", "requestContext": {"http": {"method": "POST"}},
                "headers": {"content-type": "application/json"}, "body": "not-json"}
            self.assertEqual(api.handler(event, None)["statusCode"], 400)
            event["body"] = base64.b64encode(json.dumps({"name": "Alice"}).encode()).decode()
            event["isBase64Encoded"] = True
            self.assertEqual(api.handler(event, None)["statusCode"], 200)
            event["body"] = "x" * (api.MAX_BODY_BYTES * 3)
            self.assertEqual(api.handler(event, None)["statusCode"], 413)


if __name__ == "__main__":
    unittest.main()
