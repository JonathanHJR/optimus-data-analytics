# Optimus Data Analytics Dashboard

A Streamlit dashboard for analysing Quality/Defects data exported from
**Optimus (O2)** — a Singapore government system used on JTC infrastructure
projects. Export an Excel file from Optimus, upload it here, and get
domain-specific dashboards for four project teams plus an AI-generated
written summary.

**Live demo:** https://optimus-data-analytics.streamlit.app
*(demo only — upload sample/dummy data, never real government exports; see
[Data governance](#data-governance) below)*

## Features

- **📋 Project Mgt** — workflow-status breakdown and aging/overdue tracking
- **💰 Cost** — recurring-issue frequency as a rectification-cost proxy
- **⚠️ Safety** — cross-tab risk heatmap across two categorical fields
- **✅ Quality** — defect frequency and a Pareto (80/20) chart
- **🤖 AI Insights** — sends an aggregated summary (counts + a small sample
  of free-text notes, never the raw rows) to the Gemini API for a written
  analysis
- **🗂 Raw Data** — view/filter the uploaded sheet and download it as CSV

The app auto-detects column types from whatever sheet you upload, and wires
in exact-field logic when it recognises the real Optimus "Quality Defects
Inspection Form" schema (see `CLAUDE.md` for the full schema notes and the
reasoning behind specific design choices).

## Tech stack

- [Streamlit](https://streamlit.io) — app framework / UI
- [pandas](https://pandas.pydata.org) + [openpyxl](https://openpyxl.readthedocs.io) — Excel parsing
- [Plotly](https://plotly.com/python/) — interactive charts
- [Gemini API](https://ai.google.dev) (`google-genai`) — AI Insights tab

## Running locally

```bash
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS / Linux

pip install -r requirements.txt
streamlit run app.py
```

The app serves at `http://localhost:8501`. Upload an Optimus Excel export
(or any `.xlsx`/`.xls` file) via the sidebar to get started.

### Enabling AI Insights locally

Create `.streamlit/secrets.toml` (already gitignored) with:

```toml
GEMINI_API_KEY = "your-gemini-api-key"
```

Get a free-tier key at [aistudio.google.com](https://aistudio.google.com).

## Data governance

Real Optimus/O2 records are **government data**. This public demo is built
and tested against sample/dummy data only — real exports should never be
uploaded here. Before pointing any deployment of this app at live exports,
decide with whoever owns data governance:
- Where the app runs (local machine / internal gov server / GCC)
- Whether any AI layer should ever see real data (and if so, local-only vs.
  an external API)

See `CLAUDE.md` for the full project context, confirmed export schema, and
open questions.
