import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import zipfile

from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("audit_deploy", ROOT / "scripts" / "deploy_aws.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


def manifest():
    return {"schema_version": 1, "study_id": "audit-v1", "target_reviews": 3, "questions": [
        {"id": f"{dataset}-{i}", "dataset": dataset, "qid": str(i), "pcid": "pos", "ncid": "neg",
         "question": "Question?", "answer": "Reference", "positive_document": "Complete positive text.",
         "negative_document": "Complete negative text."}
        for dataset in ("nq", "hotpotqa", "msmarco") for i in range(100)
    ]}


class DeploymentTests(unittest.TestCase):
    def test_bundle_contains_full_manifest_and_handler_only(self):
        data = manifest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "questions.json"
            path.write_text(json.dumps(data))
            bundle, result = deploy.build_bundle(path)
        self.assertEqual(result, data)
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            self.assertIn("lambda_handler.py", archive.namelist())
            self.assertEqual(json.loads(archive.read("questions.json")), data)
            self.assertTrue(all(name.endswith(".py") or name == "questions.json" for name in archive.namelist()))

    def test_production_rejects_wrong_question_count(self):
        data = manifest()
        data["questions"].pop()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "questions.json"
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "300"):
                deploy.build_bundle(path)

    def test_question_changes_cannot_reuse_study_votes(self):
        original = manifest()
        data = copy.deepcopy(original)
        data["questions"][0]["negative_document"] = "Different evidence."
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("questions.json", json.dumps(original))
        s3 = Mock()
        s3.get_object.side_effect = lambda **_: {"Body": io.BytesIO(stream.getvalue())}
        stack = {"Parameters": [{"ParameterKey": "CodeBucket", "ParameterValue": "bucket"},
                                {"ParameterKey": "CodeKey", "ParameterValue": "code.zip"}]}
        with self.assertRaisesRegex(ValueError, "NEW --study-id"):
            deploy.protect_existing_study(s3, stack, data)
        deploy.protect_existing_study(s3, stack, original)
        data["study_id"] = "new-study"
        with self.assertRaisesRegex(ValueError, "NEW --stack-name"):
            deploy.protect_existing_study(s3, stack, data)

    def test_exact_https_origins_required(self):
        self.assertEqual(deploy.validate_origins("https://n4feng.github.io/, https://n4feng.github.io"), "https://n4feng.github.io")
        for value in ("*", "http://example.org", "https://example.org/path", "https://user@example.org", "https://example.org?x=1", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                deploy.validate_origins(value)

    def test_access_denied_is_not_mistaken_for_missing_stack(self):
        client = Mock()
        client.describe_stacks.side_effect = ClientError({"Error": {"Code": "AccessDenied", "Message": "Denied"}}, "DescribeStacks")
        with self.assertRaises(ClientError):
            deploy.current_stack(client, "test")
        client.describe_stacks.side_effect = ClientError({"Error": {"Code": "ValidationError", "Message": "Stack test does not exist"}}, "DescribeStacks")
        self.assertIsNone(deploy.current_stack(client, "test"))


if __name__ == "__main__":
    unittest.main()
