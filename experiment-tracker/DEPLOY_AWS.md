# Deploy the experiment tracker on AWS

This guide deploys **Flask on AWS Lambda with DynamoDB storage**. React remains at **https://n4feng.github.io/experiments/**. It is designed for light personal research use with a target below **US$2/month**, subject to actual traffic, region, storage, and AWS allowances. Nothing is deployed until you run the commands in your AWS account.

## What runs where

| Component | Purpose |
| --- | --- |
| GitHub Pages + React | Website files, navigation UI, login form, chart rendering, chart controls, tables, SVG export |
| Lambda Function URL + Flask | HTTPS API; root-code verification and signed-token checks; reading/writing experiment records |
| DynamoDB Standard on-demand table | Projects, experiments, custom metric definitions, and plot records/chunks |
| CloudWatch Logs | Short-lived server diagnostics |
| Private deployment S3 bucket | Small packaged application artifacts used by deployment; not experiment data |

Login and token validation read environment configuration and make **no DynamoDB requests**. Sessions are signed tokens, not database rows. Failed-code counters use bounded process memory, so their limit is per Lambda environment rather than global. The React interface can render and switch chart types without database requests. Opening a project/experiment or saving a change calls the data API.

The Function URL's AWS authorization setting is deliberately **`NONE`**: a browser without AWS credentials can reach the application. **Flask authentication remains required on every data route.** Only health, login, and CORS preflight are publicly reachable without an existing token. Do not change application routes to bypass code/token checks.

The supplied template creates the table, Lambda, execution role, log group, and HTTPS Function URL. Its runtime role is scoped to this table and its log group; it does not administer your AWS account. You do not need to create EC2, a domain, DNS records, Caddy, an API Gateway, a load balancer, or a VPC/NAT gateway.

## 1. Prepare your AWS account and budget

1. Sign in to your AWS account with a role permitted to deploy this application. Deployment needs CloudFormation, Lambda, DynamoDB, CloudWatch Logs, deployment-bucket S3 access, and permission to create/pass the function's IAM role. These deployment permissions are separate from the narrower function execution role.
2. Choose **US East (N. Virginia), `us-east-1`** for the commands below. Keep the stack, function, table, and deployment bucket in the same region. You can choose another supported region, but check its pricing first.
3. In **Billing and Cost Management → Budgets**, create a **monthly cost budget of US$2** with email alerts at thresholds such as 50%, 80%, and 100%. Account-level alerts include other AWS resources too. Billing alerts can be delayed and do **not** automatically stop spending.
4. Open the AWS Console's **CloudShell** terminal in the selected region. Its credentials come from your signed-in AWS identity; do not paste access keys into this repository.

Check the account and tools:

```bash
export AWS_REGION=us-east-1
aws sts get-caller-identity
aws --version
sam --version
python3 --version
python3 -m pip --version
make --version
git --version
```

CloudShell includes AWS CLI, SAM CLI, Git, Python, and build tools. The helper needs **Python 3.9+**, pip, and make. Lambda runs Python 3.12; the included Makefile downloads matching Linux binary dependencies, so **local Python 3.12 and Docker are not required**. If pip or make is missing in CloudShell's Amazon Linux environment:

```bash
sudo dnf install -y python3-pip make
```

If `sam` is missing, install the official Linux release, selecting the matching architecture:

```bash
SAM_ARCH=$(uname -m)
case "$SAM_ARCH" in
  x86_64) SAM_PACKAGE=aws-sam-cli-linux-x86_64.zip ;;
  aarch64|arm64) SAM_PACKAGE=aws-sam-cli-linux-arm64.zip ;;
  *) printf 'Unsupported architecture: %s\n' "$SAM_ARCH"; exit 1 ;;
esac
curl --fail --location "https://github.com/aws/aws-sam-cli/releases/latest/download/$SAM_PACKAGE" --output /tmp/aws-sam-cli.zip
unzip -q /tmp/aws-sam-cli.zip -d /tmp/sam-installation
sudo /tmp/sam-installation/install
sam --version
```

For other operating systems or an existing SAM installation, use the [official SAM installation/update instructions](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html).

## 2. Get the code and deploy

In CloudShell:

```bash
git clone https://github.com/n4feng/n4feng.github.io.git
cd n4feng.github.io/experiment-tracker
python3 scripts/deploy_aws.py --region "$AWS_REGION" --stack-name experiment-tracker
```

If the checkout already exists, update it with `git pull --ff-only` instead of cloning it again. Run the helper from `experiment-tracker/`.

On first deployment, the helper:

1. Asks for a root code using a hidden prompt. **Enter keeps the requested default `666666`**; enter a private code for data that should not be accessible with the public default.
2. Generates a random `SECRET_KEY` for session signatures. It passes secrets as `NoEcho` CloudFormation parameters using a protected temporary file, not a command-line value or committed configuration.
3. Builds the Lambda code, asks before creating its private S3 artifact bucket if needed, packages the code, and prepares a CloudFormation change set.
4. Displays the proposed resource changes and asks you to confirm deployment.
5. Creates the infrastructure and prints outputs including **`ApiUrl`**, **`TableName`**, and **`FunctionName`**.

Normal updates reuse both the access code and signing key. Do not generate a new signing key on every deployment: that would invalidate every open session. Authorized AWS administrators can still inspect function configuration; `NoEcho` keeps parameter values out of routine CloudFormation output, not out of administrative access.

Optional helper settings include `--artifact-bucket` for an existing same-region artifact bucket, `--cors-origins` for exact comma-separated browser origins, and `--reserved-concurrency` for an account-supported concurrency ceiling (`-1` leaves concurrency unreserved, `0` pauses the API, and `1`–`20` caps parallel executions). The default browser origin is `https://n4feng.github.io` **without** `/experiments`.

The default creates no provisioned concurrency, NAT gateway, load balancer, or customer-managed KMS key. DynamoDB uses on-demand billing. The function accesses business data only; there is no user/session/authentication table.

Retrieve the API URL at any time:

```bash
API_URL=$(aws cloudformation describe-stacks \
  --region "$AWS_REGION" \
  --stack-name experiment-tracker \
  --query 'Stacks[0].Outputs[?OutputKey==`ApiUrl`].OutputValue | [0]' \
  --output text)
API_URL=${API_URL%/}
printf '%s\n' "$API_URL"
```

The URL looks like `https://FUNCTION-ID.lambda-url.us-east-1.on.aws`. Keep its host unchanged and do not append `/api` in the website's server setting. API requests use paths such as **`$API_URL/api/health`**, **`$API_URL/api/auth/session`**, and **`$API_URL/api/projects`**. The Lambda adapter forwards the full path to Flask; there is no additional deployment-stage prefix.

## 3. Verify the API and load the teacher example

From `experiment-tracker/`:

```bash
curl --fail-with-body "$API_URL/api/health"
curl --silent --output /dev/null --write-out '%{http_code}\n' "$API_URL/api/projects"
python3 scripts/data.py --url "$API_URL" seed
```

The health request should return `{"status":"ok"}`. The unauthenticated projects request should return **401**. The seed tool prompts for the configured access code, exchanges it for a token, and imports `examples/teacher-recall-bar.json`, skipping an identical existing plot.

The project is **HMSB Document Retrieval Application**, the experiment name is **teacher recall**, and the chart measures **NDCG@k** at `k = 1, 3, 5` for cosine similarity, base PPL, and weighted PPL. All supplied values are retained.

You can also send the full import directly. The following example uses the documented default code; substitute your configured code when changed:

```bash
curl --fail-with-body --request POST "$API_URL/api/import" \
  --header 'Content-Type: application/json' \
  --header 'X-Access-Code: 666666' \
  --data-binary @examples/teacher-recall-bar.json
```

Direct `/api/import` calls **append** plots on repetition. The seed/restore tool performs duplicate checks, which is why `seed` is preferable when rerunning setup. The [README](README.md#complete-bar-plot-upload) contains the complete JSON request and metric format.

## 4. Connect the GitHub Pages frontend

For immediate use, open **https://n4feng.github.io/experiments/**, paste `API_URL` into **Server URL**, and enter the code. The server address is remembered on that device; the token stays in the browser tab.

To make the deployment the site's default, use a local checkout with GitHub push access, or the CloudShell checkout if you have configured GitHub authentication. From the repository root, set the public runtime configuration to your actual Function URL:

```bash
export API_URL='https://YOUR-FUNCTION-ID.lambda-url.us-east-1.on.aws'
python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path('experiment-tracker/frontend/public/config.json')
path.write_text(json.dumps({'apiBaseUrl': os.environ['API_URL'].rstrip('/')}, indent=2) + '\n')
PY
cd experiment-tracker/frontend
npm ci
npm run build
cd ../..
git add experiment-tracker/frontend/public/config.json experiments/
git commit -m "Configure experiment tracker AWS API URL"
git push
```

Build with a Node.js version supported by the frontend's Vite dependency, such as Node.js 22.12+ or 24. The build copies `frontend/public/config.json` into **`experiments/config.json`** and regenerates the published assets. Commit **both** the source configuration and generated `experiments/` output. Editing only the published copy will be undone by the next build.

Wait for the repository's checks and Pages deployment to finish, then refresh the website. If the browser remembers an older manually entered URL, sign out and replace that value on the login screen. The API URL is public configuration; never put an access code or signing key into either JSON file.

Verify the full workflow in the website: sign in, open the project, open **teacher recall**, inspect all three NDCG series, switch bar/line views, and open the exact-values table. Creating a project or custom metric should persist after refresh. Chart previews and view controls do not call DynamoDB. In **View data**, dragging series columns previews their order locally; **Save order** writes the chosen array to the plot settings in DynamoDB.

## Update or rotate credentials

For an ordinary update, from `experiment-tracker/`:

```bash
git pull --ff-only
python3 scripts/deploy_aws.py --region "$AWS_REGION" --stack-name experiment-tracker
```

Review the change set before confirming. The existing DynamoDB table and secrets are reused. Deploy this backend update to enable **View data → Save order** (`plot.series_order`); GitHub Pages updates alone cannot enable the new server validation. No table migration is required. After deployment, save an order and reload the experiment to confirm it persists. If you intend to change credentials:

```bash
python3 scripts/deploy_aws.py --region "$AWS_REGION" --stack-name experiment-tracker --rotate-access-code
python3 scripts/deploy_aws.py --region "$AWS_REGION" --stack-name experiment-tracker --rotate-session-secret
```

Each is a separate optional deployment. Either rotation invalidates existing browser sessions. Keep the same stack name and region to update the existing deployment.

## Data limits and atomic imports

The public JSON API retains its **4 MiB maximum request size**, **100 plots per import**, **10,000 rows per plot**, and **64 numeric series per plot**. DynamoDB's individual-item limit is **400 KB**, so the AWS backend compresses normalized import data and splits it into chunks of at most **250 KiB**; it does not truncate rows to fit one item. An import's compressed batch must be at most **3,500 KiB (3,584,000 bytes)** to reserve space for metadata within DynamoDB's transaction size limit. Exceeding that guard returns **413** before staging; split the batch into smaller uploads.

The backend stages new data with an expiry and publishes a completed import atomically. Readers do not see partly written projects, experiments, or plots if a request fails. Abandoned staged chunks are eligible for TTL cleanup; DynamoDB TTL deletion is asynchronous. The compressed-batch guard and bounded publication transaction keep the import within DynamoDB transaction size/action limits. Splitting a batch creates multiple independent atomic imports, not one transaction across all uploads.

Lambda's buffered responses have a **6 MiB** limit. The browser and JSON backup tool fetch one plot per page and assemble the results automatically. Direct clients should request `GET /api/experiments/<id>?limit=1&cursor=0`, repeat using each returned `next_cursor`, and stop when it is `null`. `plot_count` remains the total number of plots. The legacy unpaginated response is retained for compatibility, but large experiments can exceed the response limit when requested that way.

Authentication, health checks, and preflight do not read or write this table. DynamoDB requests occur when fetching or saving business data. Different business API calls have different costs: one plot fetch may read metadata plus multiple data chunks, and an atomic import may perform multiple writes.

## Back up and restore experiment data

Use the authenticated application-level JSON tool. It works with both the deployed AWS API and the local SQLite API, and does not require a managed full-table export or an S3 backup bucket.

From `experiment-tracker/`:

```bash
mkdir -p "$HOME/tracker-backups"
python3 scripts/data.py --url "$API_URL" export --output "$HOME/tracker-backups/tracker-backup.json"
```

The tool creates a new owner-readable/writable JSON file and refuses to overwrite an existing file; choose a new filename for each backup. The file contains projects, experiments, custom metric definitions, complete plot data, and plotting options. Keep it outside the public Git repository. If you ran the export in CloudShell, use **Actions → Download file** to save a copy on your computer; CloudShell should not be your only copy. Export when no one is writing if you need a consistent snapshot across multiple API reads.

To restore into a new deployment:

```bash
RESTORE_API_URL='https://NEW-FUNCTION-ID.lambda-url.us-east-1.on.aws'
python3 scripts/data.py --url "$RESTORE_API_URL" restore --input "$HOME/tracker-backups/tracker-backup.json"
```

Restore recreates custom metrics and empty projects/experiments as well as plots. It reuses matching names, preserves existing project/experiment descriptions, refuses incompatible metric definitions, and skips already-restored plot data/options while preserving intentional duplicates in the backup. It uses multiple authenticated requests, so a whole backup restoration is not one database transaction; rerun it after a recoverable interruption. Review conflicts rather than assuming an existing same-named custom metric has the same meaning. Internal record IDs may change on restore, while names and recorded results are preserved.

Exports and restores incur normal Lambda/DynamoDB request usage. The template does not enable paid continuous backup/PITR; if you enable it later, include its storage charges in your budget.

## Cost controls and cleanup

**Under US$2/month is a low-traffic target, not a hard cap.** Lambda invocations, execution time, DynamoDB request units, stored data, logs, artifact storage, backups, and data transfer can contribute to the bill. Existing account usage may have already consumed shared AWS allowances. Prices vary by region and can change; consult [Lambda pricing](https://aws.amazon.com/lambda/pricing/) and [DynamoDB pricing](https://aws.amazon.com/dynamodb/pricing/) for the selected region.

Keep the supplied low-cost configuration: on-demand DynamoDB, no provisioned concurrency, no customer VPC/NAT gateway, and short log retention. The log group keeps **7 days** by default. The small private artifact bucket also has S3 storage/request charges; it is not an unlimited free resource. A reserved-concurrency ceiling, if your account permits one, limits parallel executions but is not a monthly cost cap. Even rejected access-code attempts reach Lambda and can be billed.

Check Billing after initial use and set alerts before sharing the URL. DynamoDB's Standard storage allowance is distinct from on-demand read/write request charges; do not assume free provisioned capacity covers on-demand requests.

Before deleting the deployment, export its records and note the **TableName**, **FunctionName**, and deployment artifact bucket from the helper/stack. To remove the stack:

```bash
aws cloudformation delete-stack --region "$AWS_REGION" --stack-name experiment-tracker
aws cloudformation wait stack-delete-complete --region "$AWS_REGION" --stack-name experiment-tracker
```

The table has **retention policies**, so deleting the stack intentionally leaves your DynamoDB data behind. The deployment artifact bucket is separate and is also retained. These resources can continue to incur storage charges. When you are certain the backup is usable and the resources are no longer needed, delete the retained table and the dedicated artifact bucket through the AWS console. Do not remove a shared artifact bucket or another application's table.

A new stack does not automatically adopt a previously retained table. Restore the JSON backup into the new stack's table, or arrange a deliberate CloudFormation resource import. Also remove/update the website's runtime API URL when its backend is no longer available.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| SAM build cannot find pip/make or download dependencies | Check `python3 -m pip --version`, `make --version`, and outbound package access. The Makefile build does not need local Python 3.12 or Docker. |
| Deployment cannot create/pass an IAM role | Use an AWS deployment identity with the required permissions; the function's runtime role is not the deployment identity. |
| Reserved-concurrency quota error | Omit the optional concurrency setting or choose a value permitted by your account. |
| Login works with curl but the browser cannot connect | Confirm `CORS_ORIGINS` contains exactly `https://n4feng.github.io`, use HTTPS, and enter the Function URL without `/api`. |
| Health works but projects returns 401 | This is expected without authentication. Sign in or send a valid bearer token/access-code header. |
| 413 or an import-size/transaction error | Reduce the JSON request or split its plots into smaller requests. No partial import should be visible. |
| 500 or unexpected deployment behavior | Inspect the function's CloudWatch log group; keep credentials and request bodies out of diagnostic posts. |
| Frontend keeps using the old API | Replace its saved server URL at login and confirm both source and built `config.json` were updated. |

## AWS references

- [CloudShell environment and included tools](https://docs.aws.amazon.com/cloudshell/latest/userguide/vm-specs.html)
- [CloudShell authentication](https://docs.aws.amazon.com/cloudshell/latest/userguide/welcome.html)
- [AWS SAM installation](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- [Lambda Function URL security and authorization](https://docs.aws.amazon.com/lambda/latest/dg/urls-auth.html)
- [DynamoDB transaction and item constraints](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Constraints.html)
- [AWS Budgets](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html)
