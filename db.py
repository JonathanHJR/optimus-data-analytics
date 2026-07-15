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


def get_latest_analysis(file_id: int, analysis_type: str) -> dict | list | None:
    """Return the most recent saved analysis result of this type for a
    file, or None if none exists yet. Callers unwrap the shape themselves:
    "insights" results are {"text": ...}, "classification" results are
    {"column": ..., "categories": ..., "labels": [...]}."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT result FROM analyses WHERE file_id = %s AND type = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (file_id, analysis_type),
        )
        row = cur.fetchone()
        return row[0] if row else None


def delete_analyses(file_id: int, analysis_type: str) -> None:
    """Deletes every saved analysis of this type for a file — used to
    clear the way for a fresh regenerate, since only one "current" result
    per type is kept at a time."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM analyses WHERE file_id = %s AND type = %s",
            (file_id, analysis_type),
        )
        conn.commit()


def list_files(project_id: int) -> list[dict]:
    with get_connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, form_type, filename, detected_columns, uploaded_at "
            "FROM files WHERE project_id = %s ORDER BY uploaded_at DESC",
            (project_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def get_project(project_id: int) -> dict | None:
    with get_connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, name, description, created_at FROM projects WHERE id = %s",
            (project_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def rename_project(project_id: int, name: str, description: str = "") -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE projects SET name = %s, description = %s WHERE id = %s",
            (name, description, project_id),
        )
        conn.commit()


def delete_project(project_id: int) -> None:
    """Deletes the project and, via ON DELETE CASCADE, every file/record/
    analysis under it. Caller is responsible for confirming this with the
    user first — this function itself does not ask."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM projects WHERE id = %s", (project_id,))
        conn.commit()


def delete_file(file_id: int) -> None:
    """Deletes the file and, via ON DELETE CASCADE, its records/analyses.
    Smaller blast radius than delete_project, but same caveat: caller
    confirms first."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM files WHERE id = %s", (file_id,))
        conn.commit()
