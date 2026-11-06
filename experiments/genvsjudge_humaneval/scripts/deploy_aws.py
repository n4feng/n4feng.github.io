#!/usr/bin/env python3
"""Deploy from AWS CloudShell or any workstation with boto3 and AWS credentials.

No SAM, Docker, GPU, or Node build is required. Questions stay in a private S3
deployment bundle and Lambda; only blinded assignments are served to browsers.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import re
import secrets
import sys
import time
from urllib.parse import urlsplit
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def build_bundle(questions_path: Path) -> tuple[bytes, dict]:
    # Import only after CLI parsing so --help works without AWS packages.
    sys.path.insert(0, str(ROOT / "server"))
    from lambda_handler import validate_manifest

    manifest = json.loads(questions_path.read_text(encoding="utf-8"))
    validate_manifest(manifest)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((ROOT / "server").glob("*.py")):
            archive.write(path, path.name)
        archive.writestr("questions.json", json.dumps(manifest, ensure_ascii=False))
    if len(stream.getvalue()) > 50 * 1024 * 1024:
        raise ValueError("Deployment ZIP exceeds 50 MiB; inspect unexpectedly large passages.")
    return stream.getvalue(), manifest


def validate_origins(value: str) -> str:
    origins = [part.strip().rstrip("/") for part in value.split(",")]
    for origin in origins:
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("Allowed origins must be exact HTTPS origins, e.g. https://n4feng.github.io")
    return ",".join(dict.fromkeys(origins))


def current_stack(client, name):
    from botocore.exceptions import ClientError
    try:
        return client.describe_stacks(StackName=name)["Stacks"][0]
    except ClientError as error:
        if error.response["Error"]["Code"] == "ValidationError" and "does not exist" in str(error):
            return None
        raise


def ensure_bucket(client, bucket, account, region):
    from botocore.exceptions import ClientError
    try:
        client.head_bucket(Bucket=bucket, ExpectedBucketOwner=account)
    except ClientError as error:
        if str(error.response["Error"]["Code"]) not in {"404", "NoSuchBucket", "NotFound"}:
            raise
        options = {"Bucket": bucket}
        if region != "us-east-1":
            options["CreateBucketConfiguration"] = {"LocationConstraint": region}
        client.create_bucket(**options)
    location = client.get_bucket_location(Bucket=bucket, ExpectedBucketOwner=account)["LocationConstraint"] or "us-east-1"
    if location != region:
        raise ValueError(f"Artifact bucket is in {location}; deployment region is {region}.")
    client.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True,
        "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
    })
    client.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={
        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
    })


def protect_existing_study(s3, stack, manifest):
    """Never attach existing votes to resampled or edited question contents."""
    params = {p["ParameterKey"]: p.get("ParameterValue") for p in stack.get("Parameters", [])}
    if not params.get("CodeBucket") or not params.get("CodeKey"):
        raise ValueError("Existing stack is not a GenVsJudge audit deployment; use a different stack name.")
    response = s3.get_object(Bucket=params["CodeBucket"], Key=params["CodeKey"])
    with zipfile.ZipFile(io.BytesIO(response["Body"].read())) as archive:
        previous = json.loads(archive.read("questions.json"))
    if previous["study_id"] != manifest["study_id"]:
        raise ValueError("Each stack is bound to one study. Use a NEW --stack-name for a different --study-id.")
    before = {q["id"]: q for q in previous["questions"]}
    after = {q["id"]: q for q in manifest["questions"]}
    if before != after:
        raise ValueError("This study already has a different question manifest. Keep it unchanged or use a NEW --study-id and --stack-name.")


def wait_for_stack(client, name, timeout=900):
    deadline, previous = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        stack = current_stack(client, name)
        state = stack["StackStatus"] if stack else "DELETED"
        if state != previous:
            print(f"CloudFormation: {state}", flush=True)
            previous = state
        if state in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
            return stack
        if not state.endswith("_IN_PROGRESS"):
            events = client.describe_stack_events(StackName=name)["StackEvents"]
            reasons = [f"{e['LogicalResourceId']}: {e.get('ResourceStatusReason', '')}" for e in events if e["ResourceStatus"].endswith("FAILED")]
            raise RuntimeError("Deployment failed: " + state + "\n" + "\n".join(reasons[:5]))
        time.sleep(10)
    raise TimeoutError("Stack is still running. Check CloudFormation before retrying deployment.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, required=True, help="Private 300-question manifest from sample_questions.py")
    parser.add_argument("--stack-name", default="genvsjudge-humaneval")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--profile", help="Optional AWS shared credentials profile")
    parser.add_argument("--artifact-bucket", help="Dedicated private deployment bucket; default includes account and region")
    parser.add_argument("--allowed-origins", default="https://n4feng.github.io")
    parser.add_argument("--reserved-concurrency", type=int, default=-1, help="-1: account default; 0: paused; 1..20: execution limit (not a cost cap)")
    parser.add_argument("--output", type=Path, default=ROOT / "deployment.json", help="Save non-secret deployment outputs (gitignored)")
    parser.add_argument("--validate-only", action="store_true", help="Validate and package locally; make no AWS calls")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,55}", args.stack_name):
        parser.error("Use a stack name starting with a letter, at most 56 letters/digits/hyphens.")
    if not -1 <= args.reserved_concurrency <= 20:
        parser.error("--reserved-concurrency must be -1..20")
    origins = validate_origins(args.allowed_origins)
    bundle, manifest = build_bundle(args.questions)
    print(f"Validated {len(manifest['questions'])} questions; study {manifest['study_id']}; ZIP {len(bundle):,} bytes.")
    if args.validate_only:
        return
    import boto3
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    s3, cf = session.client("s3"), session.client("cloudformation")
    stack = current_stack(cf, args.stack_name)
    if stack and stack["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"}:
        raise ValueError(f"Existing stack is {stack['StackStatus']}; resolve it before updating.")
    if stack:
        protect_existing_study(s3, stack, manifest)
    bucket = args.artifact_bucket or f"genvsjudge-audit-{account}-{args.region}"
    ensure_bucket(s3, bucket, account, args.region)
    key = f"{args.stack_name}/{uuid.uuid4().hex}.zip"
    s3.put_object(Bucket=bucket, Key=key, Body=bundle, ContentType="application/zip", ServerSideEncryption="AES256")
    parameters = [
        {"ParameterKey": "CodeBucket", "ParameterValue": bucket},
        {"ParameterKey": "CodeKey", "ParameterValue": key},
        {"ParameterKey": "AllowedOrigins", "ParameterValue": origins},
        {"ParameterKey": "ReservedConcurrency", "ParameterValue": str(args.reserved_concurrency)},
        {"ParameterKey": "SessionSecret", **({"UsePreviousValue": True} if stack else {"ParameterValue": secrets.token_urlsafe(48)})},
    ]
    options = {"StackName": args.stack_name, "TemplateBody": (ROOT / "scripts" / "template.json").read_text(),
               "Parameters": parameters, "Capabilities": ["CAPABILITY_IAM"]}
    print(f"Deploying to AWS account {account}, region {args.region}, stack {args.stack_name}.", flush=True)
    if stack:
        cf.update_stack(**options)
    else:
        cf.create_stack(**options, Tags=[{"Key": "Project", "Value": "GenVsJudge-HumanEval"}])
    deployed = wait_for_stack(cf, args.stack_name)
    outputs = {item["OutputKey"]: item["OutputValue"] for item in deployed.get("Outputs", [])}
    outputs.update({"study_id": manifest["study_id"], "region": args.region, "stack_name": args.stack_name})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(outputs, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(outputs, indent=2))
    print("Open https://n4feng.github.io/experiments/genvsjudge_humaneval/ and enter ApiUrl with your name.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Deployment error: {error}", file=sys.stderr)
        sys.exit(1)
