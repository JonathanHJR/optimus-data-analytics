# Optimus Data Analytics Dashboard

## What this is
A Streamlit dashboard for analysing Quality/Defects (and similar) data exported
from the **Optimus (O2)** platform — a Singapore government system (JTC infra
projects) that we have **no backend control or configuration rights** over.

The workflow is deliberately export-based:
1. **Export** data from Optimus as an Excel file (`.xlsx`).
2. **Upload** that file into this app.
3. **Analyse** — the app parses it and produces domain-specific dashboards.

This export-then-upload pipeline is the core constraint: we cannot change the
Optimus website, so all logic lives downstream of the Excel export.

## Domain teams (the four dashboard tabs)
The app serves four domain teams, each with its own tab:

| Team | Focus | Current visualisations |
|------|-------|------------------------|
| Project Management | Aging & workflow bottlenecks | Aging histogram, workflow-status counts |
| Cost (Contract Mgt) | Rectification / variation-order cost proxy | Recurring-issue frequency |
| Safety | Risk concentration | Cross-tab risk heatmap |
| Quality | Defect root cause | Defect-code frequency + Pareto chart |

## Confirmed real export schema (Quality Defects Inspection Form)
Confirmed against a real export (`Quality Defects Inspection Forms/Issues-51-
Quality Defects Inspection Form.xlsx`, sheet `Issues`, 28 columns). Differs
from the earlier guesses: it's `Workflow` (not "Workflow Status") and
`Trade (Discipline)` (not "Trade"). Key fields:
- Raised date: `Date of Inspection`
- Target closure: `Due Date for Inspection Closure`
- Status: `Workflow`
- Last-edit timestamp: `Modification time`
- **No actual closure-date column exists.** Only the target due date and a
  last-modified timestamp.
- **No single "Defect Code" column.** Instead there are 4 trade-specific
  System+Component column pairs (Architectural/Electrical/Mechanical/C&S),
  only one pair populated per row depending on `Trade (Discipline)`. Same
  split for `Type of Inspection Check` and `Quality Inspection Check Item`.
- `Created by` / `Modified by` contain real names — be mindful of this under
  the data governance section below.

`app.py` now wires to this exact schema:
- `consolidate_taxonomy()` coalesces each trade-specific pair into one
  unified `System` / `Component` / `Quality Inspection Check Item` /
  `Type of Inspection Check` column, used for Cost/Quality/Safety grouping.
- `compute_aging()` replaces the placeholder aging logic. Since there's no
  real closure date, a Workflow value is treated as "closed" if it contains
  `approved`, `closed`, `complete`, or `rejected` (see `CLOSED_WORKFLOW_KEYWORDS`
  in app.py) — closed items are aged raised→Modification time, open items
  raised→now, and "overdue" = open and past Due Date for Inspection Closure.
  **This keyword heuristic is unconfirmed** — only two Workflow values have
  been observed so far (`SO Rep Asst (S) Acknowledgment`, `Approved (PIR)`).
  Revisit once a fuller export shows the full set of Workflow states.
- The generic `guess_columns()` heuristic is kept as a fallback for sheets
  that don't match this exact schema.

## Next steps when more data is available
1. Confirm the full set of `Workflow` values to validate/replace the
   closed-state keyword heuristic above.
2. Consider contractor-level quality scoring.
3. Decide whether other Optimus export types (Safety, Cost-specific forms)
   need their own exact-schema wiring, or continue to share this one.

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
