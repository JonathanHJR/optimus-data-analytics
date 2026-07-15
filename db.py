"""Database layer for O2 Data Analytics — Neon Postgres.

Hybrid relational + JSONB schema (see schema.sql / CLAUDE.md's "Planned:
database integration" section for the full reasoning): Projects/Files are
normal typed columns, Records.data is JSONB since Optimus form types have
completely different, unrelated column sets — a fully normalized
table-per-form-type schema would force a migration every time a new form
type appears.
"""

import json
import os

import pandas as pd
import psycopg2
import psycopg2.extras


def get_database_url() -> str | None:
    """Resolve the Neon connection string from Streamlit secrets (if
    running under Streamlit) or the environment."""
    try:
        import streamlit as st
        url = st.secrets.get("DATABASE_URL")
        if url:
            return url
    except Exception:
        pass
    return os.environ.get("DATABASE_URL")


def get_connection():
    db_url = get_database_url()
    if not db_url:
        raise RuntimeError("No DATABASE_URL found in secrets or environment.")
    return psycopg2.connect(db_url)


def create_project(name: str, description: str = "") -> int:
    """Insert a new project, return its id."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (name, description) VALUES (%s, %s) RETURNING id",
            (name, description),
        )
        project_id = cur.fetchone()[0]
        conn.commit()
        return project_id


def list_projects() -> list[dict]:
    with get_connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT id, name, description, created_at FROM projects ORDER BY created_at DESC")
        return [dict(r) for r in cur.fetchall()]


def save_file(project_id: int, form_type: str, filename: str, detected_columns: dict, df: pd.DataFrame) -> int:
    """Insert a file record plus every row of `df` as a Record. Returns the
    new file's id.

    Uses df.to_json() (not a manual dict conversion) to serialize rows:
    pandas' own JSON encoder correctly handles numpy types (int64, float64)
    and datetime/Timestamp/NaT values that json.dumps() can't handle
    directly — a naive df.to_dict() + json.dumps() would raise a
    TypeError the moment a row contains a real number or date, which is
    every real Optimus export."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO files (project_id, form_type, filename, detected_columns) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (project_id, form_type, filename, json.dumps(detected_columns)),
        )
        file_id = cur.fetchone()[0]

        records = json.loads(df.to_json(orient="records", date_format="iso"))
        rows = [(file_id, idx, json.dumps(record)) for idx, record in enumerate(records)]
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO records (file_id, row_index, data) VALUES %s",
            rows,
            template="(%s, %s, %s::jsonb)",
        )
        conn.commit()
        return file_id


def load_file_records(file_id: int) -> pd.DataFrame:
    """Reconstruct a DataFrame from a file's stored records, in original
    row order."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT data FROM records WHERE file_id = %s ORDER BY row_index",
            (file_id,),
        )
        rows = [r[0] for r in cur.fetchall()]
    return pd.DataFrame(rows)


def save_analysis(file_id: int, analysis_type: str, result) -> int:
    """Insert an analysis result (AI Insights text or Classification data).
    `result` is stored as JSONB — plain strings get wrapped in {"text": ...}
    so both shapes round-trip through the same JSONB column cleanly."""
    if analysis_type not in ("insights", "classification"):
        raise ValueError(f"Unknown analysis type: {analysis_type!r}")
    payload = result if isinstance(result, dict) else {"text": result}
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analyses (file_id, type, result) VALUES (%s, %s, %s) RETURNING id",
            (file_id, analysis_type, json.dumps(payload)),
        )
        analysis_id = cur.fetchone()[0]
        conn.commit()
        return analysis_id


def list_files(project_id: int) -> list[dict]:
    with get_connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, form_type, filename, detected_columns, uploaded_at "
            "FROM files WHERE project_id = %s ORDER BY uploaded_at DESC",
            (project_id,),
        )
        return [dict(r) for r in cur.fetchall()]
