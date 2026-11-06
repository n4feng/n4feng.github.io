"""Public human-audit API. Question roles and participant records stay server-side.

Names are self-reported, not verified identities. A normalized name contributes
at most one review to each question, across sessions and HTTP retries.
"""

from __future__ import annotations

import base64
from collections import Counter
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import random
import re
import time
import unicodedata

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError


LOG = logging.getLogger(__name__)
DATASETS = ("nq", "hotpotqa", "msmarco")
RATINGS = {"supports", "partial", "does_not_support", "unsure"}
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
SESSION_SECONDS = 30 * 24 * 60 * 60
ASSIGNMENT_SECONDS = 24 * 60 * 60
MAX_BODY_BYTES = 32_768
SERIALIZER = TypeSerializer()
DESERIALIZER = TypeDeserializer()


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _encode(data):
    return {key: SERIALIZER.serialize(value) for key, value in data.items()}


def _decode(data):
    return {key: DESERIALIZER.deserialize(value) for key, value in data.items()}


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(value):
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def normalize_name(value):
    if not isinstance(value, str) or len(value) > 200:
        raise ApiError(400, "Enter your name using 1–80 characters.")
    display = " ".join(unicodedata.normalize("NFKC", value).split())
    if not 1 <= len(display) <= 80 or any(unicodedata.category(c).startswith("C") for c in display):
        raise ApiError(400, "Enter your name using 1–80 visible characters.")
    normalized = display.casefold()
    reviewer_id = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return display, reviewer_id


def validate_manifest(manifest, *, strict=True):
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("Expected question manifest schema_version 1")
    study_id = manifest.get("study_id")
    if not isinstance(study_id, str) or not IDENTIFIER.fullmatch(study_id):
        raise ValueError("study_id must be a short identifier")
    if manifest.get("target_reviews") != 3:
        raise ValueError("This study requires target_reviews=3")
    questions = manifest.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError("The manifest must contain questions")
    ids, query_ids, sizes = set(), set(), Counter()
    for question in questions:
        if not isinstance(question, dict):
            raise ValueError("Each question must be an object")
        question_id = question.get("id")
        if not isinstance(question_id, str) or not IDENTIFIER.fullmatch(question_id) or question_id in ids:
            raise ValueError("Question IDs must be unique short identifiers")
        ids.add(question_id)
        dataset = question.get("dataset")
        if dataset not in DATASETS:
            raise ValueError("Unknown question dataset")
        for field in ("qid", "pcid", "ncid", "question", "answer", "positive_document", "negative_document"):
            if not isinstance(question.get(field), str) or not question[field].strip():
                raise ValueError(f"Question {question_id} needs a nonempty {field}")
        if question["pcid"] == question["ncid"]:
            raise ValueError("Positive and negative document IDs cannot match")
        query_key = (dataset, question["qid"])
        if query_key in query_ids:
            raise ValueError("Each dataset must contain unique question IDs")
        query_ids.add(query_key)
        sizes[dataset] += 1
    if strict and (len(questions) != 300 or any(sizes[dataset] != 100 for dataset in DATASETS)):
        raise ValueError("Production requires exactly 100 unique questions per dataset, 300 total")
    return {question["id"]: question for question in questions}


class DynamoStore:
    """Only vote creation and its counter increment mutate public-study data."""

    def __init__(self, table_name, study_id, client=None):
        self.table_name = table_name
        self.pk = "STUDY#" + study_id
        self.client = client or boto3.client("dynamodb")

    def _key(self, sk):
        return _encode({"PK": self.pk, "SK": sk})

    def _query(self, prefix):
        kwargs = {
            "TableName": self.table_name,
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
            "ExpressionAttributeValues": _encode({":pk": self.pk, ":prefix": prefix}),
            "ConsistentRead": True,
        }
        while True:
            page = self.client.query(**kwargs)
            yield from (_decode(item) for item in page.get("Items", []))
            if not page.get("LastEvaluatedKey"):
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def counts(self):
        return {item["question_id"]: int(item["count"]) for item in self._query("COUNT#")}

    def reviewed(self, reviewer_id):
        return {
            item["question_id"] for item in self._query("RESPONSE#")
            if item.get("reviewer_id") == reviewer_id
        }

    def response(self, question_id, reviewer_id):
        result = self.client.get_item(
            TableName=self.table_name,
            Key=self._key(f"RESPONSE#{question_id}#{reviewer_id}"),
            ConsistentRead=True,
        )
        return _decode(result["Item"]) if "Item" in result else None

    def save(self, response):
        question_id, reviewer_id = response["question_id"], response["reviewer_id"]
        item = {"PK": self.pk, "SK": f"RESPONSE#{question_id}#{reviewer_id}", **response}
        try:
            self.client.transact_write_items(TransactItems=[
                {"Put": {
                    "TableName": self.table_name,
                    "Item": _encode(item),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }},
                {"Update": {
                    "TableName": self.table_name,
                    "Key": self._key("COUNT#" + question_id),
                    "UpdateExpression": "SET question_id = :qid, dataset = :dataset ADD #count :one",
                    "ExpressionAttributeNames": {"#count": "count"},
                    "ExpressionAttributeValues": _encode({
                        ":qid": question_id, ":dataset": response["dataset"], ":one": 1,
                    }),
                }},
            ])
            return False
        except ClientError as error:
            if error.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            existing = self.response(question_id, reviewer_id)
            if existing is None:
                # Includes a concurrent transaction conflict; the client can retry
                # the exact response without risking an extra count.
                raise ApiError(503, "Another review is being saved. Please retry your submission.") from error
            fields = ("ratings", "displayed_ratings", "document_order", "notes")
            if all(existing.get(field) == response.get(field) for field in fields):
                return True
            raise ApiError(409, "You have already reviewed this question. The saved review was kept.") from error


class AuditApp:
    def __init__(self, manifest, table_name, secret, *, client=None, strict=True, clock=time.time):
        self.questions = validate_manifest(manifest, strict=strict)
        if not isinstance(secret, str) or len(secret) < 32:
            raise ValueError("SESSION_SECRET must contain at least 32 characters")
        self.study_id = manifest["study_id"]
        self.target = 3
        self.secret = secret.encode("utf-8")
        self.store = DynamoStore(table_name, self.study_id, client)
        self.clock = clock

    def _sign(self, payload):
        encoded = _b64(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        signature = _b64(hmac.new(self.secret, encoded.encode("ascii"), hashlib.sha256).digest())
        return encoded + "." + signature

    def _verify(self, token, kind):
        try:
            if not isinstance(token, str) or len(token) > 4096:
                raise ValueError("Invalid token")
            encoded, signature = token.split(".")
            expected = hmac.new(self.secret, encoded.encode("ascii"), hashlib.sha256).digest()
            if not hmac.compare_digest(expected, _unb64(signature)):
                raise ValueError("Invalid signature")
            payload = json.loads(_unb64(encoded))
            if (payload.get("kind") != kind or payload.get("study_id") != self.study_id
                    or not isinstance(payload.get("exp"), int) or payload["exp"] <= self.clock()):
                raise ValueError("Expired or mismatched token")
            return payload
        except (ValueError, TypeError, UnicodeError, AttributeError) as error:
            message = "Your session has expired. Please enter your name again." if kind == "session" else "This assignment has expired. Load another question and try again."
            raise ApiError(401, message) from error

    def session(self, body):
        name, reviewer_id = normalize_name(body.get("name"))
        token = self._sign({
            "kind": "session", "study_id": self.study_id, "name": name,
            "reviewer_id": reviewer_id, "exp": int(self.clock()) + SESSION_SECONDS,
        })
        return {"token": token, "name": name, "reviewer_id": reviewer_id, "study_id": self.study_id}

    def authenticate(self, headers):
        authorization = headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise ApiError(401, "Enter your name to start or resume reviewing.")
        return self._verify(authorization[7:], "session")

    def _order(self, reviewer_id, question_id):
        payload = json.dumps([self.study_id, reviewer_id, question_id], separators=(",", ":")).encode("utf-8")
        flip = hmac.new(self.secret, b"document-order:" + payload, hashlib.sha256).digest()[0] % 2
        return {"A": "negative", "B": "positive"} if flip else {"A": "positive", "B": "negative"}

    def stats(self, counts=None):
        counts = self.store.counts() if counts is None else counts
        values = [counts.get(question_id, 0) for question_id in self.questions]
        by_dataset = {}
        for dataset in DATASETS:
            dataset_counts = [counts.get(question_id, 0) for question_id, question in self.questions.items() if question["dataset"] == dataset]
            by_dataset[dataset] = {
                "total_questions": len(dataset_counts), "min_reviews": min(dataset_counts, default=0),
                "total_reviews": sum(dataset_counts),
                "questions_at_target": sum(count >= self.target for count in dataset_counts),
            }
        return {
            "study_id": self.study_id, "total_questions": len(values), "target_reviews": self.target,
            "min_reviews": min(values), "total_reviews": sum(values),
            "questions_at_target": sum(count >= self.target for count in values),
            "remaining_reviews": sum(max(0, self.target - count) for count in values),
            "by_dataset": by_dataset,
        }

    def next_question(self, session, exclude=None, question_id=None):
        reviewed = self.store.reviewed(session["reviewer_id"])
        counts = self.store.counts()
        choices = [question_id for question_id in self.questions if question_id not in reviewed]
        result = {"reviewer_completed": len(set(self.questions) & reviewed), "stats": self.stats(counts)}
        # Explicit ID reissues an assignment after reconnect, including after a
        # successful save whose HTTP response was lost. It never changes votes.
        if question_id is not None and (not isinstance(question_id, str) or question_id not in self.questions):
            raise ApiError(400, "Unknown question to resume.")
        if question_id is not None:
            choices = [question_id]
        if not choices:
            return {**result, "question": None, "reason": "all_reviewed"}
        # A skip does not count as a judgment. If it is the only eligible item,
        # keep it available rather than claiming that the reviewer is finished.
        if len(choices) > 1 and isinstance(exclude, str) and exclude in choices:
            choices.remove(exclude)
        minimum = min(counts.get(question_id, 0) for question_id in choices)
        question_id = random.SystemRandom().choice([question_id for question_id in choices if counts.get(question_id, 0) == minimum])
        question = self.questions[question_id]
        order = self._order(session["reviewer_id"], question_id)
        assignment_token = self._sign({
            "kind": "assignment", "study_id": self.study_id, "question_id": question_id,
            "reviewer_id": session["reviewer_id"], "exp": int(self.clock()) + ASSIGNMENT_SECONDS,
        })
        return {**result, "question": {
            "id": question_id, "dataset": question["dataset"], "question": question["question"],
            "answer": question["answer"],
            "documents": {position: question[role + "_document"] for position, role in order.items()},
            "assignment_token": assignment_token,
        }}

    def submit(self, session, body):
        question_id = body.get("question_id")
        if not isinstance(question_id, str) or question_id not in self.questions:
            raise ApiError(400, "Unknown question.")
        assignment = self._verify(body.get("assignment_token"), "assignment")
        if assignment.get("question_id") != question_id or assignment.get("reviewer_id") != session["reviewer_id"]:
            raise ApiError(400, "This assignment belongs to another question or reviewer.")
        ratings = body.get("ratings")
        if not isinstance(ratings, dict) or set(ratings) != {"A", "B"} or any(not isinstance(value, str) or value not in RATINGS for value in ratings.values()):
            raise ApiError(400, "Choose one support rating for each document.")
        notes = body.get("notes", "")
        if not isinstance(notes, str) or len(notes) > 2000:
            raise ApiError(400, "Optional notes must be at most 2,000 characters.")
        question = self.questions[question_id]
        order = self._order(session["reviewer_id"], question_id)
        response = {
            "question_id": question_id, "dataset": question["dataset"], "qid": question["qid"],
            "pcid": question["pcid"], "ncid": question["ncid"],
            "reviewer_id": session["reviewer_id"], "name": session["name"],
            "ratings": {order[position]: value for position, value in ratings.items()},
            "displayed_ratings": ratings, "document_order": order, "notes": notes.strip(),
            "created_at": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
        }
        duplicate = self.store.save(response)
        reviewed = self.store.reviewed(session["reviewer_id"])
        return {"saved": True, "duplicate": duplicate, "reviewer_completed": len(set(self.questions) & reviewed), "stats": self.stats()}

    def dispatch(self, method, path, headers, body, query=None):
        if method == "GET" and path == "/api/health":
            return {"ok": True, "study_id": self.study_id, "question_count": len(self.questions)}
        if method == "GET" and path == "/api/stats":
            return self.stats()
        if method == "POST" and path == "/api/session":
            return self.session(body)
        if method == "GET" and path == "/api/next":
            return self.next_question(self.authenticate(headers), (query or {}).get("exclude"), (query or {}).get("question_id"))
        if method == "POST" and path == "/api/responses":
            return self.submit(self.authenticate(headers), body)
        raise ApiError(404, "Unknown API endpoint.")


_app = None


def get_app():
    global _app
    if _app is None:
        path = Path(os.environ.get("QUESTIONS_FILE", Path(__file__).with_name("questions.json")))
        manifest = json.loads(path.read_text(encoding="utf-8"))
        _app = AuditApp(manifest, os.environ["TABLE_NAME"], os.environ["SESSION_SECRET"])
    return _app


def _body(event):
    value = event.get("body") or "{}"
    if not isinstance(value, str) or len(value) > MAX_BODY_BYTES * 2:
        raise ApiError(413, "Request body is too large.")
    try:
        raw = base64.b64decode(value, validate=True) if event.get("isBase64Encoded") else value.encode("utf-8")
        if len(raw) > MAX_BODY_BYTES:
            raise ApiError(413, "Request body is too large.")
        body = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise ApiError(400, "Expected a valid JSON object.") from error
    if not isinstance(body, dict):
        raise ApiError(400, "Expected a JSON object.")
    return body


def handler(event, context):
    """Lambda Function URL payload v2 (and API Gateway v1 for local tests)."""
    headers = {str(key).lower(): str(value) for key, value in (event.get("headers") or {}).items()}
    origin = headers.get("origin")
    origins = {value.strip() for value in os.environ.get("ALLOWED_ORIGINS", "https://n4feng.github.io").split(",") if value.strip()}
    response_headers = {
        "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff", "Vary": "Origin",
    }
    if origin in origins:
        response_headers.update({
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
            "Access-Control-Max-Age": "600",
        })
    try:
        if origin and origin not in origins:
            raise ApiError(403, "This website origin is not allowed.")
        method = event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod", "GET")
        path = (event.get("rawPath") or event.get("path") or "/").rstrip("/") or "/"
        if method == "OPTIONS":
            return {"statusCode": 204, "headers": response_headers, "body": ""}
        if method == "POST" and not headers.get("content-type", "").lower().startswith("application/json"):
            raise ApiError(415, "Use Content-Type: application/json.")
        payload = get_app().dispatch(method, path, headers, _body(event) if method == "POST" else {}, event.get("queryStringParameters"))
        status = 200
    except ApiError as error:
        status, payload = error.status, {"error": error.message}
    except Exception:
        LOG.exception("Audit request failed")
        status, payload = 503, {"error": "The audit service is temporarily unavailable. Please try again shortly."}
    if status == 503:
        response_headers["Retry-After"] = "2"
    return {"statusCode": status, "headers": response_headers, "body": json.dumps(payload, ensure_ascii=False)}
