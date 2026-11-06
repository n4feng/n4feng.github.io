"""Exercise sampling with the actual GenVsJudge JSONL field conventions."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sample_questions.py"
SPEC = importlib.util.spec_from_file_location("audit_sample_questions", SCRIPT)
sampler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sampler)


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


class SamplingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prepared = self.root / sampler.DEFAULT_PREPARED_ROOT
        self.saved = self.root / "evaluation-sample"
        self.pairs = [
            {"qid": "q1", "pcid": "p1a", "ncid": "n1"},
            {"qid": "q1", "pcid": "p1b", "ncid": "n1"},
            {"qid": "q1", "pcid": "p1c", "ncid": "n1"},
            {"qid": "q2", "pcid": "p2", "ncid": "n2"},
            {"qid": "q3", "pcid": "p3", "ncid": "n3"},
            {"qid": "q4", "pcid": "p4", "ncid": "n4"},
        ]
        self.full_text = "Long evidence α\n" + "word " * 5000 + "END OF DOCUMENT"
        for dataset in sampler.DATASETS:
            write_jsonl(self.prepared / dataset / "train_pairs.jsonl", self.pairs[:4])
            write_jsonl(self.prepared / dataset / "test_pairs.jsonl", self.pairs[4:])
            write_jsonl(self.saved / dataset / "10000_sample_ids.jsonl", list(reversed(self.pairs)))
            write_jsonl(self.root / "data" / "qa" / f"{dataset}.jsonl", [
                {"id": f"q{number}", "question": f"Question {number}?", "answer": f"Gold answer {number}"}
                for number in range(1, 5)
            ])
            document_ids = {row[field] for row in self.pairs for field in ("pcid", "ncid")}
            write_jsonl(self.root / "data" / "corpus" / f"{dataset}.jsonl", [
                {"id": document_id, "text": self.full_text + document_id}
                for document_id in sorted(document_ids)
            ])

    def build(self, **kwargs):
        options = {"per_dataset": 4, "expected_pairs": 6, **kwargs}
        return sampler.build_manifest(self.root, **options)

    def test_samples_unique_questions_and_keeps_full_text_and_pair_provenance(self):
        manifest = self.build()
        self.assertEqual(len(manifest["questions"]), 12)
        self.assertEqual(manifest["target_reviews"], 3)
        self.assertEqual(manifest["source"]["cohort"], "prepared_train_test")
        self.assertIn("not a held-out", manifest["source"]["cohort_note"])
        for dataset in sampler.DATASETS:
            questions = [q for q in manifest["questions"] if q["dataset"] == dataset]
            self.assertEqual({q["qid"] for q in questions}, {"q1", "q2", "q3", "q4"})
            self.assertEqual(len({q["id"] for q in questions}), 4)
            for question in questions:
                self.assertEqual(question["positive_document"], self.full_text + question["pcid"])
                self.assertEqual(question["negative_document"], self.full_text + question["ncid"])
                source = question["source"]
                rows = [row for _, row in sampler.read_jsonl(self.root / source["pair_file"])]
                self.assertEqual(rows[source["pair_index"]]["qid"], question["qid"])
                self.assertEqual(source["pair_line_number"], source["pair_index"] + 1)
                if question["qid"] == "q1":
                    self.assertEqual(source["pairs_for_question_in_pool"], 3)
            self.assertEqual(manifest["source"]["datasets"][dataset]["pool_pairs"], 6)
            self.assertEqual(manifest["source"]["datasets"][dataset]["pool_unique_questions"], 4)

    def test_reproducible_and_seed_changes_sample(self):
        first = self.build(per_dataset=2, seed=17)
        self.assertEqual(first, self.build(per_dataset=2, seed=17))
        second = self.build(per_dataset=2, seed=91)
        self.assertNotEqual(first["questions"], second["questions"])

    def test_saved_sample_is_explicit_and_records_its_own_order(self):
        manifest = self.build(samples_root=Path("evaluation-sample"))
        self.assertEqual(manifest["source"]["cohort"], "saved_sample")
        for question in manifest["questions"]:
            source = question["source"]
            self.assertTrue(source["pair_file"].endswith("10000_sample_ids.jsonl"))
            self.assertEqual(source["split"], "saved_sample")
            self.assertEqual(list(reversed(self.pairs))[source["pair_index"]]["qid"], question["qid"])

    def test_never_falls_back_when_requested_sample_is_missing(self):
        (self.saved / "nq" / "10000_sample_ids.jsonl").unlink()
        with self.assertRaises(FileNotFoundError):
            self.build(samples_root=self.saved)

    def test_rejects_wrong_source_count_and_insufficient_unique_questions(self):
        with self.assertRaisesRegex(ValueError, "Expected 10,000 source pairs"):
            self.build(expected_pairs=10000)
        with self.assertRaisesRegex(ValueError, "need 5 distinct questions"):
            self.build(per_dataset=5)

    def test_rejects_duplicate_pair_across_preparation_splits(self):
        path = self.prepared / "nq" / "test_pairs.jsonl"
        write_jsonl(path, [self.pairs[0], self.pairs[5]])
        with self.assertRaisesRegex(ValueError, "duplicate document pair"):
            self.build()

    def test_rejects_same_positive_and_negative(self):
        path = self.prepared / "nq" / "test_pairs.jsonl"
        write_jsonl(path, [self.pairs[4], {"qid": "q4", "pcid": "p4", "ncid": "p4"}])
        with self.assertRaisesRegex(ValueError, "positive and negative IDs match"):
            self.build()

    def test_missing_documents_are_errors_not_silently_resampled(self):
        path = self.root / "data" / "corpus" / "nq.jsonl"
        write_jsonl(path, [row for _, row in sampler.read_jsonl(path) if row["id"] != "n4"])
        with self.assertRaisesRegex(ValueError, "missing 1 requested IDs"):
            self.build()

    def test_empty_reference_answer_is_an_error(self):
        path = self.root / "data" / "qa" / "nq.jsonl"
        rows = [row for _, row in sampler.read_jsonl(path)]
        rows[0]["answer"] = " "
        write_jsonl(path, rows)
        with self.assertRaisesRegex(ValueError, "needs nonempty answer"):
            self.build()

    def test_identifier_rules_preserve_leading_zeros_and_numeric_ids(self):
        self.assertEqual(sampler.identifier(42, "test"), "42")
        self.assertEqual(sampler.identifier("0042", "test"), "0042")
        for value in (None, True, False, " ", [], float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                sampler.identifier(value, "test")

    def test_rejects_conflicting_cohorts_and_invalid_study_id(self):
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            self.build(prepared_root=self.prepared, samples_root=self.saved)
        with self.assertRaisesRegex(ValueError, "study_id"):
            self.build(study_id="study#invalid")

    def test_cli_does_not_overwrite_without_explicit_flag(self):
        output = self.root / "private" / "questions.json"
        args = ["--repo-root", str(self.root), "--output", str(output), "--per-dataset", "4", "--expected-pairs", "6"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sampler.main(args), 0)
        before = output.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sampler.main(args + ["--seed", "12"]), 1)
        self.assertEqual(output.read_bytes(), before)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sampler.main(args + ["--seed", "12", "--overwrite"]), 0)
        self.assertNotEqual(output.read_bytes(), before)
        self.assertEqual(json.loads(output.read_text())["seed"], 12)


if __name__ == "__main__":
    unittest.main()
