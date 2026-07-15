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
import psycopg2
import streamlit as st
import plotly.express as px
from google import genai
from google.genai import types as genai_types

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


def get_database_url() -> str | None:
    """Resolve the Neon connection string the same way as the Gemini API
    key above: Streamlit secrets first, then the environment."""
    url = None
    try:
        url = st.secrets.get("DATABASE_URL")
    except Exception:
        pass
    return url or os.environ.get("DATABASE_URL")


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
# Sidebar — upload
# ----------------------------------------------------------------------
st.sidebar.title("Optimus Analytics")
st.sidebar.write("Upload an Excel export from O2 to begin.")
uploaded = st.sidebar.file_uploader("Excel file (.xlsx)", type=["xlsx", "xls"])

# TEMPORARY — verifying Airbase's network can reach Neon before building any
# real database feature on top of it. Remove once confirmed either way.
with st.sidebar.expander("🔌 Database connectivity test"):
    if st.button("Test Neon connection"):
        db_url = get_database_url()
        if not db_url:
            st.error("No DATABASE_URL found in secrets or environment.")
        else:
            try:
                conn = psycopg2.connect(db_url, connect_timeout=10)
                cur = conn.cursor()
                cur.execute("SELECT 1")
                result = cur.fetchone()[0]
                conn.close()
                st.success(f"Connected — SELECT 1 returned {result}")
            except Exception as e:
                st.error(f"Connection failed: {e}")

if uploaded is None:
    st.title("Optimus Data Analytics Dashboard")
    st.info(
        "👈 Upload an Optimus Excel export in the sidebar to get started.\n\n"
        "Works with any Optimus export form — this app auto-detects date, "
        "status, and category columns generically and shows a quick "
        "aggregate overview, then lets AI Insights do the actual "
        "interpretation."
    )
    st.stop()

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

# AI Insights is tied to whatever file was last analysed — clear it on a new
# upload so a stale analysis from a previous file never lingers on screen.
upload_identity = (uploaded.name, uploaded.size)
if st.session_state.get("upload_identity") != upload_identity:
    st.session_state.pop("ai_insights", None)
    st.session_state.pop("taxonomy", None)
    st.session_state.pop("classification", None)
    st.session_state["upload_identity"] = upload_identity

st.title("Optimus Data Analytics Dashboard")
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
                except genai.errors.APIError as e:
                    st.error(f"AI request failed: {e}")
        if "ai_insights" in st.session_state:
            st.markdown(st.session_state["ai_insights"])
            st.download_button(
                "Download AI Insights (Markdown)",
                st.session_state["ai_insights"].encode("utf-8"),
                "ai_insights.md",
                "text/markdown",
            )

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
                        # A fresh taxonomy invalidates any previous run's
                        # classification, since labels were assigned against
                        # the old category list.
                        st.session_state.pop("classification", None)
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
                        except (genai.errors.APIError, json.JSONDecodeError) as e:
                            st.error(f"AI classification failed: {e}")
                    progress.empty()

            classification = st.session_state.get("classification")
            if classification and classification["column"] == target_col:
                labeled_df = df.copy()
                labeled_df["AI Category"] = classification["labels"]
                st.caption(
                    "Categories Gemini proposed: " + ", ".join(classification["categories"])
                )
                with st.container(border=True):
                    st.plotly_chart(
                        bar_of_counts(
                            labeled_df, "AI Category",
                            f"AI-classified categories for '{target_col}'",
                        ),
                        width="stretch",
                    )
                st.download_button(
                    "Download classified data (CSV)",
                    labeled_df.to_csv(index=False).encode("utf-8"),
                    "optimus_classified.csv",
                    "text/csv",
                )

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
