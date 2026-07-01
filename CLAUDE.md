# Optimus Data Analytics Dashboard

## What this is
A Streamlit dashboard for analysing Quality/Defects (and similar) data exported
from the **Optimus (O2)** platform — a Singapore government system (JTC infra
projects) that we have **no backend control or configuration rights** over.

The workflow is deliberately export-based:
1. **Export** data from Optimus as an Excel file (`.xlsx`).
2. **Upload** that file into this app.
3. **Analyse** — the app parses it generically and an AI tab interprets it.

This export-then-upload pipeline is the core constraint: we cannot change the
Optimus website, so all logic lives downstream of the Excel export.

## Architecture decision: no per-form-type logic (2026-06-24)
Optimus has many export form types (Quality Defects Inspection, Safety
Observation, and more to come) with **different, unrelated schemas**. The
app originally had four "domain team" tabs (Project Mgt / Cost / Safety /
Quality) with hand-coded logic (`compute_aging()`, `consolidate_taxonomy()`,
exact column-name constants) tuned specifically to the Quality Defects form.

Once a second form type (Safety Observation) was confirmed with a
genuinely different, much flatter schema (no due-date, no closure
timestamp, no trade taxonomy), showing all four Quality-tuned tabs on
Safety data became actively misleading — e.g. a "Quality — Defect
Frequency" tab labeling Safety inspection-type counts. Rather than build a
form-type detector and a growing per-form tab/logic mapping (which doesn't
scale as more form types are added), **all exact-schema logic was
removed**. The app now has three tabs:

| Tab | What it does |
|-----|--------------|
| 📊 Overview | Plain frequency counts (`bar_of_counts()`) for whatever status/category columns `guess_columns()` generically detects — no business-specific formulas, no aging, no Pareto, no heatmap. |
| 🤖 AI Insights | Sends the same generic aggregate summary to Gemini, which infers the form type and what matters from the data itself (see AI layer section). This is where form-specific interpretation now lives — in the AI's reasoning, not in our code. |
| 🗂 Raw Data | Unfiltered view + CSV download + detected column types (debugging aid). |

`guess_columns()` (name/dtype/cardinality heuristics) is the **only**
column-classification logic left, and it's intentionally form-agnostic.
Removed entirely: `compute_aging()`, `consolidate_taxonomy()`,
`QUALITY_TAXONOMY`, `CLOSED_WORKFLOW_KEYWORDS`, `RAISED_DATE_COL` /
`DUE_DATE_COL` / `WORKFLOW_COL` / `MODIFIED_TIME_COL`.

## Confirmed real export schemas
### Quality Defects Inspection Form
`Quality Inspection Forms/Issues-51-Quality Defects Inspection Form.xlsx`,
sheet `Issues`, 28 columns, only 3 sample rows so far. Key fields: raised
date `Date of Inspection`, target closure `Due Date for Inspection
Closure`, status `Workflow`, last-edit `Modification time`. No actual
closure-date column. No single "Defect Code" column — instead 4
trade-specific System+Component column pairs (Architectural/Electrical/
Mechanical/C&S), only one pair populated per row depending on `Trade
(Discipline)`. `Created by` / `Modified by` contain real names.
Confirmed `Workflow` values: `SO Rep Asst (S) Acknowledgment`, `Approved
(PIR)`.

### Safety Observation Form
`Safety Observation Forms/Issues-51-Safety Observation Form.xlsx`, sheet
`Issues`, 8 columns, **141 sample rows** (much fuller sample). Far flatter
than the Quality form: `ID`, `S/N`, `Workflow`, `Inspection Category` (2
values: Observation Regime / Assessment Regime), `Inspection Type` (2
values: Physical / Drone), `Inspection Date`, `Description of Observation`
(free text), `JTC SWO Recommendation` (constant `"N.A."` in this sample).
**No due date, no closure/modification timestamp, no trade taxonomy** —
genuinely less metadata than the Quality form, not just a different shape
of the same thing. Confirmed `Workflow` values: `Approved` (134/141),
`SO Acknowledgement` (4), `RE/ RTO Verification` (3).

Across both forms, 5 distinct `Workflow` values have now been observed and
all are consistent with a simple "contains approved/closed/complete/
rejected → terminal" reading — though that heuristic is no longer encoded
in app.py (see architecture decision above); it's left to the AI's
judgment now.

### pandas dtype bug fixed (2026-06-24)
`guess_columns()`'s category detection checked `series.dtype == "object"`,
which fails on pandas ≥ 2.x/3.x's newer dedicated string dtype (`"str"`,
distinct from legacy `"object"`) — confirmed on pandas 3.0.3. This silently
misclassified categorical columns as free text on **every** upload; it was
masked for the Quality form only because its categories happened to come
through a since-removed manual taxonomy override, not through this check.
Fixed by switching to `pd.api.types.is_string_dtype()`, which handles both
dtypes.

## Next steps when more data is available
1. Get a sample of a third Optimus form type to stress-test that the
   generic approach (no per-form logic) continues to hold up, not just for
   two forms.
2. Consider contractor-level quality scoring (Quality form specific, on
   hold pending a fuller export).

## AI layer (added for the public demo — dummy data only)
The "🤖 AI Insights" tab sends an aggregated text summary of the uploaded
data — counts and a capped sample of free-text notes, never the full raw
rows — to the **Gemini API** (`google-genai` SDK, `GEMINI_MODEL =
"gemini-2.5-flash"` in app.py). `build_data_summary(df, cols)` builds that
summary entirely from `guess_columns()`'s generic output (no form-specific
fields referenced by name); `generate_ai_insights()` makes the single call.
`AI_SYSTEM_PROMPT` explicitly tells Gemini it isn't told the form type in
advance and should infer it from the data — confirmed working well: given
the Safety form's summary, Gemini correctly opened its response with "This
data appears to be from Safety Observations forms" unprompted.
- **Free-text column selection is also generic**: a text column is sampled
  only if its average word count per value is ≥ 3 (multi-word narrative
  text), which cleanly separates genuine notes (e.g. `Description of
  Observation`) from IDs/serial numbers (e.g. `ID`, `S/N`) without
  hardcoding any column names — works the same on any form type.
- **Provider choice**: deliberately Gemini, not Claude/Anthropic — the
  Claude subscription used for development (this Claude Code session) is
  not the user's own and should not be wired into the deployed app. Gemini
  was chosen because Google AI Studio offers a free-tier API key with no
  card required, which fits a personal/student project.
- **API key**: read via `get_gemini_client()` from `st.secrets`
  (`.streamlit/secrets.toml` locally — gitignored — or the app's Settings ->
  Secrets on Streamlit Cloud) or the `GEMINI_API_KEY` env var. The tab
  shows a setup hint instead of erroring if no key is configured.
- **Model name caveat**: `gemini-2.5-flash` was chosen for confidence (a
  well-established name as of when this was wired up) over conflicting,
  unverifiable web search results for newer names. If the AI Insights call
  starts failing, check the current free-tier model name in the picker at
  aistudio.google.com and update the `GEMINI_MODEL` constant.
- **Cost control**: gated behind a "Generate AI Insights" button (not
  auto-run on every Streamlit rerun/upload) and the summary text itself is
  capped (top 5 category columns, top 8 values each, ≤15 sampled free-text
  values per qualifying text column) to keep each call small and within
  free-tier limits.
- **Reliability note**: free-tier Gemini calls can return `503 UNAVAILABLE`
  under demand spikes (observed in practice) — currently surfaced as a
  plain `st.error`, not auto-retried. Worth adding retry-with-backoff if
  this becomes frequent.
- **Governance reminder**: this was added on the explicit assumption that
  only public/dummy data reaches this app (see Data governance below) — the
  real exports' `Created by` / `Modified by` / observation-notes fields
  contain real names and project specifics. Revisit whether an AI layer is
  acceptable at all once real Optimus data is in scope, and note Gemini's
  free tier may have different data-usage terms than a paid API key —
  re-check before ever pointing this at real data.

## Deployment overview — three platforms coexist (2026-07-01)
All three hosting targets are live simultaneously. No conflicts — each uses its
own entry-point file and they share the same `app.py` / `requirements.txt`:

| Platform | Entry point | Gemini key source | Status |
|---|---|---|---|
| Streamlit Community Cloud | auto-detected `app.py` | `.streamlit/secrets.toml` via SCC Secrets UI | Live |
| RabbitDeploy | `Procfile` | Not configured (AI tab shows setup hint) | Live POC |
| Airbase (GCC) | `Dockerfile` + `airbase.json` | `.env` file, injected by Airbase CLI at runtime | Live POC |

Do **not** remove any of these files — they serve different platforms.

## Airbase deployment (2026-07-01)
Airbase is GovTech's self-serve GCC-hosted PaaS (Pilot stage). It is
Docker-based and CLI-driven — no GitHub import, no web UI deploy button.
Separate from RabbitDeploy and Streamlit Community Cloud.

Live URL (requires TechPass / accessible on normal browser):
**https://o2-data-analytics.fbi-dbe.airbases.gov.sg/** *(confirm current URL
in the Airbase console Environments tab — handle is `fbi-dbe/o2-data-analytics`)*

### CLI workflow
```bash
airbase login          # opens browser TechPass SSO
airbase deploy --yes   # builds Docker image locally, pushes to Airbase registry, deploys
```
Airbase CLI is installed at `C:\Users\jingr\AppData\Local\Airbase\CLI\airbase.exe`.
If `airbase` isn't found in a terminal, open a **new** terminal window after
install (PATH update only applies to terminals opened after the installer ran).

### Key files
- **`Dockerfile`** — uses `gdssingapore/airbase:python-3.13` (GDS-hardened base
  image, mandatory for CSP compliance on GCC). Runs as non-root `USER app`
  (GCC security requirement). Port `${PORT:-3000}` (Airbase injects `$PORT`
  at runtime; default 3000 matches `airbase.json`).
- **`airbase.json`** — `framework: "container"`, `handle: "fbi-dbe/o2-data-analytics"`,
  `port: 3000`. No env var support in this file.
- **`.env`** — gitignored, not in `.dockerignore`. Airbase CLI reads this at
  deploy time and injects values as runtime environment variables. Currently
  contains `GEMINI_API_KEY`. **Never hardcode secrets in Dockerfile ENV
  directives** (Airbase docs explicitly prohibit this).

### Critical gotchas encountered
**Docker image caching**: `airbase deploy` reuses an existing local Docker image
with the same tag even if the Dockerfile changed. If changes aren't taking
effect, delete the cached image first:
```bash
docker rmi local.airbase.sg/o2-data-analytics:<image-id>
# then redeploy
airbase deploy --yes
```
Find the `<image-id>` in Docker Desktop or `docker images`.

**Port mismatch → 502**: `airbase.json` says port 3000 but if the cached image
still runs on the old port (e.g. 8501), Airbase's Kong gateway gets a 502.
Fix: delete cached image, force fresh build.

**`gatherUsageStats = false` required**: Streamlit's metrics utility tries to
write a machine-ID file to a restricted path in the container on first start,
causing a startup error. Fixed by `.streamlit/config.toml` setting
`gatherUsageStats = false`. Without this, the container may appear to crash
before the app comes up.

**CSP warning (non-blocking)**: The browser console shows a CSP violation for
Streamlit's built-in inline script `window.prerenderReady = false` in its
`index.html`. This is Streamlit's own code, not ours. Airbase's own
documentation states "Streamlit generally works fine with CSP" — this warning
does not break any app functionality (file upload, charts, AI Insights all
work). No fix needed; it's an accepted known limitation.

**`gdssingapore/airbase:python-3.13` is mandatory** (not `python:3.11-slim` or
similar). The standard Python Docker images are not GCC-hardened and produce
real CSP violations that do break the app. The GDS base image resolves this.

### `.streamlit/config.toml` — required settings
```toml
[browser]
gatherUsageStats = false   # prevents container startup error (restricted filesystem)
```

## RabbitDeploy POC deployment (2026-06-24)
Live POC URL: **https://o2-analytics-dashboard.cio.sandbox.gov.sg/**

RabbitDeploy is GovTech CIO Office's internal sandbox PaaS, separate from
the public Streamlit Cloud demo — see the "Guide: Direct Claude Path" the
user pasted in-session for the full onboarding flow (TechPass login →
`#ask-rabbit` Slack for workspace assignment → create project). Only
reachable via a SEED/COMET device; the deployed app itself is a normal
public-ish gov-network URL though.

### Getting code in: ZIP upload, not git
There's no GitHub-import option in the UI. Two paths exist: a Repository
Token for `git push`, or **"Update Code" → upload a `.zip`**. We used the
ZIP path specifically because the SEED laptop can't run Claude Code (not
the user's device) and downloads are blocked there — so the workflow is:
develop with Claude on the main machine → `git archive --format=zip -o
out.zip HEAD` to export exactly the git-tracked files (auto-excludes
`.git`, `__pycache__`, venv, real Excel data, secrets — all gitignored) →
transfer just that one ZIP to the SEED laptop → upload via "Update Code".
When file transfer itself was also blocked, the fallback was recreating
each file by hand in VS Code via clipboard paste (text clipboard apparently
isn't blocked, only file downloads) and zipping **locally on the SEED
laptop** (Explorer → Send to → Compressed folder — no download involved).

### Critical deployment gotcha: Procfile required for non-FastAPI/Flask apps
Clicking "Deploy as POC" creates two Northflank services per product:
- **`<project>-main`** — the actual app container
- **`cf-<project>`** — a Cloudflare tunnel sidecar (`cloudflared`) that
  gives the public `*.cio.sandbox.gov.sg` URL without opening inbound
  firewall ports on Northflank. Public request → Cloudflare edge → this
  tunnel → `-main`.

RabbitDeploy's build script (visible in the Build Log) only **auto-detects
FastAPI or Flask** — it greps `main.py`/`app.py`/etc. for `= FastAPI(` or
`= Flask(` patterns, and if neither matches (as for any other framework,
Streamlit included), it falls back to a hardcoded `exec uvicorn main:app
--host 0.0.0.0 --port 3000`. That doesn't exist in a Streamlit project, so
the container crash-loops forever (Server Log: `Could not import module
"main"`, repeating every ~30s). From the browser this shows up as
`upstream connect error... connection timeout` — the Cloudflare tunnel is
working fine, it's just connecting to a container with nothing listening.

**Fix: a `Procfile`** (exact filename, no extension) in the repo root —
the build script checks for this *before* the FastAPI/Flask fallback, and
uses its `web:` line verbatim if present:
```
web: streamlit run app.py --server.port 3000 --server.address 0.0.0.0 --server.headless true --server.enableCORS false --server.enableXsrfProtection false
```
Each flag matters: port `3000` + `0.0.0.0` matches the container's
`EXPOSE 3000` (the ingress won't reach any other port/interface);
`--server.headless true` skips Streamlit's interactive first-run "enter
your email" prompt, which would otherwise hang forever in a
non-interactive container (the exact issue hit locally earlier in this
project, fixed there via a `credentials.toml` — headless mode is the
container-appropriate equivalent); `--server.enableCORS/XsrfProtection
false` is needed because Streamlit's default same-origin checks don't
match the Cloudflare tunnel's external hostname.

After adding the Procfile: re-zip, re-upload via "Update Code", then
"Rebuild" the `-main` service. Confirmed working — Server Log showed a
clean `Uvicorn server started on 0.0.0.0:3000` / `You can now view your
Streamlit app in your browser.`

### Database (not currently used)
"Create Database" offers a **Neon-hosted PostgreSQL** instance (AWS
Singapore, `ap-southeast-1`) with a dev connection string and Neon's
branching/autoscaling. **Not provisioned** — this app is stateless
(processes each upload in-memory per session), so there's nothing to
persist yet. Revisit only if a real cross-session persistence need shows
up (e.g. a history of past AI Insights).

### Production deployment — separately gated
The same project page has a "Deploy as Production" path (also Northflank,
via "GovPaaS Prod"), but it's blocked behind a Security Clearance /
"Production Ready Status" approval that has to be requested from CIOO —
not pursued; still POC-stage only.

## Data governance — IMPORTANT
Real Optimus/O2 records are **government data**. Before pointing this app at
live exports, the following must be decided with whoever owns data governance:
- **Where the app runs** (local laptop / internal gov server / GCC).
- **Whether any AI layer ever sees the data** (and if so, local-only e.g.
  Ollama vs. external endpoints).
Building and testing against sample/dummy data has no such constraint.

## Tech stack
- **Streamlit** — app framework / UI
- **pandas** — Excel parsing and data wrangling
- **openpyxl** — `.xlsx` reader backend for pandas
- **plotly** — interactive charts

## Run
```bash
# from the project root, with the virtual environment active
streamlit run app.py
```
App serves at `localhost:8501`. Upload an Optimus Excel export via the sidebar.

## Environment setup (reference)
```bash
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS / Linux
pip install streamlit pandas openpyxl plotly
```
