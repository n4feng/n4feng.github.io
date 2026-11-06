#!/usr/bin/env python3
"""Privately export human-audit responses using your AWS credentials.

No public export endpoint exists. JSONL retains the original response records;
CSV files are intended for analysis and escape spreadsheet formula prefixes.
Majority tallies describe votes and never automatically change dataset labels.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
DATASETS = ("nq", "hotpotqa", "msmarco")
RATINGS = ("supports", "partial", "does_not_support", "unsure")
OUTPUT_FILES = ("responses.jsonl", "responses.csv", "questions.csv", "summary.json")
RESPONSE_FIELDS = (
    "study_id", "question_id", "dataset", "qid", "pcid", "ncid", "reviewer_id", "name",
    "positive_rating", "negative_rating", "displayed_A_rating", "displayed_B_rating",
    "document_A_role", "document_B_role", "notes", "created_at",
)
QUESTION_FIELDS = (
    "study_id", "question_id", "dataset", "qid", "pcid", "ncid", "question", "answer",
    "pair_file", "pair_index", "pair_line_number", "source_split", "review_count", "at_target",
    *(f"{role}_{rating}_votes" for role in ("positive", "negative") for rating in RATINGS),
    "positive_majority", "negative_majority",
)


def validate_questions(manifest: dict, *, strict: bool = True) -> dict[str, dict]:
    """Validate export identity without importing the Lambda runtime."""
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("Expected manifest schema_version 1")
    study_id = manifest.get("study_id")
    if not isinstance(study_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", study_id):
        raise ValueError("Invalid study_id")
    if manifest.get("target_reviews") != 3:
        raise ValueError("This audit requires target_reviews=3")
    if not isinstance(manifest.get("questions"), list) or not manifest["questions"]:
        raise ValueError("Manifest needs a nonempty questions list")
    questions, query_keys, sizes = {}, set(), Counter()
    for question in manifest["questions"]:
        if not isinstance(question, dict):
            raise ValueError("Each question must be an object")
        for field in ("id", "dataset", "qid", "pcid", "ncid", "question", "answer"):
            if not isinstance(question.get(field), str) or not question[field].strip():
                raise ValueError(f"Manifest question needs nonempty {field}")
        key = question["id"]
        dataset = question["dataset"]
        if key in questions or dataset not in DATASETS:
            raise ValueError("Duplicate question ID or unknown dataset")
        query_key = (dataset, question["qid"])
        if query_key in query_keys or question["pcid"] == question["ncid"]:
            raise ValueError("Duplicate question within dataset or matching document IDs")
        questions[key] = question
        query_keys.add(query_key)
        sizes[dataset] += 1
    if strict and (len(questions) != 300 or any(sizes[name] != 100 for name in DATASETS)):
        raise ValueError("Production export requires 100 questions per dataset, 300 total")
    return questions


def fetch_responses(table, study_id: str) -> list[dict]:
    """Read every response page consistently; counts are derived from votes."""
    options = {
        "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
        "ExpressionAttributeValues": {":pk": "STUDY#" + study_id, ":prefix": "RESPONSE#"},
        "ConsistentRead": True,
    }
    result = []
    while True:
        page = table.query(**options)
        result.extend(page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            return result
        options["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def table_from_stack(client, stack_name: str) -> str:
    stacks = client.describe_stacks(StackName=stack_name).get("Stacks", [])
    if len(stacks) != 1:
        raise ValueError(f"Expected one CloudFormation stack: {stack_name}")
    outputs = {entry["OutputKey"]: entry["OutputValue"] for entry in stacks[0].get("Outputs", [])}
    if not outputs.get("TableName"):
        raise ValueError(f"Stack {stack_name} has no TableName output")
    return outputs["TableName"]


def validate_response(row: dict, questions: dict, study_id: str, seen: set) -> None:
    if not isinstance(row, dict):
        raise ValueError("Each response must be an object")
    question = questions.get(row.get("question_id"))
    if question is None:
        raise ValueError(f"Response references unknown question: {row.get('question_id')!r}")
    for field in ("dataset", "qid", "pcid", "ncid"):
        if row.get(field) != question[field]:
            raise ValueError(f"Response {row['question_id']}: {field} differs from the supplied manifest")
    for field in ("reviewer_id", "name", "created_at"):
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise ValueError(f"Response {row['question_id']}: missing {field}")
    key = (row["question_id"], row["reviewer_id"])
    if key in seen:
        raise ValueError(f"Duplicate reviewer/question response: {key}")
    seen.add(key)
    if row.get("PK", "STUDY#" + study_id) != "STUDY#" + study_id:
        raise ValueError("Response belongs to a different study")
    ratings, displayed, order = row.get("ratings"), row.get("displayed_ratings"), row.get("document_order")
    if not isinstance(ratings, dict) or set(ratings) != {"positive", "negative"}:
        raise ValueError("Response needs original positive/negative ratings")
    if any(value not in RATINGS for value in ratings.values()):
        raise ValueError("Response has an invalid support rating")
    if not isinstance(displayed, dict) or set(displayed) != {"A", "B"}:
        raise ValueError("Response needs displayed A/B ratings")
    if not isinstance(order, dict) or set(order) != {"A", "B"} or set(order.values()) != {"positive", "negative"}:
        raise ValueError("Response needs a valid A/B document order")
    if any(displayed[position] != ratings[order[position]] for position in ("A", "B")):
        raise ValueError("Displayed and original-role ratings disagree")
    if not isinstance(row.get("notes", ""), str):
        raise ValueError("Response notes must be text")


def majority(tallies: Counter, count: int, minimum: int = 3) -> str | None:
    if count < minimum:
        return None
    return next((rating for rating in RATINGS if tallies[rating] * 2 > count), None)


def aggregate(question_rows: list[dict], responses: list[dict], target: int) -> dict:
    counts = [row["review_count"] for row in question_rows]
    negative_votes = {rating: sum(row[f"negative_{rating}_votes"] for row in question_rows) for rating in RATINGS}
    negative_majorities = {rating: sum(row["negative_majority"] == rating for row in question_rows) for rating in RATINGS}
    return {
        "total_questions": len(question_rows),
        "total_reviews": sum(counts),
        "distinct_reviewers": len({row["reviewer_id"] for row in responses}),
        "min_reviews": min(counts, default=0),
        "max_reviews": max(counts, default=0),
        "mean_reviews_per_question": sum(counts) / len(counts) if counts else 0.0,
        "questions_with_any_review": sum(count > 0 for count in counts),
        "questions_at_target": sum(count >= target for count in counts),
        "remaining_reviews": sum(max(0, target - count) for count in counts),
        "negative_rating_vote_counts": negative_votes,
        "negative_rating_majority_question_counts": {
            **negative_majorities,
            "no_majority": sum(row["negative_majority"] is None for row in question_rows),
        },
    }


def build_reports(manifest: dict, responses: list[dict], *, strict: bool = True) -> tuple[list, list, dict]:
    questions = validate_questions(manifest, strict=strict)
    study_id, target = manifest["study_id"], manifest["target_reviews"]
    grouped, seen, flat_responses = defaultdict(list), set(), []
    for row in sorted(responses, key=lambda entry: (str(entry.get("question_id", "")), str(entry.get("reviewer_id", "")))):
        validate_response(row, questions, study_id, seen)
        grouped[row["question_id"]].append(row)
        flat_responses.append({
            "study_id": study_id,
            **{field: row[field] for field in ("question_id", "dataset", "qid", "pcid", "ncid", "reviewer_id", "name", "created_at")},
            "positive_rating": row["ratings"]["positive"],
            "negative_rating": row["ratings"]["negative"],
            "displayed_A_rating": row["displayed_ratings"]["A"],
            "displayed_B_rating": row["displayed_ratings"]["B"],
            "document_A_role": row["document_order"]["A"],
            "document_B_role": row["document_order"]["B"],
            "notes": row.get("notes", ""),
        })
    question_rows = []
    for question_id, question in questions.items():
        votes = grouped[question_id]
        source = question.get("source", {})
        result = {
            "study_id": study_id,
            "question_id": question_id,
            **{field: question[field] for field in ("dataset", "qid", "pcid", "ncid", "question", "answer")},
            **{field: source.get(field) for field in ("pair_file", "pair_index", "pair_line_number")},
            "source_split": source.get("split"),
            "review_count": len(votes),
            "at_target": len(votes) >= target,
        }
        for role in ("positive", "negative"):
            tallies = Counter(row["ratings"][role] for row in votes)
            result.update({f"{role}_{rating}_votes": tallies[rating] for rating in RATINGS})
            result[f"{role}_majority"] = majority(tallies, len(votes), target)
        question_rows.append(result)
    summary = {
        "schema_version": 1,
        "study_id": study_id,
        "target_reviews": target,
        **aggregate(question_rows, responses, target),
        "by_dataset": {
            dataset: aggregate(
                [row for row in question_rows if row["dataset"] == dataset],
                [row for row in responses if row["dataset"] == dataset],
                target,
            )
            for dataset in DATASETS
        },
        "source": manifest.get("source", {}),
        "interpretation": {
            "reviews": "One response per self-reported reviewer name and question; identities are not independently verified.",
            "vote_counts": "Each response contributes one negative-document rating and one positive-document rating.",
            "majority_question_counts": "A rating needs strictly more than half of a question's votes and at least three total reviews. Ties and fewer than three reviews have no majority.",
            "labels": "These are human-audit judgments; no original dataset labels are automatically changed.",
        },
    }
    return flat_responses, question_rows, summary


def json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def spreadsheet_safe(value):
    """Keep potentially executable reviewer input inert when opening a CSV."""
    if isinstance(value, str) and (value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@"))):
        return "'" + value
    return value


def check_destinations(output_dir: Path, overwrite: bool) -> None:
    existing = [name for name in OUTPUT_FILES if (output_dir / name).exists()]
    if existing and not overwrite:
        raise FileExistsError(f"Export files already exist: {', '.join(existing)}. Use a new --output-dir or --overwrite.")


def write_reports(output_dir: Path, manifest: dict, responses: list[dict], *, overwrite=False, strict=True, metadata=None) -> dict:
    check_destinations(output_dir, overwrite)
    flat_responses, questions, summary = build_reports(manifest, responses, strict=strict)
    summary["export"] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "csv_text": "Potential spreadsheet formula prefixes are escaped with an apostrophe; raw text is preserved in responses.jsonl.",
        **(metadata or {}),
    }
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".export-", dir=output_dir) as staging:
        temporary = Path(staging)
        with (temporary / "responses.jsonl").open("w", encoding="utf-8") as handle:
            for row in responses:
                handle.write(json.dumps(row, ensure_ascii=False, default=json_default, allow_nan=False) + "\n")
        for filename, fields, rows in (
            ("responses.csv", RESPONSE_FIELDS, flat_responses),
            ("questions.csv", QUESTION_FIELDS, questions),
        ):
            with (temporary / filename).open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows({key: spreadsheet_safe(value) for key, value in row.items()} for row in rows)
        (temporary / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=json_default, allow_nan=False) + "\n", encoding="utf-8")
        check_destinations(output_dir, overwrite)
        for filename in OUTPUT_FILES:
            source = temporary / filename
            source.chmod(0o600)
            if overwrite:
                source.replace(output_dir / filename)
            else:
                # Hard-link creation is exclusive: a concurrent export cannot
                # silently replace private records between the check and write.
                import os
                os.link(source, output_dir / filename)
    return summary


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--questions", type=Path, required=True, help="The unchanged private questions.json deployed to Lambda")
    target = result.add_mutually_exclusive_group()
    target.add_argument("--stack-name", help="CloudFormation stack (default: genvsjudge-humaneval)")
    target.add_argument("--table-name", help="Explicit DynamoDB response table instead of stack lookup")
    result.add_argument("--region", default="us-east-1")
    result.add_argument("--profile", help="Optional named AWS credentials profile")
    result.add_argument("--output-dir", type=Path, default=ROOT / "exports")
    result.add_argument("--overwrite", action="store_true")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        check_destinations(args.output_dir, args.overwrite)
        manifest = json.loads(args.questions.read_text(encoding="utf-8"))
        validate_questions(manifest)
        import boto3
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        table_name = args.table_name or table_from_stack(session.client("cloudformation"), args.stack_name or "genvsjudge-humaneval")
        started = datetime.now(timezone.utc).isoformat()
        responses = fetch_responses(session.resource("dynamodb").Table(table_name), manifest["study_id"])
        finished = datetime.now(timezone.utc).isoformat()
        summary = write_reports(args.output_dir, manifest, responses, overwrite=args.overwrite, metadata={
            "table_name": table_name,
            "region": args.region,
            "collection_started_at": started,
            "collection_finished_at": finished,
            "read_consistency": "Strongly consistent per page; a multi-page query is not an atomic snapshot if reviews arrive during export.",
        })
        print(f"Exported {summary['total_reviews']} reviews for {summary['total_questions']} questions to {args.output_dir.resolve()}")
        print(f"Minimum reviews: {summary['min_reviews']}; questions at target: {summary['questions_at_target']}/{summary['total_questions']}")
        print("Exports contain participant names. Keep these files private.")
        return 0
    except ImportError:
        print("Export requires boto3. Install with: python -m pip install boto3", file=sys.stderr)
        return 1
    except Exception as error:
        # AWS errors are shown to the authenticated operator, never to visitors.
        print(f"Export failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
