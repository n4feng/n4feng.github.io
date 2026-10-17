# Experiment tracker

A React dashboard for Naihe Feng's research experiments, backed by a Flask API and SQLite database.

The navigation is **Projects → Experiments → Metric plots**. The website supports creating projects, experiments, and metric definitions; pasting or uploading JSON; grouped bar and line plots; plot descriptions and axis labels; and viewing the underlying numbers.

## Hosting architecture

GitHub Pages serves the compiled frontend at **https://n4feng.github.io/experiments/**. GitHub Pages cannot execute Python, so run the Flask API on a separate HTTPS host with persistent disk storage. The existing Jekyll academic website is retained.

The static HTML, JavaScript, and login screen are public. Every experiment read and write goes through the authenticated API. Runtime records are stored in SQLite, never in the React bundle or GitHub. The requested example is intentionally included as a public sample in `examples/teacher-recall-bar.json`.

The only role is **root**, with read and write access. Its default access code is `666666`; configure `ROOT_ACCESS_CODE` on the server to change it. Because the default is documented in this public repository, set a private code before storing data that needs to remain private. The browser receives a time-limited session token after login; it never contains a built-in root code.

## Run locally

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

## Deploy the Flask server

One supported option is Docker on a Linux server with an HTTPS reverse proxy:

```bash
cd experiment-tracker
docker compose up --build -d
docker compose exec api flask --app app:create_app seed-example
```

The API binds to `127.0.0.1:8000` on the host. Point your existing HTTPS reverse proxy at this address. The `experiment-data` volume retains the database when the container is replaced. Do not remove the volume when updating the app.

The container uses Gunicorn, not Flask's development server. A Python-capable host can also run this from `experiment-tracker/server`:

```bash
gunicorn --bind 127.0.0.1:8000 --workers 2 --threads 2 'app:create_app()'
```

Set `DATABASE_PATH` to a persistent, writable disk path and `CORS_ORIGINS` to `https://n4feng.github.io`. Keep database backups outside this public repository. Use SQLite's online backup API or stop writes while taking a filesystem backup; copying only the main file during WAL writes can omit recent records.

| Server setting | Default | Purpose |
| --- | --- | --- |
| `ROOT_ACCESS_CODE` | `666666` | Root read/write access code |
| `DATABASE_PATH` | `server/instance/experiments.sqlite3` | Persistent SQLite file; Docker uses `/data/experiments.sqlite3` |
| `SECRET_KEY` | Generated and saved beside the database | Signs browser sessions; keep the `.secret` file private and persistent |
| `CORS_ORIGINS` | GitHub site plus localhost development origins | Exact comma-separated browser origins; Compose limits this to the GitHub site |
| `SESSION_TTL_SECONDS` | `43200` | Browser session lifetime: 12 hours |
| `AUTH_RATE_MAX_FAILURES` | `5` | Failed code attempts allowed per client within the window |
| `AUTH_RATE_WINDOW_SECONDS` | `60` | Failed authentication window in seconds |

Code rotation invalidates existing sessions. The API uses no authentication cookies. Behind a reverse proxy, the default rate limit sees the proxy's address, so its failure budget is shared by proxied visitors; configure trustworthy client-IP forwarding deliberately if you need per-visitor limits. Never trust arbitrary forwarded headers from the public internet.

After the API has a real HTTPS URL, put that URL in `frontend/public/config.json`:

```json
{"apiBaseUrl": "https://YOUR-API-HOST"}
```

Then rebuild and commit the source and generated assets:

```bash
cd experiment-tracker/frontend
npm ci
npm run build
```

Vite outputs to the repository's `experiments/` directory. Commit both `experiment-tracker/frontend/` and `experiments/` when making frontend changes. Jekyll copies the built page and assets; it excludes `experiment-tracker/` source and examples from the published website. Hash navigation keeps deep links compatible with GitHub Pages. Until a deployment URL is configured, visitors can enter the API URL on the login page.

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

Change `"type": "bar"` to `"type": "line"` for a line plot. Omitting the entire `plot` object uses defaults. The seed command imports the checked-in example once; its chart uses the complete supplied values without rounding the stored data.

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

Create requests for projects and experiments use `{"name":"...","description":"..."}`. All mutations return JSON. Invalid input fails with an actionable error; a failed multi-plot import leaves no partially created project, experiment, or plot behind.

## Checks

```bash
cd experiment-tracker/server
python -m unittest discover -s tests -v
cd ../frontend
npm ci
npm run build
```

The GitHub Actions workflow checks the API, builds React, and checks that committed Pages assets match the frontend source.

## References

- [GitHub Pages is static hosting](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages)
- [Flask production deployment](https://flask.palletsprojects.com/en/stable/deploying/)
- [Gunicorn deployment](https://flask.palletsprojects.com/en/stable/deploying/gunicorn/)
- [Vite static deployment](https://vite.dev/guide/static-deploy.html)
- [Visual reference: RISA experiment results](https://sujay152002.github.io/3D-Point-Cloud-Retrieval-RISA/)
