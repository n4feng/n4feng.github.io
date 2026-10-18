"""Deployment-helper checks; no AWS commands or provisioning are performed."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


path = Path(__file__).resolve().parents[2] / "scripts" / "deploy_aws.py"
specification = importlib.util.spec_from_file_location("deployment_helper", path)
deploy = importlib.util.module_from_spec(specification)
specification.loader.exec_module(deploy)


class DeploymentHelperTests(unittest.TestCase):
    def arguments(self, **overrides):
        return SimpleNamespace(
            **{"rotate_access_code": False, "rotate_session_secret": False, "cors_origins": None, "reserved_concurrency": None, **overrides}
        )

    def test_updates_preserve_signing_key_access_code_and_optional_settings(self):
        with patch.object(deploy.getpass, "getpass", side_effect=AssertionError("Updates must not prompt for a new code")), patch.object(deploy.secrets, "token_urlsafe", side_effect=AssertionError("Updates must not rotate sessions")):
            parameters = deploy.make_parameters(self.arguments(), existing=True)
        self.assertEqual({item["ParameterKey"] for item in parameters}, {"RootAccessCode", "SessionSecret", "CorsOrigins", "ReservedConcurrency"})
        self.assertTrue(all(item.get("UsePreviousValue") is True and "ParameterValue" not in item for item in parameters))

    def test_first_deployment_generates_a_key_and_honors_default_access_code(self):
        with patch.object(deploy.getpass, "getpass", return_value=""), patch.object(deploy.secrets, "token_urlsafe", return_value="test-random-signing-key" * 3):
            parameters = {item["ParameterKey"]: item for item in deploy.make_parameters(self.arguments(), existing=False)}
        self.assertEqual(parameters["RootAccessCode"]["ParameterValue"], "666666")
        self.assertGreaterEqual(len(parameters["SessionSecret"]["ParameterValue"]), 32)
        self.assertEqual(parameters["CorsOrigins"]["ParameterValue"], "https://n4feng.github.io")
        self.assertEqual(parameters["ReservedConcurrency"]["ParameterValue"], "-1")

    def test_explicit_access_code_rotation_does_not_rotate_signing_key(self):
        with patch.object(deploy.getpass, "getpass", return_value="new-private-code"):
            parameters = {item["ParameterKey"]: item for item in deploy.make_parameters(self.arguments(rotate_access_code=True, cors_origins="https://example.com"), existing=True)}
        self.assertEqual(parameters["RootAccessCode"]["ParameterValue"], "new-private-code")
        self.assertTrue(parameters["SessionSecret"]["UsePreviousValue"])
        self.assertEqual(parameters["CorsOrigins"]["ParameterValue"], "https://example.com")

    def test_secret_request_uses_private_file_not_process_arguments_and_cleans_up(self):
        payload = {"Parameters": [{"ParameterKey": "RootAccessCode", "ParameterValue": "private-test-code"}]}
        observed = []

        def invoke(arguments, region):
            self.assertEqual(region, "us-east-1")
            self.assertNotIn("private-test-code", " ".join(arguments))
            file_argument = arguments[arguments.index("--cli-input-json") + 1]
            self.assertTrue(file_argument.startswith("file://"))
            request_path = Path(file_argument.removeprefix("file://"))
            self.assertEqual(request_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(request_path.read_text()), payload)
            observed.append(request_path)
            return {"Id": "fake-change-set"}

        with patch.object(deploy, "aws_json", side_effect=invoke):
            self.assertEqual(deploy.protected_request(["cloudformation", "create-change-set"], payload, "us-east-1"), {"Id": "fake-change-set"})
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].exists())

    def test_secret_file_is_removed_after_failed_aws_request(self):
        observed = []

        def fail(arguments, _region):
            observed.append(Path(arguments[-1].removeprefix("file://")))
            raise deploy.DeploymentError("simulated AWS failure")

        with patch.object(deploy, "aws_json", side_effect=fail):
            with self.assertRaises(deploy.DeploymentError):
                deploy.protected_request(["cloudformation", "create-change-set"], {"secret": "private"}, "us-east-1")
        self.assertFalse(observed[0].exists())


if __name__ == "__main__":
    unittest.main()
