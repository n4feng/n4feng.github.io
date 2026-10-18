# Experiment tracker

A React dashboard for Naihe Feng's research experiments, backed by a Flask API. The recommended low-traffic deployment uses **AWS Lambda + DynamoDB**; SQLite remains available for local development.

The navigation is **Projects → Experiments → Metric plots**. The website supports creating projects, experiments, and metric definitions; pasting or uploading JSON; grouped bar and line plots; plot descriptions and axis labels; and viewing the underlying numbers.

## Hosting architecture

GitHub Pages serves React at **https://n4feng.github.io/experiments/** alongside the existing Jekyll academic website. A Lambda Function URL provides the Flask API's HTTPS endpoint, and DynamoDB stores experiment records. Follow [the complete AWS deployment guide](DEPLOY_AWS.md) to create the infrastructure in your own AWS account. The repository alone does not create an AWS deployment.

| Action | Where it runs | DynamoDB access |
| --- | --- | --- |
| Load HTML/CSS/JavaScript, display the login form | GitHub Pages / React | None |
| Chart rendering, bar/line toggles, tables, SVG export, local view controls | React | None |
| Verify the root code, issue or validate a signed session token | Flask in Lambda | None; code/signing key are environment configuration |
| Health checks and CORS preflight requests | Flask in Lambda | None |
| Fetch or save projects, experiments, custom metrics, plot data/options | Flask in Lambda | Business records only |

Opening a project or experiment can fetch its records from the API; the subsequent rendering and chart controls run in React. Keeping the login form in React does **not** move secret verification into the public JavaScript bundle. Lambda verifies the code and signed tokens without storing sessions or users in DynamoDB.

Static HTML, JavaScript, and the login screen are public. Every experiment read/write is authenticated. Runtime records are not added to the frontend bundle or Git history. The requested example is intentionally public in `examples/teacher-recall-bar.json`.

The only role is **root**, with read and write access. Its requested default code is `666666`. The deploy helper accepts Enter to retain this default or a hidden entry for a private code; a private code is recommended for nonpublic research data. The browser receives a time-limited token and stores it in the current browser tab, never a built-in root code.

## Deploy on AWS

Start with [DEPLOY_AWS.md](DEPLOY_AWS.md). The supplied SAM template and deploy helper create the table, function, execution role, logs, and HTTPS Function URL. You do not need an EC2 instance, domain name, DNS record, or reverse proxy for this deployment.

The architecture is intended to remain **under US$2/month with light personal usage**, but that is a usage-dependent estimate, not a billing cap. Read the deployment guide's budget and cleanup steps before deploying. DynamoDB stores experiment data only; authentication makes no database calls.

After deployment, set the actual Function URL in `frontend/public/config.json`, rebuild, and commit both the source configuration and generated `experiments/` assets. Until that is done, the login page accepts a manually entered API URL. The guide includes the exact commands.

## Run locally with SQLite

From the repository root:

```bash
python -m venv experiment-tracker/server/.venv
source experiment-tracker/server/.venv/bin/activate
pip install -r experiment-tracker/server/requirements.txt
cd experiment-tracker/server
flask --app app:create_app seed-example
flask --app app:create_app run --port 8000
```

In another terminal:

```bash
cd experiment-tracker/frontend
npm ci
npm run dev
```

Open `http://localhost:5173/experiments/`, set the API server URL to `http://localhost:8000`, and enter the access code. Local HTTP is allowed only for loopback hosts; deployed API URLs must use HTTPS.

Docker Compose is also retained for local use:

```bash
cd experiment-tracker
docker compose up --build -d
docker compose exec api flask --app app:create_app seed-example
```

It binds the API to `127.0.0.1:8000` and keeps SQLite in the `experiment-data` volume. Do not remove that volume when updating a local workspace. Compose restricts browser CORS to the published GitHub site by default; use the Python development command above for the local Vite frontend, or explicitly add its origin to Compose's `CORS_ORIGINS`.

| Server setting | Default / deployment value | Purpose |
| --- | --- | --- |
| `STORAGE_BACKEND` | `sqlite` locally; `dynamodb` in SAM | Selects the data store |
| `DYNAMODB_TABLE` | Table created by SAM | AWS experiment records |
| `ROOT_ACCESS_CODE` | `666666` unless changed | Root read/write access code |
| `SECRET_KEY` | Generated once by the deploy helper; reused on updates | Signs stateless sessions; required with DynamoDB |
| `DATABASE_PATH` | `server/instance/experiments.sqlite3` locally | Local SQLite database; Docker uses `/data/experiments.sqlite3` |
| `CORS_ORIGINS` | AWS: `https://n4feng.github.io` | Exact allowed browser origins, without URL paths |
| `SESSION_TTL_SECONDS` | `43200` | Session lifetime: 12 hours |
| `AUTH_RATE_MAX_FAILURES` | `5` | Failed code attempts allowed per client within a window |
| `AUTH_RATE_WINDOW_SECONDS` | `60` | Authentication failure window in seconds |

SQLite development generates and saves its signing secret beside the database if none is configured. Lambda always uses the stable environment secret; it never uses `/tmp` as a persistent database or key store. Code or signing-key rotation invalidates existing sessions. Authentication uses no cookies. The in-memory failed-login budget is local to a running process/Lambda environment, not a global distributed limit; it creates no DynamoDB records.

Keep local databases and backups outside this public repository. For SQLite backups use its online backup API, or stop writes before copying files; copying only the main database during WAL writes can lose recent records. The [authenticated JSON backup tool](DEPLOY_AWS.md#back-up-and-restore-experiment-data) also works with the local API.

## Complete bar-plot upload

Set `API_BASE` to your Flask host, without `/api` at the end. This request creates the named project and experiment if necessary, then appends the plot. Repeating an import appends another plot; it does not overwrite an existing one.

```bash
API_BASE='https://YOUR-API-HOST'
curl --fail-with-body --request POST "$API_BASE/api/import" \
  --header 'Content-Type: application/json' \
  --header 'X-Access-Code: 666666' \
  --data-binary @experiment-tracker/examples/teacher-recall-bar.json
```

Use your configured root code instead of `666666` if changed. Run this command from the repository root. The full payload is:

```json
{
  "project": {"name": "HMSB Document Retrieval Application"},
  "experiment": {"name": "teacher recall"},
  "metrics": [{
    "metric": "ndcg_at_k",
    "data": [
      {"k": 1, "cosine_ndcg": 0.5138458002987476, "base_ndcg": 0.5752039526600023, "weighted_ndcg": 0.6070320579110652},
      {"k": 3, "cosine_ndcg": 0.6768888758703948, "base_ndcg": 0.7250525992014297, "weighted_ndcg": 0.7556947189052264},
      {"k": 5, "cosine_ndcg": 0.7204136032625917, "base_ndcg": 0.7620344135559811, "weighted_ndcg": 0.7900924993035837}
    ],
    "plot": {
      "type": "bar",
      "title": "Teacher retrieval quality",
      "description": "Comparison of cosine similarity, base PPL, and weighted PPL. Higher NDCG is better.",
      "x_key": "k",
      "x_label": "Retrieval cutoff (k)",
      "y_label": "NDCG@k",
      "y_min": 0,
      "y_max": 1,
      "series_labels": {"cosine_ndcg": "Cosine similarity", "base_ndcg": "Base PPL", "weighted_ndcg": "Weighted PPL"},
      "colors": {"cosine_ndcg": "#94a3b8", "base_ndcg": "#60a5fa", "weighted_ndcg": "#2dd4bf"}
    }
  }]
}
```

Change `"type": "bar"` to `"type": "line"` for a line plot. Omitting the entire `plot` object uses defaults. The seed commands skip an identical checked-in example; its chart uses the complete supplied values without rounding the stored data.

## Metric data formats

`ndcg_at_k` is the canonical name (not `ndct_at_k`). Each row requires a positive integer `k` and at least one numeric score. Scores must be finite and in `[0, 1]`. There are no fixed method names: `cosine_ndcg`, `base_ndcg`, `weighted_ndcg`, or additional numeric fields are all discovered and plotted. Each `k` must be unique. Missing series values are shown as gaps, not zero.

F1, precision, recall, and accuracy use an `x` field and one or more scores in `[0, 1]`:

```json
{"metric": "f1", "data": [{"x": "baseline", "macro_f1": 0.72, "micro_f1": 0.76}, {"x": "weighted", "macro_f1": 0.79, "micro_f1": 0.82}]}
```

Define a metric through the website or `POST /api/metrics`:

```json
{"id": "latency_ms", "name": "Latency", "description": "Mean request latency in milliseconds; lower is better.", "x_key": "batch_size"}
```

Then upload it with its declared x field and any numeric series:

```json
{"metric": "latency_ms", "data": [{"batch_size": 1, "baseline": 72.5, "optimized": 51.3}, {"batch_size": 8, "baseline": 180, "optimized": 125}], "plot": {"type": "line", "x_label": "Batch size", "y_label": "Latency (ms)"}}
```

## Size and transaction limits

All deployments accept JSON requests up to **4 MiB**, with at most **100 plots per import**, **10,000 rows per plot**, and **64 numeric series per plot**. Every row must include its metric's x field and at least one finite score. An omitted series is a gap; an explicit `null` is not a numeric score.

The AWS backend compresses and chunks plot data instead of assuming a full experiment fits in one DynamoDB item. A normalized import batch must compress to at most **3,500 KiB**, split into chunks of at most **250 KiB**. Oversized imports are rejected before publication; failed imports never expose a partial project, experiment, or plot. See [AWS deployment and limits](DEPLOY_AWS.md#data-limits-and-atomic-imports) for storage-specific limits. Split large exports into multiple experiment requests when necessary. The browser and backup tool fetch experiment plots one page at a time to stay below Lambda's buffered response limit.

## API reference

All routes below except health and session creation require `Authorization: Bearer TOKEN` or `X-Access-Code: CODE`. A session is created with `POST /api/auth/session` and JSON `{"code":"666666"}`; its response contains `token`.

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/health` | API availability |
| POST | `/api/auth/session` | Exchange access code for a session token |
| GET / POST | `/api/projects` | List or create projects |
| GET | `/api/projects/<id>` | Project and experiment list |
| POST | `/api/projects/<id>/experiments` | Create an experiment |
| GET | `/api/experiments/<id>` | Experiment and its plots |
| GET / POST | `/api/metrics` | List or define metrics |
| POST | `/api/experiments/<id>/plots` | Upload `{metric, data, plot?}` |
| PATCH | `/api/plots/<id>` | Update `{plot: {...}}` options |
| POST | `/api/import` | Atomically import a project, experiment, and metric plots |

Experiment reads support `GET /api/experiments/<id>?limit=1&cursor=0`. Follow the returned `next_cursor` until it is `null`; `plot_count` is the total, while `plots` contains the current page. The React frontend and backup tool assemble these pages automatically. Without pagination parameters, the endpoint retains its original full-response behavior, which can exceed Lambda's response limit for a large experiment.

Create requests for projects and experiments use `{"name":"...","description":"..."}`. All mutations return JSON. Invalid input fails with an actionable error; a failed multi-plot import leaves no partially created project, experiment, or plot behind.

## Checks

```bash
cd experiment-tracker/server
python -m unittest discover -s tests -v
cd ../frontend
npm ci
npm run build
```

The GitHub Actions workflow checks the API and Lambda/DynamoDB paths, builds React, and checks that committed Pages assets match the frontend source. These checks do not deploy AWS resources.

## References

- [GitHub Pages is static hosting](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages)
- [Flask production deployment](https://flask.palletsprojects.com/en/stable/deploying/)
- [Gunicorn deployment](https://flask.palletsprojects.com/en/stable/deploying/gunicorn/)
- [Vite static deployment](https://vite.dev/guide/static-deploy.html)
- [Visual reference: RISA experiment results](https://sujay152002.github.io/3D-Point-Cloud-Retrieval-RISA/)
