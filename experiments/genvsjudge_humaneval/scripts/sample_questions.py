#!/usr/bin/env python3
"""Sample a reproducible, private human-audit manifest from GenVsJudge JSONL.

Only Python's standard library is needed. No models, GPU, embeddings, or
GenVsJudge imports are involved. Sampling never filters on answer overlap or
model scores. The default preparation pool includes training and test pairs;
pass --samples-root to select a particular persisted evaluation sample instead.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import random
import re
import sys
from typing import Iterator


DATASETS = ("nq", "hotpotqa", "msmarco")
PAIR_FIELDS = ("qid", "pcid", "ncid")
DEFAULT_PREPARED_ROOT = Path("empirical_data/train_models/Qwen_Qwen3-8B")
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data" / "questions.json"


def read_jsonl(path: Path) -> Iterator[tuple[int, dict]]:
    """Yield physical line numbers for useful error messages and provenance."""
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{line_number}: invalid JSON") from error
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            yield line_number, row


def identifier(value: object, source: str) -> str:
    """Match the project's stringified IDs without numeric coercion."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (str, int, float))
        or isinstance(value, float) and not math.isfinite(value)
        or not str(value).strip()
    ):
        raise ValueError(f"{source}: expected a nonempty string or numeric ID")
    return str(value)


def resolve_path(path: Path, repo_root: Path) -> Path:
    return (path if path.is_absolute() else repo_root / path).resolve()


def source_path(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return str(path)


def load_pairs(
    files: list[tuple[str, Path]], repo_root: Path, expected_pairs: int
) -> tuple[dict[str, list[dict]], dict]:
    """Group all source pairs by question, keeping exact original positions."""
    groups: dict[str, list[dict]] = {}
    seen = set()
    sources = []
    total = 0
    for split, path in files:
        file_count = 0
        for pair_index, (line_number, row) in enumerate(read_jsonl(path)):
            pair = {
                field: identifier(row.get(field), f"{path}:{line_number} {field}")
                for field in PAIR_FIELDS
            }
            key = tuple(pair[field] for field in PAIR_FIELDS)
            if pair["pcid"] == pair["ncid"]:
                raise ValueError(f"{path}:{line_number}: positive and negative IDs match")
            if key in seen:
                raise ValueError(f"{path}:{line_number}: duplicate document pair {key}")
            seen.add(key)
            pair["source"] = {
                "pair_file": source_path(path, repo_root),
                "pair_index": pair_index,
                "pair_line_number": line_number,
                "split": split,
            }
            groups.setdefault(pair["qid"], []).append(pair)
            file_count += 1
        if file_count == 0:
            raise ValueError(f"Empty pair file: {path}")
        sources.append({"path": source_path(path, repo_root), "split": split, "pairs": file_count})
        total += file_count
    if total != expected_pairs:
        raise ValueError(
            f"Expected {expected_pairs:,} source pairs, found {total:,} in "
            f"{', '.join(str(path) for _, path in files)}. "
            "Select the intended cohort or explicitly set --expected-pairs."
        )
    return groups, {"pair_files": sources, "pool_pairs": total, "pool_unique_questions": len(groups)}


def selected_records(path: Path, wanted: set[str], fields: tuple[str, ...]) -> dict[str, dict]:
    """Stream large corpus files; retain only the sampled questions/documents."""
    result = {}
    for line_number, row in read_jsonl(path):
        key = identifier(row.get("id"), f"{path}:{line_number} id")
        if key not in wanted:
            continue
        if key in result:
            raise ValueError(f"{path}:{line_number}: duplicate requested ID {key!r}")
        for field in fields:
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError(f"{path}:{line_number}: ID {key!r} needs nonempty {field}")
        result[key] = row
    missing = wanted.difference(result)
    if missing:
        raise ValueError(f"{path}: missing {len(missing)} requested IDs, including {sorted(missing)[:3]}")
    return result


def build_manifest(
    repo_root: Path,
    *,
    prepared_root: Path | None = None,
    samples_root: Path | None = None,
    data_root: Path = Path("data"),
    study_id: str = "genvsjudge-audit-v1",
    seed: int = 42,
    per_dataset: int = 100,
    expected_pairs: int = 10000,
) -> dict:
    """Uniformly sample unique questions, then one saved pair per question."""
    if prepared_root is not None and samples_root is not None:
        raise ValueError("--prepared-root and --samples-root are mutually exclusive")
    if not isinstance(study_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", study_id):
        raise ValueError("study_id must be 1–80 letters, digits, dots, underscores or hyphens")
    for label, value in (("per_dataset", per_dataset), ("expected_pairs", expected_pairs)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{label} must be a positive integer")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    root = repo_root.resolve()
    raw_root = resolve_path(data_root, root)
    saved_sample = samples_root is not None
    pool_root = resolve_path(
        samples_root if saved_sample else prepared_root or DEFAULT_PREPARED_ROOT,
        root,
    )
    cohort = "saved_sample" if saved_sample else "prepared_train_test"
    questions = []
    dataset_sources = {}
    for dataset in DATASETS:
        files = (
            [("saved_sample", pool_root / dataset / "10000_sample_ids.jsonl")]
            if saved_sample else [
                ("prepared_train", pool_root / dataset / "train_pairs.jsonl"),
                ("prepared_test", pool_root / dataset / "test_pairs.jsonl"),
            ]
        )
        groups, provenance = load_pairs(files, root, expected_pairs)
        if len(groups) < per_dataset:
            raise ValueError(
                f"{dataset}: need {per_dataset} distinct questions, found only {len(groups)}. "
                "Repeated pairs do not count as additional questions."
            )
        rng = random.Random(f"{seed}:{dataset}")
        selected_qids = rng.sample(sorted(groups), per_dataset)
        selected_pairs = [rng.choice(groups[qid]) for qid in selected_qids]
        qa_path = raw_root / "qa" / f"{dataset}.jsonl"
        corpus_path = raw_root / "corpus" / f"{dataset}.jsonl"
        qa = selected_records(qa_path, set(selected_qids), ("question", "answer"))
        documents = selected_records(
            corpus_path,
            {pair[field] for pair in selected_pairs for field in ("pcid", "ncid")},
            ("text",),
        )
        dataset_sources[dataset] = {
            **provenance,
            "qa_file": source_path(qa_path, root),
            "corpus_file": source_path(corpus_path, root),
            "sampled_questions": per_dataset,
        }
        for index, pair in enumerate(selected_pairs, 1):
            question = qa[pair["qid"]]
            questions.append({
                "id": f"{dataset}-{index:04d}",
                "dataset": dataset,
                **{field: pair[field] for field in PAIR_FIELDS},
                "question": question["question"],
                "answer": question["answer"],
                "positive_document": documents[pair["pcid"]]["text"],
                "negative_document": documents[pair["ncid"]]["text"],
                "source": {
                    **pair["source"],
                    "pairs_for_question_in_pool": len(groups[pair["qid"]]),
                },
            })
    return {
        "schema_version": 1,
        "study_id": study_id,
        "seed": seed,
        "target_reviews": 3,
        "source": {
            "cohort": cohort,
            "pool_root": source_path(pool_root, root),
            "data_root": source_path(raw_root, root),
            "sampling": "Uniform unique question IDs without replacement; one uniformly selected saved pair per question",
            "filters": "None: no answer-string, score, or semantic filtering",
            "documents": "Full source text, without truncation",
            "cohort_note": (
                "Explicit saved sample; no guarantee of train/test separation is inferred."
                if saved_sample else
                "Preparation training and test pairs combined. This is not a held-out evaluation cohort."
            ),
            "datasets": dataset_sources,
        },
        "questions": questions,
    }


def positive_integer(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--repo-root", type=Path, required=True, help="Path to the GenVsJudge checkout on your data server")
    pools = result.add_mutually_exclusive_group()
    pools.add_argument("--prepared-root", type=Path, help=f"Preparation pool relative to repo-root (default: {DEFAULT_PREPARED_ROOT})")
    pools.add_argument("--samples-root", type=Path, help="Explicit pool with DATASET/10000_sample_ids.jsonl; relative to repo-root or absolute")
    result.add_argument("--data-root", type=Path, default=Path("data"), help="Raw qa/ and corpus/ directories, relative to repo-root or absolute")
    result.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Private manifest destination, relative to your working directory or absolute")
    result.add_argument("--study-id", default="genvsjudge-audit-v1", help="Use a new ID when starting a different set of questions")
    result.add_argument("--seed", type=int, default=42)
    result.add_argument("--per-dataset", type=positive_integer, default=100, help="Distinct questions per dataset; production Lambda requires 100")
    result.add_argument("--expected-pairs", type=positive_integer, default=10000, help="Required source pair count per dataset, before grouping questions")
    result.add_argument("--overwrite", action="store_true", help="Explicitly replace an existing manifest; do not change an active study's questions")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.output.exists() and not args.overwrite:
            raise FileExistsError(f"Output already exists: {args.output}. Use --overwrite only when intentionally replacing it.")
        manifest = build_manifest(
            args.repo_root,
            prepared_root=args.prepared_root,
            samples_root=args.samples_root,
            data_root=args.data_root,
            study_id=args.study_id,
            seed=args.seed,
            per_dataset=args.per_dataset,
            expected_pairs=args.expected_pairs,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w" if args.overwrite else "x", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        print(f"Saved {len(manifest['questions'])} questions to {args.output.resolve()}")
        print(f"Study: {args.study_id}; source: {manifest['source']['cohort']}; seed: {args.seed}")
        for dataset, source in manifest["source"]["datasets"].items():
            print(f"  {dataset}: {source['sampled_questions']} / {source['pool_unique_questions']} unique questions from {source['pool_pairs']} pairs")
        print(manifest["source"]["cohort_note"])
        return 0
    except (OSError, ValueError) as error:
        print(f"Sampling failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
