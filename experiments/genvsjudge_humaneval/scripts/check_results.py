#!/usr/bin/env python3
"""Show current human-audit progress and judgments, by dataset and in total.

By default, read private responses from DynamoDB with your AWS credentials and
print aggregates only. Use --progress-only for public counts without credentials.
No database records are changed and no participant names or notes are printed.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from export_responses import (
    DATASETS, RATINGS, ROOT, build_reports, fetch_responses, validate_questions,
)


SERVER_URL = "https://fisjwglhwhqjmox7xgulvddsge0zuibi.lambda-url.us-east-1.on.aws"
DEFAULT_STACK = "genvsjudge-humaneval"


def normalize_server_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("Use the HTTPS base Lambda URL, without /api, credentials, or query parameters.")
    return value


def request_json(server_url: str, path: str, timeout: float = 30) -> dict:
    request = Request(server_url + path, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except HTTPError as error:
        raise ValueError(f"{path} returned HTTP {error.code}; verify the audit Lambda URL.") from error
    except URLError as error:
        raise ValueError(f"Cannot reach {server_url}: {error.reason}") from error
    except (ValueError, UnicodeError) as error:
        raise ValueError(f"{path} did not return valid JSON.") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must return a JSON object.")
    return payload


def validate_health(health: dict, study_id: str | None = None) -> None:
    if (health.get("ok") is not True or type(health.get("question_count")) is not int
            or health["question_count"] != 300 or not health.get("study_id")
            or not isinstance(health["study_id"], str)):
        raise ValueError("This endpoint is not a ready 300-question audit. Use the audit Lambda URL, not the experiment tracker.")
    if study_id is not None and health["study_id"] != study_id:
        raise ValueError("The supplied questions.json study_id differs from the deployed audit.")


def validate_stats(stats: dict, health: dict) -> None:
    if stats.get("study_id") != health["study_id"]:
        raise ValueError("Health and statistics refer to different studies; retry after checking the deployment.")
    if stats.get("total_questions") != 300 or stats.get("target_reviews") != 3:
        raise ValueError("Expected statistics for 300 questions with a target of three reviews.")
    groups = stats.get("by_dataset")
    if not isinstance(groups, dict) or set(groups) != set(DATASETS):
        raise ValueError("Statistics must include nq, hotpotqa, and msmarco.")
    fields = ("total_questions", "min_reviews", "total_reviews", "questions_at_target")
    for group in [stats, *groups.values()]:
        if not isinstance(group, dict) or any(type(group.get(key)) is not int or group[key] < 0 for key in fields):
            raise ValueError("Statistics contain missing or invalid review counts.")
        if group["questions_at_target"] > group["total_questions"]:
            raise ValueError("Statistics contain an invalid count of completed questions.")
    if any(group["total_questions"] != 100 for group in groups.values()):
        raise ValueError("Expected 100 questions in each dataset.")
    for field in ("total_questions", "total_reviews", "questions_at_target"):
        if stats[field] != sum(group[field] for group in groups.values()):
            raise ValueError(f"Dataset statistics do not sum to the reported {field}.")
    if stats["min_reviews"] != min(group["min_reviews"] for group in groups.values()):
        raise ValueError("Dataset minima do not match the reported minimum.")
    if type(stats.get("remaining_reviews")) is not int or stats["remaining_reviews"] < 0:
        raise ValueError("Statistics contain an invalid remaining review count.")


def resolve_questions(path: Path | None = None) -> Path:
    candidates = [path.expanduser()] if path is not None else [ROOT / "data/questions.json", Path.home() / "questions.json"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "The deployed questions.json is required for complete results, including untouched questions. "
        "Pass --questions /path/to/questions.json, or use --progress-only for public progress."
    )


def table_from_stack(client, stack_name: str, server_url: str) -> str:
    stacks = client.describe_stacks(StackName=stack_name).get("Stacks", [])
    if len(stacks) != 1:
        raise ValueError(f"Expected one CloudFormation stack: {stack_name}")
    outputs = {item["OutputKey"]: item["OutputValue"] for item in stacks[0].get("Outputs", [])}
    if not outputs.get("ApiUrl") or not outputs.get("TableName"):
        raise ValueError(f"Stack {stack_name} must have ApiUrl and TableName outputs.")
    if normalize_server_url(outputs["ApiUrl"]) != server_url:
        raise ValueError(
            f"Stack {stack_name} belongs to a different Lambda URL. "
            "Use the matching --stack-name or --server-url."
        )
    return outputs["TableName"]


def summarize_results(manifest: dict, responses: list[dict], *, strict: bool = True) -> dict:
    """Reuse the export's validation and majority rules; retain only aggregates."""
    _, question_rows, summary = build_reports(manifest, responses, strict=strict)
    summary.pop("source", None)
    groups = [(summary, question_rows)] + [
        (summary["by_dataset"][dataset], [row for row in question_rows if row["dataset"] == dataset])
        for dataset in DATASETS
    ]
    for group, rows in groups:
        group["questions_below_target"] = sum(not row["at_target"] for row in rows)
        for role in ("positive", "negative"):
            group[f"{role}_rating_vote_counts"] = {
                rating: sum(row[f"{role}_{rating}_votes"] for row in rows)
                for rating in RATINGS
            }
            group[f"{role}_rating_majority_question_counts"] = {
                **{rating: sum(row[f"{role}_majority"] == rating for row in rows) for rating in RATINGS},
                "no_majority": sum(row[f"{role}_majority"] is None for row in rows),
                "no_majority_at_target": sum(row["at_target"] and row[f"{role}_majority"] is None for row in rows),
            }
    summary["mode"] = "dynamodb"
    return summary


def _table(headers: tuple[str, ...], rows: list[tuple]) -> str:
    cells = [[str(value) for value in row] for row in [headers, *rows]]
    widths = [max(len(row[index]) for row in cells) for index in range(len(headers))]
    return "\n".join(
        "  ".join(value.ljust(widths[index]) if index == 0 else value.rjust(widths[index])
                  for index, value in enumerate(row)).rstrip()
        for row in cells
    )


def format_report(report: dict) -> str:
    groups = [(dataset, report["by_dataset"][dataset]) for dataset in DATASETS] + [("TOTAL", report)]
    target = report["target_reviews"]
    lines = [f"Human audit: {report['study_id']}"]
    if "server_url" in report:
        lines.append(f"Server: {report['server_url']}")
    if "retrieved_at" in report:
        lines.append(f"Retrieved (UTC): {report['retrieved_at']}")
    lines.extend(["", "Review progress", _table(
        ("Dataset", "Questions", "Reviews", "Min", f"At {target}+", "Remaining", "Reviewers"),
        [(name, group["total_questions"], group["total_reviews"], group["min_reviews"],
          group["questions_at_target"], group.get("remaining_reviews", "n/a"), group.get("distinct_reviewers", "n/a"))
         for name, group in groups],
    )])
    if report.get("mode") == "progress_only":
        lines.extend(["", "Public progress only: individual judgments and reviewer counts are private.",
                      "Per-dataset remaining counts are not supplied by the public API (n/a).",
                      "Run without --progress-only using AWS credentials and questions.json for results."])
        return "\n".join(lines)
    for role in ("positive", "negative"):
        lines.extend(["", f"Original {role} document: vote counts", _table(
            ("Dataset", "Supports", "Partial", "No support", "Unsure", "All votes"),
            [(name, *(group[f"{role}_rating_vote_counts"][rating] for rating in RATINGS), group["total_reviews"])
             for name, group in groups],
        ), "", f"Original {role} document: question majority counts", _table(
            ("Dataset", "Supports", "Partial", "No support", "Unsure", "No majority at 3+", "Fewer than 3"),
            [(name, *(group[f"{role}_rating_majority_question_counts"][rating] for rating in RATINGS),
              group[f"{role}_rating_majority_question_counts"]["no_majority_at_target"], group["questions_below_target"])
             for name, group in groups],
        )])
    lines.extend(["", "Each review rates both documents. Vote counts are not question counts.",
                  "A majority requires at least three reviews and strictly more than half agreeing.",
                  "Reviewers are distinct normalized self-reported names; TOTAL deduplicates across datasets.",
                  "These are ongoing descriptive judgments, not an established false-negative rate.",
                  "DynamoDB reads are consistent per page, not an atomic multi-page snapshot."])
    return "\n".join(lines)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--questions", type=Path, help="Unchanged deployed manifest (default: module data/questions.json, then ~/questions.json)")
    result.add_argument("--server-url", default=SERVER_URL, help="Audit Lambda base URL (default: the configured live study)")
    result.add_argument("--progress-only", action="store_true", help="Read public progress only; no AWS credentials or manifest needed")
    target = result.add_mutually_exclusive_group()
    target.add_argument("--stack-name", help=f"CloudFormation stack (default: {DEFAULT_STACK})")
    target.add_argument("--table-name", help="Explicit DynamoDB table; bypasses stack lookup/URL verification")
    result.add_argument("--region", default="us-east-1")
    result.add_argument("--profile", help="Optional named AWS credentials profile")
    result.add_argument("--timeout", type=float, default=30, help="HTTP request timeout in seconds (default: 30)")
    result.add_argument("--json", action="store_true", help="Print aggregate JSON instead of tables")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise ValueError("--timeout must be a finite number greater than zero.")
        server_url = normalize_server_url(args.server_url)
        health = request_json(server_url, "/api/health", timeout=args.timeout)
        validate_health(health)
        if args.progress_only:
            report = request_json(server_url, "/api/stats", timeout=args.timeout)
            validate_stats(report, health)
            report = {**report, "mode": "progress_only"}
        else:
            questions_path = resolve_questions(args.questions)
            manifest = json.loads(questions_path.read_text(encoding="utf-8"))
            validate_questions(manifest)
            validate_health(health, manifest["study_id"])
            import boto3
            session = boto3.Session(profile_name=args.profile, region_name=args.region)
            table_name = args.table_name or table_from_stack(
                session.client("cloudformation"), args.stack_name or DEFAULT_STACK, server_url,
            )
            started = datetime.now(timezone.utc).isoformat()
            responses = fetch_responses(session.resource("dynamodb").Table(table_name), manifest["study_id"])
            report = summarize_results(manifest, responses)
            report["collection"] = {
                "table_name": table_name, "region": args.region, "started_at": started,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "read_consistency": "Strongly consistent per page; not an atomic multi-page snapshot.",
                "stack_url_verified": not bool(args.table_name),
            }
        report["server_url"] = server_url
        report["retrieved_at"] = datetime.now(timezone.utc).isoformat()
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) if args.json else format_report(report))
        return 0
    except ImportError:
        print("Results require boto3. Install with: python -m pip install boto3. Or use --progress-only.", file=sys.stderr)
    except Exception as error:
        print(f"Could not check audit results: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
