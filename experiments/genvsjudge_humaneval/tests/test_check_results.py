"""Read-only result checks preserve audit denominators and participant privacy."""

import copy
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("audit_check_results", SCRIPTS / "check_results.py")
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def fixture(per_dataset=2):
    questions = [
        {
            "id": f"{dataset}-{index:04d}", "dataset": dataset,
            "qid": f"q{index}", "pcid": f"p{index}", "ncid": f"n{index}",
            "question": "Private question text", "answer": "Private gold answer",
        }
        for dataset in ("nq", "hotpotqa", "msmarco")
        for index in range(1, per_dataset + 1)
    ]
    manifest = {"schema_version": 1, "study_id": "audit-test", "target_reviews": 3, "questions": questions}
    negatives = {
        "nq-0001": ["supports", "supports", "does_not_support"],
        "hotpotqa-0001": ["supports", "supports", "partial", "partial"],
        "hotpotqa-0002": ["supports", "supports"],
        "msmarco-0001": ["partial", "partial", "unsure"],
        "msmarco-0002": ["does_not_support"] * 3,
    }
    responses = []
    for question in questions:
        for index, rating in enumerate(negatives.get(question["id"], [])):
            order = {"A": "negative", "B": "positive"} if index % 2 else {"A": "positive", "B": "negative"}
            ratings = {"positive": "supports", "negative": rating}
            responses.append({
                "PK": "STUDY#audit-test", "SK": f"RESPONSE#{question['id']}#private-r{index}",
                "question_id": question["id"],
                **{key: question[key] for key in ("dataset", "qid", "pcid", "ncid")},
                "reviewer_id": f"private-r{index}", "name": f"Private Reviewer {index}",
                "ratings": ratings, "document_order": order,
                "displayed_ratings": {position: ratings[role] for position, role in order.items()},
                "notes": "Private reviewer notes", "created_at": "2026-11-09T12:00:00+00:00",
            })
    return manifest, responses


def health():
    return {"ok": True, "study_id": "audit-test", "question_count": 300}


def empty_stats():
    return {
        "study_id": "audit-test", "total_questions": 300, "target_reviews": 3,
        "min_reviews": 0, "total_reviews": 0, "questions_at_target": 0,
        "remaining_reviews": 900,
        "by_dataset": {
            name: {"total_questions": 100, "min_reviews": 0, "total_reviews": 0, "questions_at_target": 0}
            for name in ("nq", "hotpotqa", "msmarco")
        },
    }


class FakeCloudFormation:
    def __init__(self, url=None):
        self.url = url or checker.SERVER_URL
        self.calls = []

    def describe_stacks(self, **kwargs):
        self.calls.append(kwargs)
        return {"Stacks": [{"Outputs": [
            {"OutputKey": "TableName", "OutputValue": "audit-table"},
            {"OutputKey": "ApiUrl", "OutputValue": self.url + "/"},
        ]}]}


class CheckResultsTests(unittest.TestCase):
    def test_empty_study_retains_all_300_questions(self):
        manifest, _ = fixture(100)
        summary = checker.summarize_results(manifest, [])
        self.assertEqual(summary["total_questions"], 300)
        self.assertEqual(summary["total_reviews"], 0)
        self.assertEqual(summary["min_reviews"], 0)
        self.assertEqual(summary["remaining_reviews"], 900)
        self.assertEqual(summary["questions_below_target"], 300)
        for dataset in summary["by_dataset"].values():
            self.assertEqual(dataset["total_questions"], 100)
            self.assertEqual(dataset["remaining_reviews"], 300)

    def test_totals_preserve_roles_and_do_not_sum_distinct_reviewers(self):
        manifest, responses = fixture()
        summary = checker.summarize_results(manifest, responses, strict=False)
        self.assertEqual(summary["total_reviews"], 15)
        self.assertEqual(summary["distinct_reviewers"], 4)
        self.assertEqual(summary["min_reviews"], 0)
        self.assertEqual(summary["questions_at_target"], 4)
        self.assertEqual(summary["remaining_reviews"], 4)
        self.assertEqual(summary["positive_rating_vote_counts"], {
            "supports": 15, "partial": 0, "does_not_support": 0, "unsure": 0,
        })
        self.assertEqual(summary["negative_rating_vote_counts"], {
            "supports": 6, "partial": 4, "does_not_support": 4, "unsure": 1,
        })
        self.assertEqual(summary["by_dataset"]["nq"]["total_reviews"], 3)
        self.assertEqual(summary["by_dataset"]["hotpotqa"]["total_reviews"], 6)
        self.assertEqual(summary["by_dataset"]["msmarco"]["total_reviews"], 6)

    def test_majority_distinguishes_insufficient_reviews_and_ties(self):
        manifest, responses = fixture()
        summary = checker.summarize_results(manifest, responses, strict=False)
        self.assertEqual(summary["questions_below_target"], 2)
        self.assertEqual(summary["negative_rating_majority_question_counts"]["supports"], 1)
        self.assertEqual(summary["negative_rating_majority_question_counts"]["partial"], 1)
        self.assertEqual(summary["negative_rating_majority_question_counts"]["does_not_support"], 1)
        self.assertEqual(summary["positive_rating_majority_question_counts"]["supports"], 4)
        self.assertEqual(summary["negative_rating_majority_question_counts"]["no_majority_at_target"], 1)
        self.assertEqual(summary["positive_rating_majority_question_counts"]["no_majority_at_target"], 0)
        self.assertEqual(summary["by_dataset"]["hotpotqa"]["questions_below_target"], 1)
        self.assertEqual(summary["by_dataset"]["hotpotqa"]["negative_rating_majority_question_counts"]["no_majority_at_target"], 1)

    def test_output_excludes_participant_records_and_question_text(self):
        manifest, responses = fixture()
        summary = checker.summarize_results(manifest, responses, strict=False)
        for output in (json.dumps(summary), checker.format_report(summary)):
            for private_value in ("Private Reviewer", "private-r", "Private reviewer notes", "Private question text", "Private gold answer"):
                self.assertNotIn(private_value, output)

    def test_health_rejects_tracker_or_wrong_study(self):
        checker.validate_health(health(), "audit-test")
        for value in ({"status": "ok"}, {**health(), "question_count": 299}, {**health(), "ok": False}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                checker.validate_health(value)
        with self.assertRaises(ValueError):
            checker.validate_health(health(), "different-study")

    def test_stats_reject_mismatched_study_or_unbalanced_cohort(self):
        checker.validate_stats(empty_stats(), health())
        with self.assertRaises(ValueError):
            checker.validate_stats({**empty_stats(), "study_id": "wrong"}, health())
        broken = empty_stats()
        broken["by_dataset"]["nq"]["total_questions"] = 99
        with self.assertRaises(ValueError):
            checker.validate_stats(broken, health())

    def test_invalid_http_payload_does_not_look_like_an_empty_study(self):
        for body in (b"<html>Service unavailable</html>", b"[]"):
            with self.subTest(body=body), patch.object(checker, "urlopen", return_value=io.BytesIO(body)):
                with self.assertRaises(ValueError):
                    checker.request_json(checker.SERVER_URL, "/api/health")

    def test_wrong_manifest_study_fails_before_reading_aws(self):
        manifest, _ = fixture(100)
        manifest["study_id"] = "different-study"
        aws = types.SimpleNamespace(Session=lambda **kwargs: self.fail("Mismatched study called AWS"))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "questions.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(checker, "request_json", return_value=health()), \
                    patch.dict(sys.modules, {"boto3": aws}), redirect_stdout(stdout), redirect_stderr(stderr):
                code = checker.main(["--questions", str(path), "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("study_id", stderr.getvalue())

    def test_stack_lookup_verifies_the_supplied_endpoint(self):
        client = FakeCloudFormation()
        self.assertEqual(checker.table_from_stack(client, "study-stack", checker.SERVER_URL), "audit-table")
        self.assertEqual(client.calls, [{"StackName": "study-stack"}])
        with self.assertRaises(ValueError):
            checker.table_from_stack(FakeCloudFormation("https://other.lambda-url.us-east-1.on.aws"), "study-stack", checker.SERVER_URL)

    def test_progress_only_needs_neither_manifest_nor_aws_credentials(self):
        aws = types.SimpleNamespace(Session=lambda **kwargs: self.fail("Progress called AWS"))
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(checker, "request_json", side_effect=[health(), empty_stats()]), \
                patch.object(checker, "resolve_questions", side_effect=AssertionError("Progress read a manifest")), \
                patch.dict(sys.modules, {"boto3": aws}), redirect_stdout(stdout), redirect_stderr(stderr):
            code = checker.main(["--progress-only", "--json"])
        self.assertEqual(code, 0, stderr.getvalue())
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["total_questions"], 300)
        self.assertEqual(report["remaining_reviews"], 900)

    def test_authenticated_workflow_queries_every_page_read_only(self):
        manifest, responses = fixture(100)
        last_key = {"PK": "STUDY#audit-test", "SK": responses[0]["SK"]}

        class Table:
            def __init__(self):
                self.calls = []
                self.pages = [
                    {"Items": responses[:1], "LastEvaluatedKey": last_key},
                    {"Items": responses[1:]},
                ]

            def query(self, **kwargs):
                self.calls.append(copy.deepcopy(kwargs))
                return self.pages.pop(0)

        table, cloud = Table(), FakeCloudFormation()
        parent = self

        class Session:
            def client(self, name):
                parent.assertEqual(name, "cloudformation")
                return cloud

            def resource(self, name):
                parent.assertEqual(name, "dynamodb")
                return types.SimpleNamespace(Table=lambda name: table if name == "audit-table" else parent.fail(name))

        aws = types.SimpleNamespace(Session=lambda **kwargs: Session())
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "questions.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(checker, "request_json", return_value=health()), \
                    patch.dict(sys.modules, {"boto3": aws}), redirect_stdout(stdout), redirect_stderr(stderr):
                code = checker.main(["--questions", str(path), "--json"])
        self.assertEqual(code, 0, stderr.getvalue())
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["total_reviews"], 15)
        self.assertEqual(result["total_questions"], 300)
        self.assertEqual(result["questions_at_target"], 4)
        self.assertEqual(len(table.calls), 2)
        self.assertEqual(table.calls[1]["ExclusiveStartKey"], last_key)
        for call in table.calls:
            self.assertTrue(call["ConsistentRead"])
            self.assertEqual(call["ExpressionAttributeValues"], {":pk": "STUDY#audit-test", ":prefix": "RESPONSE#"})
        self.assertNotIn("Private Reviewer", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
