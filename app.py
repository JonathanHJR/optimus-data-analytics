"""
Optimus Data Analytics Dashboard
---------------------------------
A starter Streamlit app for analysing Quality/Defects (and similar) data
exported from the Optimus (O2) platform as Excel files.

Designed to adapt to whatever columns your export actually contains, so it
works even before you've confirmed exact column names. Four domain-team tabs:
Project Management, Cost (Contract Mgt), Safety, and Quality.

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

# Matches .streamlit/config.toml's primaryColor, so bar/line charts read as
# part of the same theme instead of Plotly's unrelated default palette.
CHART_ACCENT_COLOR = "#0B5394"

# ----------------------------------------------------------------------
# Confirmed Optimus "Quality Defects Inspection Form" export schema.
# Trade-specific column pairs are mutually exclusive per row (only the
# pair matching that row's Trade is populated), so we coalesce each
# group into a single unified column the tabs can group/filter on.
# ----------------------------------------------------------------------
RAISED_DATE_COL = "Date of Inspection"
DUE_DATE_COL = "Due Date for Inspection Closure"
WORKFLOW_COL = "Workflow"
MODIFIED_TIME_COL = "Modification time"

QUALITY_TAXONOMY = {
    "System": ["Architectural Systems", "Electrical Systems", "Mechanical Systems", "C&S Systems"],
    "Component": ["Architectural Components", "Electrical Components", "Mechanical Components", "C&S Components"],
    "Quality Inspection Check Item": [
        "Quality Inspection Check Item (Archi/ C&S)", "Quality Inspection Check Item (M)",
    ],
    "Type of Inspection Check": [
        "Type of Inspection Check (Archi/ C&S)", "Type of Inspection Check (M&E)",
    ],
}

# Workflow values containing any of these are treated as a terminal/closed
# state for aging purposes (no real closure-date column exists in the
# export, so Modification time is used as the closure timestamp instead).
# Revisit once a fuller export shows the full set of Workflow values.
CLOSED_WORKFLOW_KEYWORDS = ["approved", "closed", "complete", "rejected"]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
@st.cache_data
def load_excel(file_bytes: bytes) -> dict[str, pd.DataFrame]:
    """Read every sheet of the uploaded Excel into a dict of DataFrames."""
    xls = pd.ExcelFile(io.BytesIO(file_bytes))
    return {name: xls.parse(name) for name in xls.sheet_names}


def guess_columns(df: pd.DataFrame) -> dict[str, list[str]]:
    """Heuristically classify columns so the app can build charts without
    knowing the exact Optimus schema in advance."""
    date_cols, status_cols, category_cols, text_cols = [], [], [], []

    for col in df.columns:
        series = df[col]
        lower = str(col).lower()

        # Date detection: by name or by successful datetime parsing
        is_dateish = any(k in lower for k in ["date", "due", "closure", "raised", "time"])
        parsed_ok = False
        if series.dtype == "object" or is_dateish:
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
        if 1 < nunique <= max(30, len(df) * 0.5) and series.dtype == "object":
            category_cols.append(col)
        else:
            text_cols.append(col)

    return {
        "date": date_cols,
        "status": status_cols,
        "category": category_cols,
        "text": text_cols,
    }


def consolidate_taxonomy(df: pd.DataFrame) -> pd.DataFrame:
    """Coalesce the trade-specific column groups (Architectural/Electrical/
    Mechanical/C&S System+Component pairs etc.) into single unified columns,
    so charts can group by one consistent field regardless of trade."""
    for new_name, source_cols in QUALITY_TAXONOMY.items():
        present = [c for c in source_cols if c in df.columns]
        if present:
            df[new_name] = df[present].bfill(axis=1).iloc[:, 0]
    return df


def compute_aging(df: pd.DataFrame) -> pd.DataFrame:
    """Open-duration tracking using Workflow status as the closure signal:
    closed items are aged raised -> Modification time, open items are aged
    raised -> now. Overdue = still open and past Due Date for Inspection
    Closure."""
    raised = pd.to_datetime(df[RAISED_DATE_COL], errors="coerce", dayfirst=True)
    due = pd.to_datetime(df[DUE_DATE_COL], errors="coerce", dayfirst=True)
    modified = pd.to_datetime(df[MODIFIED_TIME_COL], errors="coerce", dayfirst=True)
    now = pd.Timestamp.now()

    is_closed = df[WORKFLOW_COL].astype(str).str.lower().str.contains(
        "|".join(CLOSED_WORKFLOW_KEYWORDS), na=False
    )
    end = modified.where(is_closed, now)
    age_days = (end - raised).dt.days
    is_overdue = (~is_closed) & due.notna() & (now > due)

    return pd.DataFrame({"Age (days)": age_days, "Is Closed": is_closed, "Is Overdue": is_overdue})


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


AI_SYSTEM_PROMPT = """You are a construction quality/safety analyst reviewing \
inspection data exported from Optimus (O2), a JTC infrastructure-project \
system. You're given aggregated statistics and a sample of free-text \
observation notes — not the raw row data. Identify the most important \
patterns: recurring root causes, risk concentrations, and any aging/overdue \
concerns. Reference the actual categories and numbers given; do not invent \
data that isn't in the summary. Respond in markdown, under 400 words, with \
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


def build_data_summary(df: pd.DataFrame, cols: dict, has_aging: bool) -> str:
    """Aggregate the uploaded data into a compact text summary for the AI
    call — counts and samples only, never the full raw row dump."""
    lines = [f"Total records: {len(df)}"]

    if RAISED_DATE_COL in df.columns:
        raised = pd.to_datetime(df[RAISED_DATE_COL], errors="coerce", dayfirst=True).dropna()
        if not raised.empty:
            lines.append(f"Date range ({RAISED_DATE_COL}): {raised.min().date()} to {raised.max().date()}")

    if has_aging:
        aging = compute_aging(df)
        valid_age = aging["Age (days)"].dropna()
        if not valid_age.empty:
            lines.append(
                f"Aging: median {int(valid_age.median())} days, "
                f"{int(aging['Is Closed'].sum())} closed, "
                f"{int(aging['Is Overdue'].sum())} open & overdue"
            )

    if cols["status"]:
        lines.append(f"\n{cols['status'][0]} counts:")
        lines.append(df[cols["status"][0]].value_counts().head(10).to_string())

    for cat_col in cols["category"][:5]:
        lines.append(f"\n{cat_col} counts (top 8):")
        lines.append(df[cat_col].value_counts().head(8).to_string())

    text_cols = [c for c in ["Observation & Comments", "Recommendations and Remarks, where required"] if c in df.columns]
    for text_col in text_cols:
        samples = df[text_col].dropna().astype(str).str.slice(0, 200).unique()[:15]
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
        "This starter app auto-detects date, status, and category columns and "
        "builds a tab for each domain team. Once you confirm your real column "
        "names, the charts can be tuned to exact fields (e.g. *Due Date for "
        "Inspection Closure*, *Defect Code*, *Trade*)."
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
df = consolidate_taxonomy(df)
cols = guess_columns(df)
for taxonomy_col in QUALITY_TAXONOMY:
    if taxonomy_col in df.columns and taxonomy_col not in cols["category"]:
        cols["category"].append(taxonomy_col)
        if taxonomy_col in cols["text"]:
            cols["text"].remove(taxonomy_col)

has_exact_aging_cols = all(
    c in df.columns for c in [RAISED_DATE_COL, DUE_DATE_COL, WORKFLOW_COL, MODIFIED_TIME_COL]
)

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
# Tabs — one per domain team
# ----------------------------------------------------------------------
tab_pm, tab_cost, tab_safety, tab_quality, tab_ai, tab_raw = st.tabs(
    ["📋 Project Mgt", "💰 Cost", "⚠️ Safety", "✅ Quality", "🤖 AI Insights", "🗂 Raw Data"]
)

# ---- Project Management: aging & workflow bottlenecks ----
with tab_pm:
    st.subheader("Project Management — Aging & Workflow")
    if cols["status"]:
        st.plotly_chart(
            bar_of_counts(df, cols["status"][0], f"Records by {cols['status'][0]}"),
            use_container_width=True,
        )
    if has_exact_aging_cols:
        aging = compute_aging(df)
        valid_age = aging["Age (days)"].dropna()
        if not valid_age.empty:
            fig = px.histogram(
                aging.dropna(subset=["Age (days)"]), x="Age (days)", nbins=20,
                title="Aging distribution (raised → closed, or raised → now if open)",
                color_discrete_sequence=[CHART_ACCENT_COLOR],
            )
            st.plotly_chart(fig, use_container_width=True)
            m1, m2 = st.columns(2)
            m1.metric("Median age (days)", int(valid_age.median()))
            m2.metric("Overdue (open & past due)", int(aging["Is Overdue"].sum()))
    elif len(cols["date"]) >= 1:
        date_col = st.selectbox("Date column for aging", cols["date"], key="pm_date")
        d = pd.to_datetime(df[date_col], errors="coerce", dayfirst=True)
        age_days = (pd.Timestamp.now().normalize() - d).dt.days
        age_df = pd.DataFrame({"Age (days)": age_days.dropna()})
        if not age_df.empty:
            fig = px.histogram(age_df, x="Age (days)", nbins=20,
                               title="Aging distribution (days since date)",
                               color_discrete_sequence=[CHART_ACCENT_COLOR])
            st.plotly_chart(fig, use_container_width=True)
            st.metric("Median age (days)", int(age_df["Age (days)"].median()))
    else:
        st.caption("No date columns detected for aging analysis.")

# ---- Cost: recurring defects -> cost impact proxy ----
with tab_cost:
    st.subheader("Cost — Recurring Issues (cost-impact proxy)")
    st.caption(
        "Frequency of recurring observations/locations is a proxy for "
        "rectification effort and potential variation-order cost."
    )
    if cols["category"]:
        default_idx = cols["category"].index("Component") if "Component" in cols["category"] else 0
        cost_col = st.selectbox("Group by", cols["category"], index=default_idx, key="cost_cat")
        st.plotly_chart(
            bar_of_counts(df, cost_col, f"Recurring counts by {cost_col}"),
            use_container_width=True,
        )
    else:
        st.caption("No categorical columns detected.")

# ---- Safety: risk heatmap across two categories ----
with tab_safety:
    st.subheader("Safety — Risk Heatmap")
    if len(cols["category"]) >= 2:
        c1, c2 = st.columns(2)
        row_dim = c1.selectbox("Rows", cols["category"], key="safe_row")
        col_dim = c2.selectbox(
            "Columns",
            [c for c in cols["category"] if c != row_dim],
            key="safe_col",
        )
        pivot = pd.crosstab(df[row_dim], df[col_dim])
        fig = px.imshow(
            pivot, text_auto=True, aspect="auto",
            title=f"{row_dim} vs {col_dim}", color_continuous_scale="Reds",
        )
        fig.update_layout(height=500)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.caption("Need at least two categorical columns for a heatmap.")

# ---- Quality: defect-code frequency / root cause ----
with tab_quality:
    st.subheader("Quality — Defect Frequency / Root Cause")
    if cols["category"]:
        default_idx = cols["category"].index("Component") if "Component" in cols["category"] else 0
        q_col = st.selectbox("Defect/check column", cols["category"], index=default_idx, key="q_cat")
        st.plotly_chart(
            bar_of_counts(df, q_col, f"Frequency by {q_col}"),
            use_container_width=True,
        )
        # Pareto view
        counts = df[q_col].value_counts().reset_index()
        counts.columns = [q_col, "Count"]
        counts["Cumulative %"] = (
            counts["Count"].cumsum() / counts["Count"].sum() * 100
        )
        fig = px.line(counts, x=q_col, y="Cumulative %", markers=True,
                      title="Pareto (cumulative %)",
                      color_discrete_sequence=[CHART_ACCENT_COLOR])
        fig.add_hline(y=80, line_dash="dash", line_color="red")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.caption("No categorical columns detected.")

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
                summary_text = build_data_summary(df, cols, has_exact_aging_cols)
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
