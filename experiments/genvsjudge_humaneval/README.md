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

Run the commands in this section in **AWS CloudShell**, unless a step explicitly
refers to your research server or your own computer. The deployment uses this
website repository, **`n4feng/n4feng.github.io`**. You do not need to clone the
GenVsJudge research repository into CloudShell.

The script creates a dedicated CloudFormation stack (`genvsjudge-humaneval`),
an on-demand DynamoDB table, a Python Lambda function, a public Function URL,
and a private encrypted S3 artifact bucket, together with an execution role and
logs. AWS usage is billable. No manual Lambda/table creation, SAM CLI, Docker,
or frontend build is required.

**Use the new audit deployment's `ApiUrl`. The existing experiment-tracker
Lambda URL is a different service and will not work with this page.** The script
does not reuse or modify the tracker backend.

### Step 1: Download the generated manifest to your computer

After sampling on the research server, locate the generated file inside the
website checkout:

```text
experiments/genvsjudge_humaneval/data/questions.json
```

Download it to your own computer, for example by right-clicking the file in
VS Code's Remote SSH Explorer and selecting **Download**. CloudShell's browser
upload picker selects files from your computer, not directly from the research
server. Upload this generated manifest, not a raw dataset or pair file.

Keep the manifest out of the public GitHub repository to preserve document-role
blinding. Keep a copy for subsequent deployments and response exports.

### Step 2: Open CloudShell in the intended AWS account and region

1. Sign in to the [AWS Console](https://console.aws.amazon.com/).
2. Select **US East (N. Virginia) — `us-east-1`** in the region menu.
3. Search for **CloudShell**, open it, and wait for the terminal prompt.
4. Use the ordinary CloudShell environment; **Actions → Upload file** is not
   available in a CloudShell VPC environment.

CloudShell supplies the credentials of your signed-in AWS identity. You do not
need to run `aws configure`, create access keys, or provide `--profile` here.
Check the account before creating resources:

```bash
aws sts get-caller-identity
```

Confirm that the returned `Account` is the intended account. The operator needs
CloudFormation, Lambda, IAM role creation and pass-role, DynamoDB, CloudWatch
Logs, S3, and STS permissions. Permission to open CloudShell alone is not enough
to deploy these resources. A workstation can instead use configured AWS
credentials and the deployment script's `--profile` option.

### Step 3: Clone or update the website repository

For a first deployment in this CloudShell environment:

```bash
cd "$HOME"
git clone https://github.com/n4feng/n4feng.github.io.git
cd "$HOME/n4feng.github.io"
```

If that checkout already exists, use these commands instead:

```bash
cd "$HOME/n4feng.github.io"
git switch master
git pull --ff-only
```

Confirm that the deployment script is present:

```bash
ls experiments/genvsjudge_humaneval/scripts/deploy_aws.py
```

### Step 4: Upload `questions.json` through CloudShell

Return to your CloudShell home directory:

```bash
cd "$HOME"
```

In the CloudShell interface, choose **Actions → Upload file → Browse**, select
the `questions.json` downloaded to your computer, and choose **Upload**. Wait for
the success message, then verify the uploaded file:

```bash
ls -lh "$HOME/questions.json"
```

The file should have a nonzero size. CloudShell normally places uploaded files
in your home directory. If the upload reports a different destination or your
file has a different name, use that actual path in every `--questions` argument
below. Return to the repository root:

```bash
cd "$HOME/n4feng.github.io"
```

### Step 5: Install the Python dependencies

Create and activate a Python virtual environment, then install the requirements:

```bash
python3 -m venv experiments/genvsjudge_humaneval/.venv
source experiments/genvsjudge_humaneval/.venv/bin/activate
python -m pip install -r experiments/genvsjudge_humaneval/requirements.txt
```

Keep this environment activated for the remaining Python commands. Install the
requirements before validation: the validator imports the backend, which uses
`boto3` even when no AWS requests are made.

### Step 6: Validate the manifest without creating AWS resources

```bash
python experiments/genvsjudge_humaneval/scripts/deploy_aws.py \
  --questions "$HOME/questions.json" \
  --validate-only
```

Successful output starts with:

```text
Validated 300 questions; study genvsjudge-audit-v1; ZIP ... bytes.
```

The study ID may differ if you selected another ID when sampling. Validation
requires exactly 300 questions, 100 unique query IDs per dataset, the expected
manifest schema, and a target of three reviews. This command validates and
packages the backend locally without making AWS calls.

### Step 7: Deploy the AWS resources

```bash
python experiments/genvsjudge_humaneval/scripts/deploy_aws.py \
  --questions "$HOME/questions.json" \
  --region us-east-1 \
  --stack-name genvsjudge-humaneval
```

Wait for the command to finish. A successful first deployment reports
`CloudFormation: CREATE_COMPLETE`; a successful update reports `UPDATE_COMPLETE`.
The script prints `ApiUrl`, `TableName`, and `FunctionName`, and saves these
non-secret outputs to the ignored file
`experiments/genvsjudge_humaneval/deployment.json`.

Display the outputs again with:

```bash
cat experiments/genvsjudge_humaneval/deployment.json
```

A random session-signing secret is passed directly to CloudFormation as a
NoEcho parameter and preserved on updates; participants do not need this secret.

### Step 8: Retrieve and verify the audit URL

Retrieve the URL directly from this audit's CloudFormation stack. This avoids
accidentally copying the URL of the existing experiment tracker:

```bash
AUDIT_URL=$(aws cloudformation describe-stacks \
  --stack-name genvsjudge-humaneval \
  --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue | [0]" \
  --output text)

printf '%s\n' "$AUDIT_URL"
curl --fail-with-body "${AUDIT_URL%/}/api/health"
curl --fail-with-body "${AUDIT_URL%/}/api/stats"
```

Use your actual stack name if you overrode `--stack-name`. The URL should look
like `https://YOUR_FUNCTION_ID.lambda-url.us-east-1.on.aws/`. If it is empty,
`None`, or the command fails, check the account, region, stack name, and deployment
status before continuing.

The health response must contain:

```json
{
  "ok": true,
  "study_id": "genvsjudge-audit-v1",
  "question_count": 300
}
```

The study ID must match your manifest. For a fresh deployment, `/api/stats`
should report `total_questions: 300`, `target_reviews: 3`, `min_reviews: 0`,
`total_reviews: 0`, and `remaining_reviews: 900`. Existing studies retain their
counts. These requests do not create review records.

**A response containing only `{"status":"ok"}` is not the audit health
response.** If `/api/stats` also returns 404, you may be using the tracker URL.
Retrieve `ApiUrl` from the audit stack above instead of redeploying or replacing
the tracker backend.

### Step 9: Connect the participant page

1. Open [the human-audit page](https://n4feng.github.io/experiments/genvsjudge_humaneval/).
2. Enter your name and paste the exact audit URL printed in Step 8 into the
   Lambda server URL field. Replace any previously entered tracker URL.
3. Use the base URL only; do not append `/api/health`, `/api/stats`, or `/api`.
4. Connect and confirm that a query, reference answer, and Documents A/B appear.
5. Submit only genuine judgments: submissions become part of the research data.

Give participants both the page URL and this audit's `ApiUrl`. No access code,
frontend rebuild, or GitHub commit is needed to connect a deployed backend.
After deployment completes, you can close CloudShell; the hosted API and stored
responses do not depend on keeping the shell session open.

### Step 10: Update an existing deployment

For a later code update, reopen CloudShell in the same account and region, then
run:

```bash
cd "$HOME/n4feng.github.io"
git switch master
git pull --ff-only
source experiments/genvsjudge_humaneval/.venv/bin/activate
python -m pip install -r experiments/genvsjudge_humaneval/requirements.txt

python experiments/genvsjudge_humaneval/scripts/deploy_aws.py \
  --questions "$HOME/questions.json" \
  --region us-east-1 \
  --stack-name genvsjudge-humaneval
```

If the checkout, virtual environment, or manifest is no longer available, repeat
the corresponding setup steps using your saved, unchanged manifest. Re-running
deploy against the same stack updates the backend while retaining votes and the
session-signing secret. Repeat the health and statistics checks after updating.

Each stack is bound to one study. Deployment refuses changed question content
under an existing study ID. For a new cohort, use both a new `--study-id` when
sampling and a new `--stack-name` when deploying. This also prevents switching
back to an older study ID and accidentally reusing its stored votes.

### Troubleshooting

| Symptom | What to check |
| --- | --- |
| `FileNotFoundError` for the manifest | Run `ls -lh "$HOME/questions.json"` and use the actual uploaded filename/path. |
| `ModuleNotFoundError: No module named 'boto3'` | Activate the virtual environment and install `requirements.txt` as in Step 5. |
| `AccessDenied` | Read the denied action in the error. The signed-in AWS identity needs that permission; CloudShell access alone does not grant deployment permissions. |
| Stack not found or URL is `None` | Confirm the AWS account, `us-east-1` region, stack name, and that deployment completed. |
| CloudFormation failure or rollback | Open **CloudFormation → genvsjudge-humaneval → Events** in `us-east-1` and inspect the failed resource's reason. Resolve the stack state before rerunning deployment. |
| `This endpoint is not a ready 300-question audit` | Inspect `/api/health` for the exact URL entered on the page. It must return boolean `ok: true` and numeric `question_count: 300`. Retrieve the audit `ApiUrl` from Step 8; the tracker URL is not interchangeable. |
| Health works in CloudShell but the page says it cannot reach the server | Verify the URL entered on the page and inspect the browser's Network/Console panels for a CORS error. The default allowed origin is `https://n4feng.github.io`; another frontend origin must be configured explicitly. |
| Deployment refuses a changed study or manifest | Use the original manifest for a code update, or a new study ID and stack name for a new cohort. |

For browser debugging, open Developer Tools (**F12** in Chrome), select
**Network**, connect again, and inspect the `/api/health` request's **Request URL**
and **Response**. They should match the endpoint and JSON verified in Step 8.
The readiness message means the page received JSON that failed its audit health
check; a blocked network/CORS request produces a different connection error.

### Optional deployment settings

To allow another frontend origin, update the same stack with the unchanged
manifest and an explicit list of exact HTTPS origins:

```bash
python experiments/genvsjudge_humaneval/scripts/deploy_aws.py \
  --questions "$HOME/questions.json" \
  --region us-east-1 \
  --stack-name genvsjudge-humaneval \
  --allowed-origins https://n4feng.github.io,https://another.example
```

Supply any non-default origin, artifact-bucket, or concurrency settings again on
subsequent updates; omitted options use the script's defaults.

`--artifact-bucket` selects an owned, dedicated bucket in the same region;
deployment enforces public-access blocking and encryption on it.
`--reserved-concurrency` defaults to `-1` (account default), accepts `0` to pause
the API, or `1..20` to bound concurrent executions. It is not a spending cap.
The DynamoDB table is retained if the stack is deleted; deployment artifacts
also remain until explicitly removed. There is no TTL on research responses.

AWS references: [CloudShell launch and file upload](https://docs.aws.amazon.com/cloudshell/latest/userguide/getting-started.html),
[CloudShell credentials and tools](https://docs.aws.amazon.com/cloudshell/latest/userguide/welcome.html).

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
