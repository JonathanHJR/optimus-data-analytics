"""
Optimus Data Analytics Dashboard
---------------------------------
A Streamlit app for analysing exports from the Optimus (O2) platform as
Excel files. Optimus has many export form types (Quality Defects
Inspection, Safety Observation, and more to come) with different schemas,
so this app deliberately avoids hardcoding logic to any one form's exact
column names. It auto-detects column types generically, shows a quick
aggregate overview, and leaves the actual interpretation to an AI Insights
tab — that's the part that should adapt per form type, not our code.

Run with:  streamlit run app.py
"""

import io
import json
import os
import time
from pathlib import Path
import pandas as pd
import streamlit as st
import plotly.express as px
from google import genai
from google.genai import types as genai_types

import db

# Verify this against the model picker at aistudio.google.com if AI Insights
# calls start failing — free-tier model names/availability change over time.
GEMINI_MODEL = "gemini-2.5-flash"

# A real image file, not an emoji — emoji page icons are rendered client-side
# from raw SVG <text> with no font specified, so they look inconsistent
# across browsers/OSes (confirmed by reading Streamlit's frontend source).
# This bitmap renders identically everywhere.
st.set_page_config(
    page_title="Optimus Analytics",
    page_icon=str(Path(__file__).parent / "favicon.png"),
    layout="wide",
)

# Matches .streamlit/config.toml's primaryColor, so charts read as part of
# the same theme instead of Plotly's unrelated default palette.
CHART_ACCENT_COLOR = "#0B5394"


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
@st.cache_data
def load_excel(file_bytes: bytes) -> dict[str, pd.DataFrame]:
    """Read every sheet of the uploaded Excel into a dict of DataFrames."""
    xls = pd.ExcelFile(io.BytesIO(file_bytes))
    return {name: xls.parse(name) for name in xls.sheet_names}


# Streamlit reruns the whole script on every interaction anywhere on the
# page — without caching, every single click would re-fire a network round
# trip to Neon, which also scales to zero when idle (a real cold-start
# delay on top of the network hop). Cached with a short TTL so the project/
# file lists still pick up changes reasonably quickly; explicitly cleared
# right after create_project/save_file below so a just-created project or
# file shows up immediately rather than waiting out the TTL.
@st.cache_data(ttl=30)
def cached_list_projects():
    return db.list_projects()


@st.cache_data(ttl=30)
def cached_list_files(project_id: int):
    return db.list_files(project_id)


def guess_columns(df: pd.DataFrame) -> dict[str, list[str]]:
    """Heuristically classify columns by shape alone (name + dtype +
    cardinality) — no knowledge of any specific Optimus form's schema, so
    this works the same regardless of which export form was uploaded."""
    date_cols, status_cols, category_cols, text_cols = [], [], [], []

    for col in df.columns:
        series = df[col]
        lower = str(col).lower()

        # Date detection: by name or by successful datetime parsing
        is_dateish = any(k in lower for k in ["date", "due", "closure", "raised", "time"])
        parsed_ok = False
        if pd.api.types.is_string_dtype(series) or is_dateish:
            try:
                parsed = pd.to_datetime(series, errors="coerce", dayfirst=True)
                parsed_ok = parsed.notna().mean() > 0.5
            except Exception:
                parsed_ok = False
        if is_dateish and parsed_ok:
            date_cols.append(col)
            continue

        # Status / workflow columns
        if any(k in lower for k in ["status", "workflow", "state", "stage"]):
            status_cols.append(col)
            continue

        # Low-cardinality columns are good "category" candidates for grouping
        nunique = series.nunique(dropna=True)
        if 1 < nunique <= max(30, len(df) * 0.5) and pd.api.types.is_string_dtype(series):
            category_cols.append(col)
        else:
            text_cols.append(col)

    return {
        "date": date_cols,
        "status": status_cols,
        "category": category_cols,
        "text": text_cols,
    }


def bar_of_counts(df, col, title, top_n=15):
    """Horizontal bar chart of value counts for a categorical column."""
    counts = df[col].value_counts(dropna=False).head(top_n).reset_index()
    counts.columns = [col, "Count"]
    fig = px.bar(
        counts, x="Count", y=col, orientation="h", title=title, text="Count",
        color_discrete_sequence=[CHART_ACCENT_COLOR],
    )
    fig.update_layout(yaxis={"categoryorder": "total ascending"}, height=400)
    return fig


def kpi_row(df, cols):
    c1, c2, c3 = st.columns(3)
    c1.metric("Total records", len(df), border=True)
    c2.metric("Columns", df.shape[1], border=True)
    c3.metric("Date fields detected", len(cols["date"]), border=True)


AI_SYSTEM_PROMPT = """You are an analyst reviewing data exported from \
Optimus (O2), a JTC infrastructure-project system. The export could be any \
one of several Optimus form types (quality defects, safety observations, \
or others) — infer what kind of form this is and what matters most from \
the data itself, since you aren't told in advance. You're given aggregated \
statistics and a sample of free-text notes — not the raw row data. \
Identify the most important patterns: recurring themes, risk \
concentrations, and any timing/workflow concerns visible in the data. \
Reference the actual categories and numbers given; do not invent data \
that isn't in the summary. Respond in markdown, under 400 words, with \
short headers and bullet points."""


def get_gemini_client() -> genai.Client | None:
    """Resolve an API key from Streamlit secrets (local secrets.toml or the
    Streamlit Cloud app's Settings -> Secrets) or the environment."""
    api_key = None
    try:
        api_key = st.secrets.get("GEMINI_API_KEY")
    except Exception:
        pass
    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    return genai.Client(api_key=api_key) if api_key else None


def build_data_summary(df: pd.DataFrame, cols: dict, classification: dict | None = None) -> str:
    """Aggregate the uploaded data into a compact text summary for the AI
    call — counts and samples only, never the full raw row dump. Built
    entirely from the generic column detection above, so it works the same
    regardless of which Optimus form type was uploaded.

    `classification` is the optional AI Classification result (see the
    "🏷️ AI Classification" tab) — if the user has already run it this
    session, its category counts are folded in here so AI Insights can
    reference them (e.g. "42% of observations were Housekeeping issues"),
    which the raw export alone often can't say (the Safety Observation
    form has no defect-type column at all). Insights doesn't require
    classification to have been run first — this is enrichment when
    available, not a hard dependency, since classification is a separate,
    much more expensive multi-call operation."""
    lines = [f"Total records: {len(df)}"]

    if classification is not None:
        counts = classification["labels"].value_counts().head(10)
        lines.append(
            f"\nAI-classified categories for '{classification['column']}' "
            "(generated by a separate classification step, not part of the "
            "original export):"
        )
        lines.append(counts.to_string())

    if cols["date"]:
        primary_date = pd.to_datetime(df[cols["date"][0]], errors="coerce", dayfirst=True).dropna()
        if not primary_date.empty:
            lines.append(f"Date range ({cols['date'][0]}): {primary_date.min().date()} to {primary_date.max().date()}")

    if cols["status"]:
        lines.append(f"\n{cols['status'][0]} counts:")
        lines.append(df[cols["status"][0]].value_counts().head(10).to_string())

    for cat_col in cols["category"][:5]:
        lines.append(f"\n{cat_col} counts (top 8):")
        lines.append(df[cat_col].value_counts().head(8).to_string())

    # Free text worth sampling has multiple words per value (observation
    # notes); IDs/serial numbers are single "words" with no spaces, so this
    # filters them out without hardcoding any column names.
    for text_col in cols["text"]:
        values = df[text_col].dropna().astype(str)
        if values.empty or values.str.split().str.len().mean() < 3:
            continue
        samples = values.str.slice(0, 200).unique()[:15]
        if len(samples):
            lines.append(f"\nSample of '{text_col}' (up to 15, truncated):")
            lines.extend(f"- {s}" for s in samples)

    return "\n".join(lines)


def generate_ai_insights(client: genai.Client, summary_text: str, max_attempts: int = 3) -> str:
    """Calls Gemini with retry-with-backoff for transient server overload —
    free-tier calls can return 503 UNAVAILABLE under demand spikes (observed
    in practice), or 429 RESOURCE_EXHAUSTED if a burst of calls trips the
    free-tier rate limit (also observed in practice, once the classification
    feature below started firing several batched calls in quick succession).
    A 429 gets a longer backoff than a 5xx, since rate-limit windows take
    longer to clear than transient overload. Any other ClientError (4xx,
    e.g. bad request/auth) won't be fixed by retrying, so it's raised
    immediately."""
    last_error = None
    for attempt in range(max_attempts):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=summary_text,
                config=genai_types.GenerateContentConfig(
                    system_instruction=AI_SYSTEM_PROMPT,
                    max_output_tokens=1024,
                    temperature=1.0,  # written analysis benefits from varied phrasing
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )
            if response.text:
                return response.text
            return (
                "_Gemini returned an empty response — this can happen if the "
                "data summary is unusually large. Try regenerating, or check "
                "the Raw Data tab for the underlying numbers._"
            )
        except genai.errors.ServerError as e:
            last_error = e
            if attempt < max_attempts - 1:
                time.sleep(2 ** (attempt + 1))  # 2s, 4s
        except genai.errors.ClientError as e:
            if e.code == 429 and attempt < max_attempts - 1:
                last_error = e
                time.sleep(15 * (attempt + 1))  # 15s, 30s
                continue
            raise
    raise last_error


def _call_gemini_json(client: genai.Client, prompt: str, max_output_tokens: int, max_attempts: int = 5):
    """Shared helper for the classification calls below: same
    retry-with-backoff behaviour as generate_ai_insights (including the
    429-specific longer backoff), but expecting a JSON response instead of
    markdown prose. Classification fires several batched calls back-to-back
    for one column, which is exactly the pattern that tripped the free-tier
    rate limit in testing — hence more attempts here than the single-call
    AI Insights tab."""
    last_error = None
    for attempt in range(max_attempts):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=max_output_tokens,
                    response_mime_type="application/json",
                    temperature=0.0,  # classification should be deterministic, not creative
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )
            return json.loads(response.text)
        except genai.errors.ServerError as e:
            last_error = e
            if attempt < max_attempts - 1:
                time.sleep(2 ** (attempt + 1))
        except genai.errors.ClientError as e:
            if e.code == 429 and attempt < max_attempts - 1:
                last_error = e
                time.sleep(15 * (attempt + 1))  # 15s, 30s, 45s, 60s
                continue
            raise
    raise last_error


def induce_taxonomy(client: genai.Client, samples: list[str], max_categories: int = 8) -> list[str]:
    """Propose a short, fixed set of categories from a sample of free-text
    values — induced from the data itself rather than a hardcoded list, so
    this works the same for any Optimus form's free-text field. A fixed
    taxonomy (used by every batch in classify_column below) keeps labels
    consistent across the whole column, instead of each batch inventing
    its own wording for the same underlying category."""
    prompt = (
        f"Here are {len(samples)} free-text entries from a JTC Optimus "
        "project export:\n\n" + "\n".join(f"- {s}" for s in samples) +
        f"\n\nPropose a fixed list of at most {max_categories} short category "
        "labels (2-4 words each) that would sensibly classify entries like "
        "these. Respond as a JSON array of strings only."
    )
    categories = _call_gemini_json(client, prompt, max_output_tokens=256)
    labels = [str(c) for c in categories][:max_categories]
    if "Other" not in labels:
        labels.append("Other")
    return labels


def classify_batch(client: genai.Client, texts: list[str], categories: list[str]) -> list[str]:
    """Classify a batch of free-text entries into one of `categories`.
    Returns one label per input text, same order/length as `texts`."""
    numbered = "\n".join(f"{i}: {t}" for i, t in enumerate(texts))
    prompt = (
        f"Categories: {', '.join(categories)}\n\n"
        "Classify each numbered entry below into exactly one of the "
        "categories above (use 'Other' if none fit).\n\n"
        f"{numbered}\n\n"
        'Respond as a JSON array of {"index": <int>, "category": <string>} '
        "objects, one per entry, in any order."
    )
    results = _call_gemini_json(client, prompt, max_output_tokens=2048)
    labels = ["Other"] * len(texts)
    for item in results:
        idx = item.get("index")
        if isinstance(idx, int) and 0 <= idx < len(texts):
            labels[idx] = str(item.get("category", "Other"))
    return labels


def classify_all(client: genai.Client, values: list[str], categories: list[str], batch_size: int = 25, progress_callback=None) -> list[str]:
    """Classify every value into one of `categories` — a taxonomy already
    decided (via induce_taxonomy, plus optional human review/editing in the
    UI) rather than induced fresh here, so the same reviewed category list
    is used consistently across every batch. Batches multiple rows per API
    call rather than one call per row, matching the app's existing
    free-tier cost-consciousness."""
    batches = [values[i:i + batch_size] for i in range(0, len(values), batch_size)]
    all_labels: list[str] = []
    for i, batch in enumerate(batches):
        all_labels.extend(classify_batch(client, batch, categories))
        if progress_callback:
            progress_callback((i + 1) / len(batches))
        # Small pause between batches to avoid tripping the free-tier rate
        # limit in the first place (observed in testing: firing several
        # batches back-to-back hit a 429), rather than only reacting to it
        # via _call_gemini_json's retry-with-backoff after the fact.
        if i < len(batches) - 1:
            time.sleep(3)
    return all_labels


# ----------------------------------------------------------------------
# Sidebar — project selection, upload, and database load/save
# ----------------------------------------------------------------------
# ---- Project/file management dialogs ----
# Real popup modals (st.dialog) instead of st.expander for these — an
# expander pushes everything below it down the page when opened, which
# felt heavy for occasional actions like rename/delete. A dialog floats
# over the page and doesn't disturb the surrounding layout at all.
@st.dialog("New project")
def new_project_dialog():
    new_name = st.text_input("Project name")
    new_desc = st.text_input("Description (optional)")
    if st.button("Create project", type="primary") and new_name.strip():
        db.create_project(new_name.strip(), new_desc.strip())
        cached_list_projects.clear()
        st.session_state["_pending_project_choice"] = new_name.strip()
        st.rerun()


@st.dialog("Rename project")
def rename_project_dialog(project: dict):
    new_name = st.text_input("Name", value=project["name"])
    new_desc = st.text_input("Description", value=project.get("description") or "")
    if st.button("Save changes", type="primary") and new_name.strip():
        db.rename_project(project["id"], new_name.strip(), new_desc.strip())
        cached_list_projects.clear()
        st.session_state["_pending_project_choice"] = new_name.strip()
        st.rerun()


@st.dialog("Delete project")
def delete_project_dialog(project: dict):
    st.warning(
        f"This permanently deletes \"{project['name']}\" and every "
        "file/analysis saved under it. This cannot be undone."
    )
    confirm_text = st.text_input(f"Type \"{project['name']}\" to confirm")
    if st.button("Delete project", type="primary"):
        if confirm_text == project["name"]:
            db.delete_project(project["id"])
            cached_list_projects.clear()
            st.session_state["_pending_project_choice"] = "(none)"
            st.session_state.pop("loaded_file", None)
            st.rerun()
        else:
            st.error("Typed name doesn't match — project not deleted.")


@st.dialog("Delete file")
def delete_file_dialog(file_row: dict):
    st.warning(f"Delete '{file_row['filename']}' and its saved analyses? This cannot be undone.")
    if st.button("Yes, delete", type="primary"):
        db.delete_file(file_row["id"])
        cached_list_files.clear()
        if st.session_state.get("db_file_id") == file_row["id"]:
            st.session_state.pop("db_file_id", None)
            st.session_state.pop("loaded_file", None)
        st.rerun()


st.sidebar.title("Optimus Analytics")

# Database features degrade gracefully — a missing/unreachable DATABASE_URL
# (e.g. local dev without .env set up) hides the project/save UI instead of
# crashing the app; upload-and-analyse-only still works exactly as before.
try:
    projects = cached_list_projects()
    db_available = True
except Exception:
    projects = []
    db_available = False

selected_project_id = None
if db_available:
    with st.sidebar.expander("📁 Project", expanded=True):
        project_options = {p["name"]: p["id"] for p in projects}
        all_choices = ["(none)"] + list(project_options.keys())

        # Use key= so Streamlit manages this widget's state natively,
        # instead of computing index= from a session_state snapshot read
        # before this rerun's click is processed into it — that pattern
        # caused a one-rerun lag where a click would visibly revert and
        # need a second click to stick. Programmatic jumps (e.g. to a
        # newly created/renamed project) go through _pending_project_choice
        # instead of writing "project_choice" directly, because Streamlit
        # raises if a keyed widget's session_state entry is written after
        # that widget has already been instantiated in the same run — this
        # indirection is applied here, before the selectbox below exists.
        if "_pending_project_choice" in st.session_state:
            st.session_state["project_choice"] = st.session_state.pop("_pending_project_choice")
        if st.session_state.get("project_choice") not in all_choices:
            st.session_state["project_choice"] = "(none)"
        choice = st.selectbox(
            "Select a project",
            all_choices,
            key="project_choice",
        )
        if choice != "(none)":
            selected_project_id = project_options[choice]

        if st.button("+ New project"):
            new_project_dialog()

st.sidebar.write("Upload an Excel export from O2 to begin.")
uploaded = st.sidebar.file_uploader("Excel file (.xlsx)", type=["xlsx", "xls"])

# A fresh upload always takes precedence over a previously loaded saved file.
if uploaded is not None:
    st.session_state.pop("loaded_file", None)
loaded_file_info = st.session_state.get("loaded_file")

st.title("Optimus Data Analytics Dashboard")

# ---- Manage: project rename/delete, saved-files browse/load/delete ----
# Lives in the main content area (not the sidebar) specifically so it has
# room to show a real per-file action list rather than cramped dropdowns —
# and so it's usable even before any file is uploaded/loaded this session.
if db_available and selected_project_id:
    current_project = next((p for p in projects if p["id"] == selected_project_id), None)
    with st.container(border=True):
        header_col, rename_col, delete_col = st.columns([4, 1, 1])
        header_col.subheader(f"📁 Manage: {current_project['name']}")
        if rename_col.button("✏️ Rename", key=f"open_rename_{selected_project_id}"):
            rename_project_dialog(current_project)
        if delete_col.button("🗑️ Delete", key=f"open_delete_proj_{selected_project_id}"):
            delete_project_dialog(current_project)

        st.divider()
        st.write("**Saved files**")
        saved_files = cached_list_files(selected_project_id)
        if not saved_files:
            st.caption("No files saved to this project yet.")
        for f in saved_files:
            fcol1, fcol2, fcol3, fcol4 = st.columns([3, 2, 1, 1])
            fcol1.write(f["filename"])
            fcol2.caption(f"{f['form_type']} · {f['uploaded_at']:%Y-%m-%d %H:%M}")
            if fcol3.button("Load", key=f"load_{f['id']}"):
                st.session_state["loaded_file"] = {
                    "file_id": f["id"],
                    "filename": f["filename"],
                    "detected_columns": f["detected_columns"],
                }
                st.rerun()
            if fcol4.button("🗑️", key=f"delete_btn_{f['id']}"):
                delete_file_dialog(f)

if uploaded is None and loaded_file_info is None:
    st.stop()

if loaded_file_info is not None:
    df = db.load_file_records(loaded_file_info["file_id"])
    cols = loaded_file_info["detected_columns"]
    filename = loaded_file_info["filename"]
    data_identity = ("db_file", loaded_file_info["file_id"])
    # Records round-trip through JSONB as plain ISO-format date strings, not
    # a real datetime dtype. Parse them back explicitly here rather than
    # relying on the dayfirst=True parsing used elsewhere for fresh Excel
    # uploads (tuned for ambiguous DD/MM/YYYY exports) — dayfirst=True
    # actively corrupts unambiguous ISO strings: confirmed it silently turns
    # "2026-02-01" into "2026-01-02" and drops "2026-01-15" to NaT entirely.
    for date_col in cols.get("date", []):
        if date_col in df.columns:
            df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
else:
    try:
        sheets = load_excel(uploaded.read())
    except Exception:
        st.error(
            "Couldn't read that file as an Excel workbook. Make sure it's a "
            "valid, uncorrupted `.xlsx`/`.xls` export from Optimus, then "
            "re-upload it."
        )
        st.stop()

    if not sheets or all(s.empty for s in sheets.values()):
        st.warning("This file has no data in any sheet — nothing to analyse.")
        st.stop()

    sheet_name = (
        st.sidebar.selectbox("Sheet", list(sheets.keys()))
        if len(sheets) > 1
        else list(sheets.keys())[0]
    )
    df = sheets[sheet_name].copy()
    if df.empty:
        st.warning(f"Sheet '{sheet_name}' has no rows — nothing to analyse.")
        st.stop()
    df.columns = [str(c).strip() for c in df.columns]
    cols = guess_columns(df)
    filename = uploaded.name
    data_identity = ("upload", uploaded.name, uploaded.size)

# AI Insights/Classification are tied to whatever data was last analysed —
# clear them whenever the underlying data changes (new upload or a
# different saved file loaded) so a stale analysis never lingers on screen.
if st.session_state.get("data_identity") != data_identity:
    st.session_state.pop("ai_insights", None)
    st.session_state.pop("taxonomy", None)
    st.session_state.pop("classification", None)
    st.session_state.pop("db_file_id", None)
    st.session_state["data_identity"] = data_identity
    if loaded_file_info is not None:
        st.session_state["db_file_id"] = loaded_file_info["file_id"]
        # A file loaded from the database may already have a saved
        # analysis from a previous session — restore it so the tabs show
        # what was generated before instead of coming up blank every time.
        saved_insights = db.get_latest_analysis(loaded_file_info["file_id"], "insights")
        if saved_insights is not None:
            st.session_state["ai_insights"] = saved_insights["text"]
        saved_classification = db.get_latest_analysis(loaded_file_info["file_id"], "classification")
        if saved_classification is not None:
            st.session_state["classification"] = {
                "column": saved_classification["column"],
                "categories": saved_classification["categories"],
                "labels": pd.Series(saved_classification["labels"], index=df.index),
            }

if db_available and selected_project_id and data_identity[0] == "upload" and "db_file_id" not in st.session_state:
    with st.sidebar.expander("💾 Save to database"):
        form_type = st.text_input("Form type / label", value=Path(filename).stem)
        if st.button("Save this file to the selected project"):
            file_id = db.save_file(selected_project_id, form_type, filename, cols, df)
            cached_list_files.clear()
            st.session_state["db_file_id"] = file_id
            st.toast(f"Saved to database (file id {file_id}).", icon="✅")
            # Rerun so the Manage section above (already rendered earlier
            # in this same script pass, from stale cached data) picks up
            # the newly saved file immediately instead of on the next
            # unrelated interaction. st.toast survives this one rerun.
            st.rerun()

if db_available and selected_project_id:
    project_name = next((p["name"] for p in projects if p["id"] == selected_project_id), "?")
    st.caption(f"📁 Project: **{project_name}** · 📄 File: **{filename}**")
elif filename:
    st.caption(f"📄 File: **{filename}**")

kpi_row(df, cols)
if cols["date"]:
    primary_date = pd.to_datetime(df[cols["date"][0]], errors="coerce", dayfirst=True).dropna()
    if not primary_date.empty:
        st.caption(f"Date range ({cols['date'][0]}): {primary_date.min().date()} to {primary_date.max().date()}")

# Optional global filter on the first detected category column
if cols["category"]:
    with st.sidebar:
        st.subheader("Filter")
        filt_col = st.selectbox("Filter by", ["(none)"] + cols["category"])
        if filt_col != "(none)":
            choices = st.multiselect(
                f"{filt_col} values",
                sorted(df[filt_col].dropna().astype(str).unique()),
            )
            if choices:
                df = df[df[filt_col].astype(str).isin(choices)]

# ----------------------------------------------------------------------
# Tabs
# ----------------------------------------------------------------------
tab_overview, tab_ai, tab_classify, tab_raw = st.tabs(
    ["📊 Overview", "🤖 AI Insights", "🏷️ AI Classification", "🗂 Raw Data"]
)

# ---- Overview: generic aggregate counts, no per-form business logic ----
with tab_overview:
    st.subheader("Data Overview")
    st.caption(
        "Plain frequency counts from whatever status/category columns were "
        "detected — no form-specific formulas. For actual interpretation, "
        "see the AI Insights tab."
    )
    if cols["status"]:
        with st.container(border=True):
            st.plotly_chart(
                bar_of_counts(df, cols["status"][0], f"Records by {cols['status'][0]}"),
                width="stretch",
            )
    if cols["category"]:
        for cat_col in cols["category"][:5]:
            with st.container(border=True):
                st.plotly_chart(
                    bar_of_counts(df, cat_col, f"Counts by {cat_col}"),
                    width="stretch",
                )
    if not cols["status"] and not cols["category"]:
        st.caption("No status or categorical columns detected for an overview.")

# ---- AI Insights: Gemini-generated summary of the aggregated data ----
with tab_ai:
    st.subheader("AI Insights")
    client = get_gemini_client()
    if client is None:
        st.info(
            "No Gemini API key found. Add `GEMINI_API_KEY` to "
            "`.streamlit/secrets.toml` locally, or under the app's "
            "Settings -> Secrets on Streamlit Cloud, to enable this tab."
        )
    elif "ai_insights" in st.session_state:
        # An insight already exists (freshly generated, or restored from a
        # saved file) — show it and require an explicit delete before a
        # new one can be generated, rather than silently overwriting or
        # piling up duplicate saved analyses for the same file.
        st.markdown(st.session_state["ai_insights"])
        st.download_button(
            "Download AI Insights (Markdown)",
            st.session_state["ai_insights"].encode("utf-8"),
            "ai_insights.md",
            "text/markdown",
        )
        if st.button("🗑 Delete this insight"):
            st.session_state.pop("ai_insights", None)
            if "db_file_id" in st.session_state:
                db.delete_analyses(st.session_state["db_file_id"], "insights")
            st.rerun()
    else:
        classification = st.session_state.get("classification")
        if classification:
            st.caption(
                "Sends aggregated counts, a small sample of free-text notes, "
                f"and the AI-classified '{classification['column']}' "
                "categories (never the full raw data) to Gemini for a "
                "written summary."
            )
        else:
            st.caption(
                "Sends aggregated counts and a small sample of free-text notes "
                "(never the full raw data) to Gemini for a written summary. "
                "Run AI Classification first (see that tab) to also fold "
                "AI-derived categories into this summary."
            )
        if st.button("Generate AI Insights"):
            with st.spinner("Asking Gemini..."):
                summary_text = build_data_summary(df, cols, classification)
                try:
                    st.session_state["ai_insights"] = generate_ai_insights(client, summary_text)
                    if "db_file_id" in st.session_state:
                        db.save_analysis(st.session_state["db_file_id"], "insights", st.session_state["ai_insights"])
                    st.rerun()
                except genai.errors.APIError as e:
                    st.error(f"AI request failed: {e}")

# ---- AI Classification: per-row categorisation of a free-text column ----
# Distinct from AI Insights above: that tab summarises the data in prose.
# This one generates new structured data (a category per row) that may not
# exist anywhere in the original export — e.g. the Safety Observation form
# has no defect-type/severity column at all, only free-text notes.
with tab_classify:
    st.subheader("AI Classification")
    client = get_gemini_client()
    if client is None:
        st.info(
            "No Gemini API key found. Add `GEMINI_API_KEY` to "
            "`.streamlit/secrets.toml` locally, or under the app's "
            "Settings -> Secrets on Streamlit Cloud, to enable this tab."
        )
    else:
        existing_classification = st.session_state.get("classification")
        if existing_classification:
            # A classification already exists (freshly generated, or
            # restored from a saved file) — show it and require an
            # explicit delete before classifying again, whether that's a
            # re-run of the same column or a switch to a different one.
            labeled_df = df.copy()
            labeled_df["AI Category"] = existing_classification["labels"]
            st.caption(
                f"Classified column: **{existing_classification['column']}**. "
                "Categories Gemini proposed: "
                + ", ".join(existing_classification["categories"])
            )
            with st.container(border=True):
                st.plotly_chart(
                    bar_of_counts(
                        labeled_df, "AI Category",
                        f"AI-classified categories for '{existing_classification['column']}'",
                    ),
                    width="stretch",
                )
            st.download_button(
                "Download classified data (CSV)",
                labeled_df.to_csv(index=False).encode("utf-8"),
                "optimus_classified.csv",
                "text/csv",
            )
            if st.button("🗑 Delete this classification"):
                st.session_state.pop("classification", None)
                st.session_state.pop("taxonomy", None)
                if "db_file_id" in st.session_state:
                    db.delete_analyses(st.session_state["db_file_id"], "classification")
                st.rerun()
        else:
            # Same "genuine free text, not IDs/serials" heuristic already used
            # for AI Insights sampling — average of 3+ words per value.
            eligible_cols = [
                col for col in cols["text"]
                if not df[col].dropna().empty
                and df[col].dropna().astype(str).str.split().str.len().mean() >= 3
            ]
            if not eligible_cols:
                st.caption("No free-text columns detected to classify.")
            else:
                target_col = st.selectbox("Column to classify", eligible_cols)
                st.caption(
                    "Step 1: Gemini proposes a short set of categories from a "
                    "sample of this column — no predefined list, so this works "
                    "for any Optimus form's free-text field. Review/edit them, "
                    "then run classification against the confirmed list."
                )

                if st.button("1. Propose categories"):
                    values = df[target_col].dropna().astype(str)
                    with st.spinner("Asking Gemini to propose categories..."):
                        try:
                            sample_size = min(30, len(values))
                            sample = values.sample(sample_size, random_state=0).tolist()
                            st.session_state["taxonomy"] = {
                                "column": target_col,
                                "categories": induce_taxonomy(client, sample),
                            }
                        except (genai.errors.APIError, json.JSONDecodeError) as e:
                            st.error(f"Category proposal failed: {e}")

                taxonomy = st.session_state.get("taxonomy")
                if taxonomy and taxonomy["column"] == target_col:
                    st.write("**Proposed categories** — edit below if needed, one per line:")
                    edited = st.text_area(
                        "Categories", value="\n".join(taxonomy["categories"]),
                        height=160, label_visibility="collapsed",
                    )
                    confirmed_categories = [c.strip() for c in edited.split("\n") if c.strip()]

                    if st.button("2. Run classification with these categories"):
                        values = df[target_col].dropna().astype(str)
                        progress = st.progress(0.0)
                        with st.spinner("Classifying with Gemini..."):
                            try:
                                labels = classify_all(
                                    client, values.tolist(), confirmed_categories,
                                    progress_callback=progress.progress,
                                )
                                result = pd.Series("(no text)", index=df.index)
                                result.loc[values.index] = labels
                                st.session_state["classification"] = {
                                    "column": target_col,
                                    "labels": result,
                                    "categories": confirmed_categories,
                                }
                                st.session_state.pop("taxonomy", None)
                                if "db_file_id" in st.session_state:
                                    db.save_analysis(st.session_state["db_file_id"], "classification", {
                                        "column": target_col,
                                        "categories": confirmed_categories,
                                        "labels": result.tolist(),
                                    })
                                progress.empty()
                                st.rerun()
                            except (genai.errors.APIError, json.JSONDecodeError) as e:
                                progress.empty()
                                st.error(f"AI classification failed: {e}")

# ---- Raw data + detected schema ----
with tab_raw:
    st.subheader("Raw Data")
    with st.expander("Detected column types"):
        st.json(cols)
    st.dataframe(df, width="stretch")
    st.download_button(
        "Download filtered data (CSV)",
        df.to_csv(index=False).encode("utf-8"),
        "optimus_filtered.csv",
        "text/csv",
    )
