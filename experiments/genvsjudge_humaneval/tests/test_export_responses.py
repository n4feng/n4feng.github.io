"""Audit exports must preserve denominators, ties, identities and pagination."""

import copy
import csv
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_responses.py"
SPEC = importlib.util.spec_from_file_location("audit_export_responses", SCRIPT)
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)


def fixture():
    questions = []
    for dataset in exporter.DATASETS:
        for index in (1, 2):
            questions.append({
                "id": f"{dataset}-{index:04d}", "dataset": dataset,
                "qid": f"q{index}", "pcid": f"p{index}", "ncid": f"n{index}",
                "question": f"Question {index}?", "answer": f"Answer {index}",
                "source": {"pair_file": f"{dataset}/test_pairs.jsonl", "pair_index": index - 1, "pair_line_number": index, "split": "test"},
            })
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
        for index, negative in enumerate(negatives.get(question["id"], [])):
            order = {"A": "negative", "B": "positive"} if index % 2 else {"A": "positive", "B": "negative"}
            ratings = {"positive": "supports", "negative": negative}
            responses.append({
                "PK": "STUDY#audit-test", "SK": f"RESPONSE#{question['id']}#r{index}",
                "question_id": question["id"],
                **{field: question[field] for field in ("dataset", "qid", "pcid", "ncid")},
                "reviewer_id": f"r{index}", "name": f"Reviewer {index}",
                "ratings": ratings, "displayed_ratings": {key: ratings[role] for key, role in order.items()},
                "document_order": order, "notes": "evidence\ntext", "created_at": "2026-11-06T20:00:00+00:00",
            })
    return manifest, responses


class ExportTests(unittest.TestCase):
    def test_zero_questions_are_included_and_votes_are_not_question_counts(self):
        manifest, responses = fixture()
        flat, questions, summary = exporter.build_reports(manifest, responses, strict=False)
        self.assertEqual(len(flat), 15)
        self.assertEqual(len(questions), 6)
        self.assertEqual(summary["total_reviews"], 15)
        self.assertEqual(summary["min_reviews"], 0)
        self.assertEqual(summary["max_reviews"], 4)
        self.assertEqual(summary["mean_reviews_per_question"], 2.5)
        self.assertEqual(summary["questions_at_target"], 4)
        self.assertEqual(summary["remaining_reviews"], 4)
        self.assertEqual(summary["distinct_reviewers"], 4)
        self.assertEqual(summary["negative_rating_vote_counts"], {
            "supports": 6, "partial": 4, "does_not_support": 4, "unsure": 1,
        })
        self.assertEqual(summary["negative_rating_majority_question_counts"], {
            "supports": 1, "partial": 1, "does_not_support": 1, "unsure": 0, "no_majority": 3,
        })
        nq = summary["by_dataset"]["nq"]
        self.assertEqual(nq["mean_reviews_per_question"], 1.5)
        self.assertEqual(nq["min_reviews"], 0)
        self.assertEqual(nq["total_reviews"], 3)
        self.assertEqual(summary["by_dataset"]["hotpotqa"]["mean_reviews_per_question"], 3)

    def test_majority_requires_three_votes_and_strictly_more_than_half(self):
        manifest, responses = fixture()
        _, questions, _ = exporter.build_reports(manifest, responses, strict=False)
        rows = {row["question_id"]: row for row in questions}
        self.assertEqual(rows["nq-0001"]["negative_majority"], "supports")
        self.assertIsNone(rows["nq-0002"]["negative_majority"])
        self.assertIsNone(rows["hotpotqa-0001"]["negative_majority"])
        self.assertIsNone(rows["hotpotqa-0002"]["negative_majority"])
        self.assertEqual(rows["msmarco-0001"]["negative_majority"], "partial")
        self.assertEqual(rows["msmarco-0002"]["negative_majority"], "does_not_support")
        self.assertEqual(rows["nq-0002"]["negative_supports_votes"], 0)

    def test_no_responses_is_valid_and_preserves_denominator(self):
        manifest, _ = fixture()
        _, questions, summary = exporter.build_reports(manifest, [], strict=False)
        self.assertEqual(len(questions), 6)
        self.assertEqual(summary["total_reviews"], 0)
        self.assertEqual(summary["min_reviews"], 0)
        self.assertEqual(summary["remaining_reviews"], 18)
        self.assertEqual(summary["negative_rating_majority_question_counts"]["no_majority"], 6)

    def test_duplicates_and_wrong_manifest_identity_are_rejected(self):
        manifest, responses = fixture()
        with self.assertRaisesRegex(ValueError, "Duplicate reviewer/question"):
            exporter.build_reports(manifest, responses + [responses[0]], strict=False)
        for field, value in (("dataset", "msmarco"), ("qid", "other"), ("pcid", "other"), ("ncid", "other"), ("PK", "STUDY#other")):
            changed = copy.deepcopy(responses)
            changed[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                exporter.build_reports(manifest, changed, strict=False)

    def test_original_roles_are_checked_against_displayed_order(self):
        manifest, responses = fixture()
        changed = copy.deepcopy(responses)
        changed[0]["displayed_ratings"]["A"] = "unsure"
        with self.assertRaisesRegex(ValueError, "Displayed and original-role"):
            exporter.build_reports(manifest, changed, strict=False)

    def test_query_is_paginated_and_consistent_including_empty_pages(self):
        last_key = {"PK": "STUDY#audit-test", "SK": "RESPONSE#first"}
        second_key = {"PK": "STUDY#audit-test", "SK": "RESPONSE#second"}

        class FakeTable:
            def __init__(self):
                self.calls = []
                self.pages = [
                    {"Items": [{"question_id": "first"}], "LastEvaluatedKey": last_key},
                    {"Items": [], "LastEvaluatedKey": second_key},
                    {"Items": [{"question_id": "last"}]},
                ]

            def query(self, **kwargs):
                self.calls.append(copy.deepcopy(kwargs))
                return self.pages.pop(0)

        table = FakeTable()
        result = exporter.fetch_responses(table, "audit-test")
        self.assertEqual(result, [{"question_id": "first"}, {"question_id": "last"}])
        self.assertEqual(len(table.calls), 3)
        self.assertNotIn("ExclusiveStartKey", table.calls[0])
        self.assertEqual(table.calls[1]["ExclusiveStartKey"], last_key)
        self.assertEqual(table.calls[2]["ExclusiveStartKey"], second_key)
        for call in table.calls:
            self.assertTrue(call["ConsistentRead"])
            self.assertEqual(call["ExpressionAttributeValues"], {":pk": "STUDY#audit-test", ":prefix": "RESPONSE#"})

    def test_stack_lookup_uses_only_table_output(self):
        class FakeCloudFormation:
            def describe_stacks(self, **kwargs):
                self.call = kwargs
                return {"Stacks": [{"Outputs": [{"OutputKey": "TableName", "OutputValue": "audit-table"}]}]}

        client = FakeCloudFormation()
        self.assertEqual(exporter.table_from_stack(client, "audit"), "audit-table")
        self.assertEqual(client.call, {"StackName": "audit"})

    def test_writes_all_formats_privately_and_refuses_accidental_overwrite(self):
        manifest, responses = fixture()
        responses[0]["name"] = "=HYPERLINK(\"x\")"
        responses[0]["duration"] = Decimal("1.5")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "exports"
            exporter.write_reports(output, manifest, responses, strict=False)
            self.assertEqual({p.name for p in output.iterdir()}, set(exporter.OUTPUT_FILES))
            raw = [json.loads(line) for line in (output / "responses.jsonl").read_text().splitlines()]
            self.assertEqual(raw[0]["name"], responses[0]["name"])
            self.assertEqual(raw[0]["duration"], 1.5)
            with (output / "responses.csv").open(newline="") as handle:
                flat = list(csv.DictReader(handle))
            matching = next(row for row in flat if row["name"].startswith("'="))
            self.assertEqual(matching["name"], "'" + responses[0]["name"])
            self.assertEqual(matching["notes"], "evidence\ntext")
            self.assertEqual(matching["positive_rating"], "supports")
            with (output / "questions.csv").open(newline="") as handle:
                questions = list(csv.DictReader(handle))
            self.assertEqual(len(questions), 6)
            self.assertEqual(next(row for row in questions if row["question_id"] == "nq-0002")["review_count"], "0")
            for name in exporter.OUTPUT_FILES:
                self.assertEqual((output / name).stat().st_mode & 0o777, 0o600)
            before = (output / "responses.jsonl").read_bytes()
            with self.assertRaises(FileExistsError):
                exporter.write_reports(output, manifest, [], strict=False)
            self.assertEqual((output / "responses.jsonl").read_bytes(), before)
            exporter.write_reports(output, manifest, [], overwrite=True, strict=False)
            self.assertEqual((output / "responses.jsonl").read_text(), "")

    def test_production_requires_full_balanced_manifest(self):
        manifest, responses = fixture()
        with self.assertRaisesRegex(ValueError, "100 questions per dataset"):
            exporter.build_reports(manifest, responses)


if __name__ == "__main__":
    unittest.main()
