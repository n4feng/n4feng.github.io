#!/usr/bin/env python3
"""Build and deploy the tracker from AWS CloudShell or a configured workstation.

Requires Python 3.9+, pip, make, AWS CLI, and SAM CLI. Nothing is deployed until
this script is run by the operator. Secret values never appear in CLI arguments,
SAM configuration, committed files, or the change-set summary.
"""

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]


class DeploymentError(Exception):
    pass


def aws(arguments, region, *, check=True):
    result = subprocess.run(
        ["aws", *arguments, "--region", region, "--output", "json", "--no-cli-pager"],
        text=True, capture_output=True, check=False,
    )
    if check and result.returncode:
        raise DeploymentError(result.stderr.strip() or "AWS CLI request failed.")
    return result


def aws_json(arguments, region):
    output = aws(arguments, region).stdout.strip()
    return json.loads(output) if output else {}


def protected_request(arguments, payload, region):
    """Use a mode-0600 temporary file instead of putting secrets in process argv."""
    with tempfile.TemporaryDirectory(prefix="experiment-deploy-") as directory:
        path = Path(directory) / "request.json"
        with path.open("x", encoding="utf-8") as handle:
            os.chmod(path, 0o600)
            json.dump(payload, handle)
        return aws_json([*arguments, "--cli-input-json", f"file://{path}"], region)


def confirm(message, assume_yes):
    if not assume_yes and input(f"{message} [y/N] ").strip().lower() not in {"y", "yes"}:
        raise DeploymentError("Cancelled; no further resources will be changed.")


def stack_exists(stack, region):
    result = aws(["cloudformation", "describe-stacks", "--stack-name", stack], region, check=False)
    if result.returncode == 0:
        status = json.loads(result.stdout)["Stacks"][0]["StackStatus"]
        if status.endswith("_IN_PROGRESS") or status in {"ROLLBACK_COMPLETE", "DELETE_FAILED"}:
            raise DeploymentError(f"Stack is {status}; resolve that state in CloudFormation before deploying.")
        return True
    if "does not exist" in result.stderr:
        return False
    raise DeploymentError(result.stderr.strip())


def ensure_artifact_bucket(bucket, account_id, region, assume_yes):
    result = aws(
        ["s3api", "head-bucket", "--bucket", bucket, "--expected-bucket-owner", account_id],
        region, check=False,
    )
    if result.returncode == 0:
        location = aws_json(["s3api", "get-bucket-location", "--bucket", bucket], region).get("LocationConstraint")
        location = "us-east-1" if location is None else "eu-west-1" if location == "EU" else location
        if location != region:
            raise DeploymentError(f"Artifact bucket is in {location}; choose a bucket in {region}.")
        return
    if "404" not in result.stderr and "Not Found" not in result.stderr:
        raise DeploymentError(f"Cannot verify ownership/access for artifact bucket {bucket}: {result.stderr.strip()}")
    confirm(f"Create private deployment-artifact bucket {bucket}? S3 storage is billable", assume_yes)
    arguments = ["s3api", "create-bucket", "--bucket", bucket]
    if region != "us-east-1":
        arguments += ["--create-bucket-configuration", f"LocationConstraint={region}"]
    aws_json(arguments, region)
    aws_json([
        "s3api", "put-public-access-block", "--bucket", bucket,
        "--public-access-block-configuration",
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true",
    ], region)
    protected_request(["s3api", "put-bucket-encryption"], {
        "Bucket": bucket,
        "ServerSideEncryptionConfiguration": {
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}],
        },
    }, region)


def make_parameters(args, existing):
    parameters = []
    if not existing or args.rotate_access_code:
        code = getpass.getpass("Root access code (Enter uses the documented default 666666): ") or "666666"
        if not 6 <= len(code) <= 128:
            raise DeploymentError("Access code must contain 6–128 characters.")
        parameters.append({"ParameterKey": "RootAccessCode", "ParameterValue": code})
    else:
        parameters.append({"ParameterKey": "RootAccessCode", "UsePreviousValue": True})
    if not existing or args.rotate_session_secret:
        parameters.append({"ParameterKey": "SessionSecret", "ParameterValue": secrets.token_urlsafe(48)})
    else:
        parameters.append({"ParameterKey": "SessionSecret", "UsePreviousValue": True})
    optional = {
        "CorsOrigins": (args.cors_origins, "https://n4feng.github.io"),
        "ReservedConcurrency": (args.reserved_concurrency, -1),
    }
    for name, (value, default) in optional.items():
        if value is None and existing:
            parameters.append({"ParameterKey": name, "UsePreviousValue": True})
        else:
            parameters.append({"ParameterKey": name, "ParameterValue": str(default if value is None else value)})
    return parameters


def wait_for_change_set(change_set_id, region):
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        change_set = aws_json(["cloudformation", "describe-change-set", "--change-set-name", change_set_id], region)
        if change_set["Status"] == "CREATE_COMPLETE":
            return change_set
        if change_set["Status"] == "FAILED":
            reason = change_set.get("StatusReason", "Change-set creation failed.")
            if "didn't contain changes" in reason or "No updates are to be performed" in reason:
                return None
            raise DeploymentError(reason)
        time.sleep(3)
    raise DeploymentError("Timed out waiting for the change set; inspect CloudFormation before retrying.")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--stack-name", default="experiment-tracker")
    parser.add_argument("--artifact-bucket", help="Existing owned bucket, or a new bucket to create, in the deployment region.")
    parser.add_argument("--cors-origins", help="Exact comma-separated origins; omitted updates retain the previous setting.")
    parser.add_argument("--reserved-concurrency", type=int, help="-1 removes reservation, 0 pauses API, 1–20 caps concurrent executions.")
    parser.add_argument("--rotate-access-code", action="store_true", help="Prompt for a new access code on an update.")
    parser.add_argument("--rotate-session-secret", action="store_true", help="Generate a new signing key and invalidate existing sessions.")
    parser.add_argument("--yes", action="store_true", help="Approve the bucket and reviewed-template change set without interactive confirmation.")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9-]{0,47}", args.stack_name):
        parser.error("--stack-name must start with a letter and contain at most 48 letters, digits, or hyphens.")
    if args.reserved_concurrency is not None and not -1 <= args.reserved_concurrency <= 20:
        parser.error("--reserved-concurrency must be between -1 and 20.")
    return args


def main(argv=None):
    args = parse_args(argv)
    for command in ("aws", "sam", "python3", "make"):
        if shutil.which(command) is None:
            raise DeploymentError(f"Install {command} before running this helper; see DEPLOY_AWS.md.")
    subprocess.run(["python3", "-m", "pip", "--version"], check=True, capture_output=True)
    identity = aws_json(["sts", "get-caller-identity"], args.region)
    existing = stack_exists(args.stack_name, args.region)
    account = identity["Account"]
    bucket = args.artifact_bucket or f"experiment-tracker-artifacts-{account}-{args.region}"
    print(f"AWS account: {account}\nRegion: {args.region}\nStack: {args.stack_name}\nArtifact bucket: {bucket}")
    print("The template creates Lambda, an HTTPS Function URL, a retained DynamoDB table, IAM permissions, and seven-day logs.")
    print("No EC2, VPC, NAT Gateway, load balancer, API Gateway, or paid DNS service is created. Usage is still billable.")
    parameters = make_parameters(args, existing)

    subprocess.run(["sam", "build", "--template-file", str(ROOT / "template.yaml")], cwd=ROOT, check=True)
    ensure_artifact_bucket(bucket, account, args.region, args.yes)
    packaged = ROOT / ".aws-sam" / "packaged.yaml"
    subprocess.run([
        "sam", "package", "--template-file", str(ROOT / ".aws-sam" / "build" / "template.yaml"),
        "--s3-bucket", bucket, "--s3-prefix", args.stack_name,
        "--output-template-file", str(packaged), "--region", args.region,
    ], cwd=ROOT, check=True)

    change_set = protected_request(["cloudformation", "create-change-set"], {
        "StackName": args.stack_name,
        "ChangeSetName": f"tracker-{int(time.time())}-{secrets.token_hex(3)}",
        "ChangeSetType": "UPDATE" if existing else "CREATE",
        "TemplateBody": packaged.read_text(encoding="utf-8"),
        "Parameters": parameters,
        "Capabilities": ["CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND"],
        "Description": "Experiment tracker deployment; secrets passed through NoEcho parameters.",
    }, args.region)
    changes = wait_for_change_set(change_set["Id"], args.region)
    if changes is None:
        aws_json(["cloudformation", "delete-change-set", "--change-set-name", change_set["Id"]], args.region)
        print("No infrastructure or code changes to deploy.")
    else:
        print("\nCloudFormation changes:")
        for entry in changes.get("Changes", []):
            resource = entry["ResourceChange"]
            print(f"  {resource['Action']:7} {resource['LogicalResourceId']} ({resource['ResourceType']})")
        try:
            confirm("Execute this change set?", args.yes)
        except DeploymentError:
            aws_json(["cloudformation", "delete-change-set", "--change-set-name", change_set["Id"]], args.region)
            if not existing:
                # CREATE change sets reserve an empty REVIEW_IN_PROGRESS stack.
                # Remove that placeholder so a cancelled first deploy is retryable.
                aws_json(["cloudformation", "delete-stack", "--stack-name", args.stack_name], args.region)
            raise
        aws_json(["cloudformation", "execute-change-set", "--change-set-name", change_set["Id"]], args.region)
        waiter = "stack-update-complete" if existing else "stack-create-complete"
        aws(["cloudformation", "wait", waiter, "--stack-name", args.stack_name], args.region)
    stack = aws_json(["cloudformation", "describe-stacks", "--stack-name", args.stack_name], args.region)["Stacks"][0]
    print("\nDeployment outputs (no secrets):")
    for output in stack.get("Outputs", []):
        print(f"  {output['OutputKey']}: {output['OutputValue']}")
    print(f"  ArtifactBucket: {bucket}")
    print("The DynamoDB table and deployment bucket remain after stack deletion. See DEPLOY_AWS.md for backup and cleanup.")


if __name__ == "__main__":
    try:
        main()
    except (DeploymentError, subprocess.CalledProcessError, KeyboardInterrupt) as error:
        print(f"Deployment stopped: {error}", file=sys.stderr)
        sys.exit(1)
