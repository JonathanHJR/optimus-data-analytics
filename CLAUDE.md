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
