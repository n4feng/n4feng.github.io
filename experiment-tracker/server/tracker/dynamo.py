"""DynamoDB storage with immutable chunked batches and atomic publication.

The table has string PK/SK keys and optional TTL on expires_at. No scans, GSIs,
table creation, or startup writes are used. Unpublished chunks are invisible to
the API and expire; committed chunks have their TTL removed in the same
transaction that publishes the hierarchy and batch manifest.
"""

import gzip
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from uuid import uuid4

from werkzeug.exceptions import Conflict, NotFound, RequestEntityTooLarge, ServiceUnavailable

from .storage import builtin_metrics

CHUNK_BYTES = 250 * 1024
# Leave ample space for metadata within DynamoDB's 4 MiB transaction ceiling.
MAX_COMPRESSED_BATCH_BYTES = 3500 * 1024
PLOT_ID = re.compile(r"^([a-f0-9]{32})-(\d{3})$")


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def name_id(name, parent=""):
    return hashlib.sha256((parent + "\0" + name.casefold()).encode()).hexdigest()[:32]


def encode(item):
    return {key: {"B": value} if isinstance(value, bytes) else {"N": str(value)} if isinstance(value, int) else {"S": value} for key, value in item.items()}


def decode(item):
    return {key: bytes(value["B"]) if "B" in value else int(value["N"]) if "N" in value else value["S"] for key, value in item.items()}


class DynamoStore:
    def __init__(self, config):
        self.table = config["DYNAMODB_TABLE"]
        self._client = config.get("DYNAMODB_CLIENT")
        self.region = config.get("AWS_REGION")
        self.endpoint = config.get("DYNAMODB_ENDPOINT_URL")

    @property
    def client(self):
        if self._client is None:
            import boto3
            from botocore.config import Config
            self._client = boto3.client(
                "dynamodb", region_name=self.region, endpoint_url=self.endpoint,
                config=Config(retries={"mode": "standard", "max_attempts": 3}, connect_timeout=3, read_timeout=10),
            )
        return self._client

    def _key(self, pk, sk):
        return encode({"PK": pk, "SK": sk})

    def _get(self, pk, sk):
        result = self.client.get_item(TableName=self.table, Key=self._key(pk, sk), ConsistentRead=True)
        return decode(result["Item"]) if "Item" in result else None

    def _query(self, pk, prefix=""):
        arguments = {
            "TableName": self.table, "ConsistentRead": True,
            "KeyConditionExpression": "PK = :pk",
            "ExpressionAttributeValues": {":pk": {"S": pk}},
        }
        if prefix:
            arguments["KeyConditionExpression"] += " AND begins_with(SK, :prefix)"
            arguments["ExpressionAttributeValues"][":prefix"] = {"S": prefix}
        items = []
        while True:
            response = self.client.query(**arguments)
            items.extend(decode(item) for item in response.get("Items", []))
            if not response.get("LastEvaluatedKey"):
                return items
            arguments["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def _record(self, pk, sk, label):
        row = self._get(pk, sk)
        if row is None:
            raise NotFound(f"{label} not found.")
        return row, json.loads(row["payload"])

    def _item(self, pk, sk, payload, revision=1):
        return {"PK": pk, "SK": sk, "payload": compact_json(payload), "revision": revision}

    def _put_action(self, item, previous=None):
        action = {"TableName": self.table, "Item": encode(item), "ConditionExpression": "attribute_not_exists(PK)"}
        if previous:
            action.update(ConditionExpression="revision = :version", ExpressionAttributeValues={":version": {"N": str(previous["revision"])}})
        return {"Put": action}

    def _check(self, pk, sk):
        return {"ConditionCheck": {"TableName": self.table, "Key": self._key(pk, sk), "ConditionExpression": "attribute_exists(PK)"}}

    def _transact(self, actions):
        from botocore.exceptions import ClientError
        try:
            self.client.transact_write_items(TransactItems=actions, ClientRequestToken=uuid4().hex)
        except ClientError as error:
            code = error.response["Error"]["Code"]
            if code == "TransactionConflictException":
                return False
            if code == "TransactionCanceledException":
                reasons = error.response.get("CancellationReasons", [])
                if not reasons or all(reason.get("Code", "None") in {"None", "ConditionalCheckFailed", "TransactionConflict"} for reason in reasons):
                    return False
            raise
        return True

    def _project_count(self, project_id):
        return len(self._query(f"PROJECT#{project_id}", "EXPERIMENT#"))

    def _plot_count(self, experiment_id):
        return sum(json.loads(row["payload"])["count"] for row in self._query(f"EXPERIMENT#{experiment_id}", "BATCH#"))

    def list_projects(self):
        results = []
        for item in self._query("ROOT", "PROJECT#"):
            record = json.loads(item["payload"])
            record["experiment_count"] = self._project_count(record["id"])
            results.append(record)
        return sorted(results, key=lambda item: (item["name"].casefold(), item["id"]))

    def get_project(self, project_id):
        _, result = self._record("ROOT", f"PROJECT#{project_id}", "Project")
        experiments = []
        for row in self._query(f"PROJECT#{project_id}", "EXPERIMENT#"):
            experiment_id = json.loads(row["payload"])["id"]
            _, record = self._record(f"EXPERIMENT#{experiment_id}", "META", "Experiment")
            record["plot_count"] = self._plot_count(experiment_id)
            experiments.append(record)
        result["experiments"] = sorted(experiments, key=lambda item: (item["name"].casefold(), item["id"]))
        result["experiment_count"] = len(experiments)
        return result

    def create_project(self, values):
        record = {"id": name_id(values["name"]), **values}
        if not self._transact([self._put_action(self._item("ROOT", f"PROJECT#{record['id']}", record))]):
            raise Conflict("A project with this name already exists.")
        return {**record, "experiment_count": 0}

    def create_experiment(self, project_id, values):
        self._record("ROOT", f"PROJECT#{project_id}", "Project")
        record = {"id": name_id(values["name"], project_id), "project_id": project_id, **values}
        actions = [
            self._check("ROOT", f"PROJECT#{project_id}"),
            self._put_action(self._item(f"EXPERIMENT#{record['id']}", "META", record)),
            self._put_action(self._item(f"PROJECT#{project_id}", f"EXPERIMENT#{record['id']}", {"id": record["id"]})),
        ]
        if not self._transact(actions):
            raise Conflict("An experiment with this name already exists in this project.")
        return {**record, "plot_count": 0}

    def get_experiment(self, experiment_id, limit=None, offset=0):
        _, result = self._record(f"EXPERIMENT#{experiment_id}", "META", "Experiment")
        result["plots"] = []
        position = 0
        for row in self._query(f"EXPERIMENT#{experiment_id}", "BATCH#"):
            manifest = json.loads(row["payload"])
            end = position + manifest["count"]
            if limit is None:
                result["plots"].extend(self._read_batch(manifest["batch_id"]))
            elif end > offset and position < offset + limit:
                batch = self._read_batch(manifest["batch_id"])
                result["plots"].extend(batch[max(0, offset - position):max(0, offset + limit - position)])
            position = end
        result["plot_count"] = position
        if limit is not None:
            following = offset + len(result["plots"])
            result["next_cursor"] = str(following) if following < position else None
        return result

    def list_metrics(self):
        results = list(builtin_metrics().values()) + [json.loads(row["payload"]) for row in self._query("METRICS")]
        return sorted(results, key=lambda metric: (not metric["builtin"], metric["name"].casefold()))

    def get_metric(self, metric_id):
        if metric_id in builtin_metrics():
            return builtin_metrics()[metric_id]
        row = self._get("METRICS", metric_id)
        return json.loads(row["payload"]) if row else None

    def create_metric(self, values):
        if values["id"] in builtin_metrics():
            raise Conflict("A metric with this identifier already exists.")
        record = {**values, "builtin": False}
        if not self._transact([self._put_action(self._item("METRICS", record["id"], record))]):
            raise Conflict("A metric with this identifier already exists.")
        return record

    def find_experiment(self, project_name, experiment_name):
        experiment_id = name_id(experiment_name, name_id(project_name))
        row = self._get(f"EXPERIMENT#{experiment_id}", "META")
        return self.get_experiment(experiment_id) if row else None

    def _stage(self, experiment_id, entries):
        batch_id = uuid4().hex
        created = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        plots = [{"id": f"{batch_id}-{index:03}", "experiment_id": experiment_id, "created_at": created, **entry} for index, entry in enumerate(entries)]
        compressed = gzip.compress(compact_json(plots).encode(), mtime=0)
        if len(compressed) > MAX_COMPRESSED_BATCH_BYTES:
            raise RequestEntityTooLarge("Compressed plot batch exceeds 3500 KiB. Split the upload into smaller imports.")
        chunks = [compressed[index:index + CHUNK_BYTES] for index in range(0, len(compressed), CHUNK_BYTES)]
        expires = int(time.time()) + 86400
        requests = [{"PutRequest": {"Item": encode({"PK": f"BATCH#{batch_id}", "SK": f"CHUNK#{index:04}", "blob": chunk, "expires_at": expires})}} for index, chunk in enumerate(chunks)]
        for offset in range(0, len(requests), 25):
            pending = {self.table: requests[offset:offset + 25]}
            for attempt in range(6):
                response = self.client.batch_write_item(RequestItems=pending)
                pending = response.get("UnprocessedItems", {})
                if not any(pending.values()):
                    break
                time.sleep(min(0.05 * 2 ** attempt, 0.5))
            else:
                raise ServiceUnavailable("Plot storage is temporarily busy. Retry the upload.")
        manifest = {"batch_id": batch_id, "experiment_id": experiment_id, "count": len(plots), "chunks": len(chunks), "created_at": created}
        return manifest, plots

    def _publish_actions(self, manifest):
        batch_id = manifest["batch_id"]
        actions = [self._put_action(self._item(f"BATCH#{batch_id}", "META", manifest))]
        actions.append(self._put_action(self._item(f"EXPERIMENT#{manifest['experiment_id']}", f"BATCH#{manifest['created_at']}#{batch_id}", manifest)))
        for index in range(manifest["chunks"]):
            actions.append({"Update": {
                "TableName": self.table, "Key": self._key(f"BATCH#{batch_id}", f"CHUNK#{index:04}"),
                "UpdateExpression": "REMOVE expires_at", "ConditionExpression": "attribute_exists(PK)",
            }})
        return actions

    def import_records(self, project, experiment, entries, update_project=False, update_experiment=False):
        project_id = name_id(project["name"])
        experiment_id = name_id(experiment["name"], project_id)
        manifest, plots = self._stage(experiment_id, entries)
        for attempt in range(6):
            old_project = self._get("ROOT", f"PROJECT#{project_id}")
            old_experiment = self._get(f"EXPERIMENT#{experiment_id}", "META")
            project_record = json.loads(old_project["payload"]) if old_project else {"id": project_id, **project}
            experiment_record = json.loads(old_experiment["payload"]) if old_experiment else {"id": experiment_id, "project_id": project_id, **experiment}
            if update_project:
                project_record["description"] = project["description"]
            if update_experiment:
                experiment_record["description"] = experiment["description"]
            actions = [
                self._put_action(self._item("ROOT", f"PROJECT#{project_id}", project_record, old_project["revision"] + 1 if old_project else 1), old_project),
                self._put_action(self._item(f"EXPERIMENT#{experiment_id}", "META", experiment_record, old_experiment["revision"] + 1 if old_experiment else 1), old_experiment),
                *self._publish_actions(manifest),
            ]
            if old_experiment is None:
                actions.append(self._put_action(self._item(f"PROJECT#{project_id}", f"EXPERIMENT#{experiment_id}", {"id": experiment_id})))
            if self._transact(actions):
                return {"project_id": project_id, "experiment_id": experiment_id, "plot_ids": [plot["id"] for plot in plots]}
            time.sleep(min(0.02 * 2 ** attempt, 0.3))
        raise Conflict("Concurrent imports changed this project. Retry the upload.")

    def create_plot(self, experiment_id, value):
        self._record(f"EXPERIMENT#{experiment_id}", "META", "Experiment")
        manifest, plots = self._stage(experiment_id, [value])
        if not self._transact([self._check(f"EXPERIMENT#{experiment_id}", "META"), *self._publish_actions(manifest)]):
            raise Conflict("The experiment changed while uploading. Retry the upload.")
        return plots[0]

    def _read_batch(self, batch_id):
        rows = self._query(f"BATCH#{batch_id}")
        by_key = {row["SK"]: row for row in rows}
        if "META" not in by_key:
            raise NotFound("Plot not found.")
        manifest = json.loads(by_key["META"]["payload"])
        try:
            blob = b"".join(by_key[f"CHUNK#{index:04}"]["blob"] for index in range(manifest["chunks"]))
            plots = json.loads(gzip.decompress(blob))
        except (KeyError, OSError, ValueError):
            raise ServiceUnavailable("Stored plot data could not be read. Retry or restore from a backup.") from None
        for index, plot in enumerate(plots):
            override = by_key.get(f"PATCH#{index:03}")
            if override:
                plot["plot"] = json.loads(override["payload"])
        return plots

    def get_plot(self, plot_id):
        match = PLOT_ID.fullmatch(plot_id)
        if not match:
            raise NotFound("Plot not found.")
        plots = self._read_batch(match[1])
        index = int(match[2])
        if index >= len(plots):
            raise NotFound("Plot not found.")
        return plots[index]

    def patch_plot(self, plot_id, options, previous=None):
        match = PLOT_ID.fullmatch(plot_id)
        if not match:
            raise NotFound("Plot not found.")
        pk, sk = f"BATCH#{match[1]}", f"PATCH#{int(match[2]):03}"
        old = self._get(pk, sk)
        current = self.get_plot(plot_id)
        if previous is not None and current["plot"] != previous:
            raise Conflict("Plot options changed. Reload the plot before saving.")
        item = self._item(pk, sk, options, old["revision"] + 1 if old else 1)
        if not self._transact([self._check(pk, "META"), self._put_action(item, old)]):
            raise Conflict("Plot options changed. Reload the plot before saving.")
        return {**current, "plot": options}
