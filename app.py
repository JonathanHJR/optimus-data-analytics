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
import os
import pandas as pd
import streamlit as st
import plotly.express as px
from google import genai
from google.genai import types as genai_types

# Verify this against the model picker at aistudio.google.com if AI Insights
# calls start failing — free-tier model names/availability change over time.
GEMINI_MODEL = "gemini-2.5-flash"

st.set_page_config(page_title="Optimus Analytics", page_icon="🏗️", layout="wide")

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
    c1.metric("Total records", len(df))
    c2.metric("Columns", df.shape[1])
    c3.metric("Date fields detected", len(cols["date"]))


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


def build_data_summary(df: pd.DataFrame, cols: dict) -> str:
    """Aggregate the uploaded data into a compact text summary for the AI
    call — counts and samples only, never the full raw row dump. Built
    entirely from the generic column detection above, so it works the same
    regardless of which Optimus form type was uploaded."""
    lines = [f"Total records: {len(df)}"]

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


def generate_ai_insights(client: genai.Client, summary_text: str) -> str:
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=summary_text,
        config=genai_types.GenerateContentConfig(
            system_instruction=AI_SYSTEM_PROMPT,
            max_output_tokens=1024,
            thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        ),
    )
    return response.text


# ----------------------------------------------------------------------
# Sidebar — upload
# ----------------------------------------------------------------------
st.sidebar.title("Optimus Analytics")
st.sidebar.write("Upload an Excel export from O2 to begin.")
uploaded = st.sidebar.file_uploader("Excel file (.xlsx)", type=["xlsx", "xls"])

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

st.title("Optimus Data Analytics Dashboard")
kpi_row(df, cols)

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
tab_overview, tab_ai, tab_raw = st.tabs(["📊 Overview", "🤖 AI Insights", "🗂 Raw Data"])

# ---- Overview: generic aggregate counts, no per-form business logic ----
with tab_overview:
    st.subheader("Data Overview")
    st.caption(
        "Plain frequency counts from whatever status/category columns were "
        "detected — no form-specific formulas. For actual interpretation, "
        "see the AI Insights tab."
    )
    if cols["status"]:
        st.plotly_chart(
            bar_of_counts(df, cols["status"][0], f"Records by {cols['status'][0]}"),
            use_container_width=True,
        )
    if cols["category"]:
        for cat_col in cols["category"][:5]:
            st.plotly_chart(
                bar_of_counts(df, cat_col, f"Counts by {cat_col}"),
                use_container_width=True,
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
        st.caption(
            "Sends aggregated counts and a small sample of free-text notes "
            "(never the full raw data) to Gemini for a written summary."
        )
        if st.button("Generate AI Insights"):
            with st.spinner("Asking Gemini..."):
                summary_text = build_data_summary(df, cols)
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

# ---- Raw data + detected schema ----
with tab_raw:
    st.subheader("Raw Data")
    with st.expander("Detected column types"):
        st.json(cols)
    st.dataframe(df, use_container_width=True)
    st.download_button(
        "Download filtered data (CSV)",
        df.to_csv(index=False).encode("utf-8"),
        "optimus_filtered.csv",
        "text/csv",
    )
