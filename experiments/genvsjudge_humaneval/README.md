# GenVsJudge human document audit

Public participant page: **https://n4feng.github.io/experiments/genvsjudge_humaneval/**

This standalone page has no entry in the site's navigation and asks search
engines not to index it. Anyone with its URL can open it. Participants enter
their name and the dedicated Lambda API URL supplied by the organizer.
The page itself is ready independently of AWS; collecting reviews requires
sampling the real server records and running the deployment script below.

The audit contains 100 distinct questions from each of NQ, HotpotQA, and
MS MARCO. Each question includes its reference answer and the full original
positive/negative passages, displayed in blinded, participant-specific A/B
order. Original document roles and IDs stay on the backend. No existing votes,
model scores, or positive/negative labels are displayed to reviewers.

## Participant workflow

1. Enter a stable name and the supplied Lambda URL, then connect. No account or
   shared access code is required. The page explains that names and judgments
   are recorded for the research audit.
2. Read the query, reference answer, and both documents. Rate **each** document:
   `supports`, `partial`, `does_not_support`, or `unsure`. Partial support covers
   useful evidence in a multi-hop answer without assuming each individual
   passage must establish the complete answer. A matching answer string alone
   does not establish support. Optional notes can identify supporting evidence.
3. Submit and continue for as many questions as desired. Skip does not record a
   judgment. Saved responses cannot be overwritten from the public page.
4. Return later using the same name to continue. A normalized name contributes
   at most one judgment per question, including across browser sessions.

The dashboard explicitly shows **the minimum reviews across all 300 questions**,
including untouched questions with zero reviews, plus coverage at the target of
three. Selection prioritizes the least-reviewed questions the participant has
not answered. The study continues beyond three judgments when more people
participate. Completion means all 300 have at least three reviews, not simply
that 900 submissions have been collected. At least three distinct participants
and sufficient participation are needed; software cannot guarantee turnout.

Names are self-reported. Case, Unicode normalization, and whitespace differences
do not create extra reviewers, but a person can use multiple different names,
and two people using the same name share a reviewer identity. Ask participants
to use one stable, distinctive name. This is deduplication, not verified identity.

## 1. Sample records on the research server

Clone/update this website repository on the server that contains `GenVsJudge`.
The sampler needs Python 3.10+ and only the standard library; it never loads a
model, GPU, embeddings, or project Python dependencies.

From the website repository root:

```bash
python experiments/genvsjudge_humaneval/scripts/sample_questions.py \
  --repo-root /home/grad/nfeng/GenVsJudge \
  --output experiments/genvsjudge_humaneval/data/questions.json
```

By default it reads, for each dataset `nq`, `hotpotqa`, `msmarco`:

```text
GenVsJudge/empirical_data/train_models/Qwen_Qwen3-8B/<dataset>/train_pairs.jsonl
GenVsJudge/empirical_data/train_models/Qwen_Qwen3-8B/<dataset>/test_pairs.jsonl
GenVsJudge/data/qa/<dataset>.jsonl
GenVsJudge/data/corpus/<dataset>.jsonl
```

It expects the two pair files together to contain 10,000 pairs per dataset,
matching the requested preparation folder. Pair rows use `qid`, `pcid`, `ncid`;
QA rows use `id`, `question`, `answer`; corpus rows use `id`, `text`.
It samples 100 unique query IDs uniformly, then chooses one saved pair uniformly
for each query. This avoids counting two HotpotQA positive passages as two
separate audited questions. Seed 42 is the default. There is no filtering by
answer-string overlap or model predictions, and document text is not truncated.

**Cohort distinction:** the default pool includes preparation training and test
pairs. It is not necessarily the 10,000-pair evaluation cohort behind the paper's
headline result. To audit a particular persisted evaluation sample directly,
select its directory explicitly:

```bash
python experiments/genvsjudge_humaneval/scripts/sample_questions.py \
  --repo-root /home/grad/nfeng/GenVsJudge \
  --samples-root empirical_data/baseline_comparison/2026-08-31_12-45-28 \
  --study-id genvsjudge-evaluation-v1 \
  --output experiments/genvsjudge_humaneval/data/questions.json
```

That mode reads `<samples-root>/<dataset>/10000_sample_ids.jsonl`, the exact
schema used by `src/token2score/samples.py` on `dev/iclr27_rebuttal`. A saved
sample is not automatically held out: select the actual cohort used for the
result you intend to audit. The manifest records the cohort, seed, source pair
file and index, original query/document IDs, pool sizes, and repeated-query
multiplicities. The sample is question-uniform, not pair-uniform; account for
this distinction if estimating a pair-weighted population metric.

Use `--prepared-root`, `--data-root`, or `--seed` to override explicit inputs.
`--expected-pairs` and `--per-dataset` support smaller local tests, but production
deployment requires exactly 300 questions, 100 per dataset. Existing output is
preserved unless `--overwrite` is supplied. Never resample a running study under
the same study ID. Missing records or wrong source sizes fail visibly.

Generated question manifests and response exports are ignored by Git and
excluded from GitHub Pages. **Do not commit the manifest:** doing so would make
original document roles publicly discoverable and weaken the audit blinding.

## 2. Deploy Lambda and DynamoDB from AWS CloudShell

Clone/update this repository in AWS CloudShell and upload the generated
`questions.json` through the CloudShell upload action. Then, from the website
repository root, run:

```bash
python -m pip install --user -r experiments/genvsjudge_humaneval/requirements.txt

python experiments/genvsjudge_humaneval/scripts/deploy_aws.py \
  --questions "$HOME/questions.json" \
  --region us-east-1
```

The uploader's destination may differ; use its actual path. AWS CloudShell
supplies AWS credentials. A workstation can instead use configured credentials
and `--profile`. The operator needs CloudFormation, Lambda, IAM role creation
and pass-role, DynamoDB, CloudWatch Logs, S3, and STS permissions.

The script creates a dedicated CloudFormation stack (`genvsjudge-humaneval`),
an on-demand DynamoDB table, a Python Lambda function, a public Function URL,
and a private encrypted S3 artifact bucket. It does not reuse or modify the
existing experiment-tracker backend. AWS usage is billable. No SAM CLI, Docker,
or frontend build is required. A random session-signing secret is passed directly
to CloudFormation as a NoEcho parameter and preserved on updates.

Deployment prints `ApiUrl`, `TableName`, and `FunctionName`, and saves non-secret
outputs to the ignored `deployment.json`. Give participants **ApiUrl** together
with the page URL. Participants provide it on the landing form; no frontend
rebuild or commit is needed. Re-running deploy updates the same stack while
retaining votes. Deploy refuses changed question content under an existing
study ID. Each stack is bound to one study: use both a new `--study-id` when
sampling and a new `--stack-name` when deploying a new cohort. This also prevents
switching back to an older study ID and accidentally reusing its stored votes.

Optional checks and settings:

```bash
# Validate a real manifest and build the ZIP locally, without AWS calls.
python experiments/genvsjudge_humaneval/scripts/deploy_aws.py \
  --questions /path/to/questions.json --validate-only

# Add another exact HTTPS frontend origin if needed.
python experiments/genvsjudge_humaneval/scripts/deploy_aws.py \
  --questions /path/to/questions.json \
  --allowed-origins https://n4feng.github.io,https://another.example
```

`--artifact-bucket` selects an owned, dedicated bucket in the same region;
deployment enforces public-access blocking and encryption on it.
`--reserved-concurrency` defaults to `-1` (account default), accepts `0` to pause
the API, or `1..20` to bound concurrent executions. It is not a spending cap.
The DynamoDB table is retained if the stack is deleted; deployment artifacts
also remain until explicitly removed. There is no TTL on research responses.

## 3. Export private review records

Run with AWS credentials; there is intentionally no public endpoint that lists
participant names or votes:

```bash
python experiments/genvsjudge_humaneval/scripts/export_responses.py \
  --questions /path/to/questions.json \
  --stack-name genvsjudge-humaneval \
  --region us-east-1 \
  --output-dir experiments/genvsjudge_humaneval/exports
```

You can use `--table-name` instead of `--stack-name`, and `--profile` for a local
AWS profile. `--overwrite` is required to replace prior exports.

Outputs include raw response JSONL, response CSV with reviewer names and original
roles, a per-question CSV (including zero-review questions), and `summary.json`
with dataset coverage and negative-support rating tallies. Majority labels are
descriptive only and require at least three judgments and strictly more than
half agreeing; a plurality or tie is not silently called a majority. Vote-level
counts and question-level counts are reported separately. These judgments are
evidence for a robustness audit, not guaranteed corrections to gold labels.
Original IDs and pair indexes allow joining reviews to saved PPL scores later.

## Implementation and verification

- `index.html`, `styles.css`, `app.js`: responsive static participant interface.
- `server/lambda_handler.py`: signed sessions/assignments, blinded delivery,
  least-reviewed scheduling, atomic DynamoDB submissions and public aggregate
  statistics. Conditional transactions prevent retries from adding votes twice.
- `scripts/sample_questions.py`: deterministic sampler and text hydration.
- `scripts/deploy_aws.py`, `scripts/template.json`: AWS packaging and deployment.
- `scripts/export_responses.py`: authenticated offline export.
- `tests/`: sampling, API, DynamoDB, deployment and reporting checks.

```bash
python -m pip install -r experiments/genvsjudge_humaneval/requirements-dev.txt
python -m unittest discover -s experiments/genvsjudge_humaneval/tests -p 'test_*.py' -v
node --check experiments/genvsjudge_humaneval/app.js
node --test experiments/genvsjudge_humaneval/tests/frontend.test.mjs
cfn-lint --template experiments/genvsjudge_humaneval/scripts/template.json --regions us-east-1
```

API: `GET /api/health`, `POST /api/session`, `GET /api/stats`, authenticated
`GET /api/next`, and authenticated `POST /api/responses`. Errors are JSON with
an `error` field. CORS is emitted only by the Lambda handler to avoid duplicate
headers. Both Function URL invocation permissions are included for current AWS
requirements. The public backend accepts self-enrolled participants; an unlinked
URL and CORS are not authentication or protection against deliberate abuse.

The main tracker build preserves sibling experiment directories so rebuilding
`/experiments/` cannot erase this module. Source/scripts/data are excluded from
the generated Pages site, and no navigation link is added.
