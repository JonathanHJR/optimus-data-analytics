-- O2 Data Analytics — database schema (Neon Postgres)
-- Hybrid relational + JSONB model: Projects/Files are normal typed columns
-- (stable shape), Records.data is JSONB because Optimus form types have
-- completely different, unrelated column sets (Quality Defects: 28 columns
-- incl. trade-specific pairs; Safety Observation: 8; Contract Cashflow:
-- unconfirmed) — a fully normalized table-per-form-type schema would force
-- a migration every time a new form type appears. See CLAUDE.md's
-- "Planned: database integration" section for the full reasoning.

CREATE TABLE IF NOT EXISTS projects (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS files (
    id SERIAL PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    form_type TEXT,
    filename TEXT NOT NULL,
    detected_columns JSONB,          -- cached guess_columns() output
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS records (
    id SERIAL PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    row_index INTEGER NOT NULL,
    data JSONB NOT NULL              -- one row's original columns/values, whatever shape
);

CREATE TABLE IF NOT EXISTS analyses (
    id SERIAL PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK (type IN ('insights', 'classification')),
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_files_project_id ON files(project_id);
CREATE INDEX IF NOT EXISTS idx_records_file_id ON records(file_id);
CREATE INDEX IF NOT EXISTS idx_records_data ON records USING GIN (data);
CREATE INDEX IF NOT EXISTS idx_analyses_file_id ON analyses(file_id);
