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
  under demand spikes (observed in practice). `generate_ai_insights()` now
  retries with backoff (2s, 4s) on `ServerError`, and also on a 429
  `RESOURCE_EXHAUSTED` `ClientError` specifically (longer backoff — 15s,
  30s — since rate-limit windows take longer to clear than transient
  overload; see the AI Classification section below, where this was
  actually hit in testing).
- **Governance update (2026-07-14)**: real Optimus/O2 data reaching the AI
  layer has since been explicitly authorized — the AI Classification
  feature below was tested against the real Safety Observation Form export.
  The original caution here (only dummy data should reach Gemini) is no
  longer the operating assumption; kept for history, not as a current
  restriction.

## AI Classification tab (2026-07-14)
Added to close a real gap against the internship deliverable's wording
("AI models to analyse and classify... automated categorisation of
project documentation and information") — the AI Insights tab only
*summarizes* the data in prose; nothing previously generated new
structured data. The Safety Observation form is the clearest case: it has
no defect-type/severity column at all, only free-text `Description of
Observation` — the "🏷️ AI Classification" tab lets Gemini generate that
categorisation, which doesn't exist anywhere in the original export.

**Two-phase design, with a human checkpoint in between:**
1. `induce_taxonomy()` — proposes a short, fixed category list (+ always
   appends "Other") from a sample of the column, rather than hardcoding
   categories, so this stays generic across any Optimus form's free-text
   field (same "no per-form-type logic" philosophy as the rest of the app).
2. The proposed categories are shown in an editable text area — the user
   can review/edit before committing, rather than the app immediately
   classifying everything against an unreviewed, possibly-off taxonomy.
3. `classify_all()` / `classify_batch()` — classifies every row against
   the *confirmed* list, batching ~25 rows per API call (fewer, larger
   calls rather than one call per row) so every batch stays consistent
   with the same fixed categories.

**Zero-shot, not few-shot**: the classification prompt gives the category
list and the texts to classify, but no labeled example classifications.
Considered adding hand-written few-shot examples, but deferred — good
examples should come from a human verifying real zero-shot output first,
which didn't exist yet. Revisit once there's a corpus of spot-checked
correct classifications to draw examples from.

**Temperature**: classification calls (`_call_gemini_json`, shared by
both phases above) use `temperature=0.0` — deterministic categorisation,
not creative variation. `generate_ai_insights()` (AI Insights tab) keeps
`temperature=1.0` — separate config per call, no shared/global setting.

### Critical gotcha: hit a real 429 on first live test
Testing against the real Safety Observation Form (141 rows, 6 batches of
25 + 1 taxonomy call = 7 calls in quick succession) hit `429
RESOURCE_EXHAUSTED` on the classification step — despite going in assuming
usage was too low to matter. The existing retry-with-backoff only caught
`ServerError` (5xx); a 429 comes through as `ClientError` (4xx) with
`.code == 429`, so it wasn't retried at all and failed immediately.

**Fix**: `_call_gemini_json` (and `generate_ai_insights`, for the same
class of failure) now specifically catches `ClientError` with `code ==
429` and retries with a longer backoff (15s, 30s, ...) than the 5xx case.
`classify_all()` also adds a flat 3s pause between batches, to avoid
tripping the limit in the first place rather than only reacting to it
after the fact. After both fixes, a full 141-row run completed cleanly.

**Lesson**: "we probably won't hit rate limits at this scale" was wrong in
practice on the very first real test — worth verifying against the actual
free-tier limits (check the current numbers at
[ai.google.dev/gemini-api/docs/rate-limits](https://ai.google.dev/gemini-api/docs/rate-limits))
rather than assuming, especially before pointing this at a larger real
export than the 141-row sample.

### Not yet done
- Partial-failure resilience — if a batch fails after all retries are
  exhausted, the whole run still raises and prior successful batches'
  results are lost (not persisted incrementally).
- Batch size (25) was picked for the 141-row sample; reconsider upward if
  real exports are meaningfully larger, to cut down total call count.
- Few-shot examples (see above) — deferred until a corpus of verified
  classifications exists.

### AI Insights ↔ Classification enrichment (2026-07-14)
The two tabs are otherwise fully independent (separate session-state keys,
separate helper functions — `build_data_summary()` never calls the
classification functions or vice versa), deliberately: Classification is
several batched API calls plus a human review step, Insights is one cheap
call, and forcing the former as a prerequisite for the latter would punish
anyone who just wants a quick summary. But `build_data_summary(df, cols,
classification=None)` now accepts the Classification tab's result as an
optional argument — if the user already ran Classification this session,
its category counts get folded into the Insights summary (e.g. "42% of
observations were Housekeeping issues"), which the raw export alone often
can't say. Purely additive/optional, not a hard dependency either way.

## Upload 403 on files with embedded images — UNRESOLVED (2026-07-14)
The Quality Defects Inspection Form fails to upload (on Airbase staging)
with `AxiosError: Request failed with status code 403` on
`/_stcore/upload_file/...`; the Safety Observation Form uploads fine.

**Root cause narrowed down, not fixed**: reproduced independently via a
headless browser, confirmed the 403 response body is a raw nginx-style
"padding" error page (`<!-- a padding to disable MSIE and Chrome friendly
error page -->`), not anything Streamlit itself generates — meaning this
is blocked at a gateway/WAF layer in front of the app, before Streamlit
ever sees the request. Ruled out both more-obvious causes: scanned every
cell value in the Quality file for script tags/SQL keywords/path traversal
etc. (zero hits, all short plain text), and confirmed file size is
near-identical between the two forms (22KB vs 19KB). The one concrete
structural difference found: the **Quality Defects file contains 5
embedded JPEG images** (`xl/media/image*.jpeg` + a `drawing1.xml`/VML
structure) that the Safety file has none of — a very plausible WAF
trigger (embedded binary media in an uploaded Office document is a known
flagged pattern), but this is inferred from correlation, not confirmed
against actual Airbase/WAF logs (no access to those).

**This is a platform-level policy, not an app bug** — not fixable in
`app.py` or the Dockerfile. Considered disabling Streamlit's XSRF/CORS
protection (`enableXsrfProtection`/`enableCORS = false` in
`.streamlit/config.toml`) as a fix — flagged it as a real security
tradeoff needing explicit authorization, which the user then gave
specifically to test it empirically. **Tested on 2026-07-15, confirmed
it does not fix this**: identical raw 403, byte-for-byte the same
nginx-style error page, before and after disabling both settings.
Reverted immediately (`git revert`-style, plain edit back to no `[server]`
section) — confirmed the Safety Observation form still uploads correctly
post-revert. This empirically confirms the earlier reasoning: the block
happens before Streamlit's own code runs, so no Streamlit-level config
change can touch it, regardless of which specific setting is tried.

**Next step, not yet done**: raise with Airbase support/admin, armed with
the embedded-images correlation as a concrete lead — public Airbase docs
don't mention any WAF/upload-content-scanning policy at all, so this can't
be resolved from documentation alone.

## Visual polish via native Streamlit theming (2026-07-14)
Explicit constraint: no CSS injection, no `unsafe_allow_html`, no
unofficial hacks — only documented `.streamlit/config.toml` theme keys and
Streamlit's own layout primitives. This Streamlit version (1.58) has a much
richer native theme surface than commonly assumed — worth checking
`config.py`'s `_create_theme_options` calls directly for the full current
list rather than assuming only the ~5 basic keys exist.

**Used**: `font`/`headingFont` (loads a real Google Font via the
`"Name:https://fonts.googleapis.com/..."` URL syntax — no self-hosting
needed), `baseRadius`/`showSidebarBorder` for consistent rounding and a
clean sidebar divide, a distinct `[theme.sidebar]` background,
`metricValueFontWeight`, `dataframeHeaderBackgroundColor`,
`chartCategoricalColors`. `st.metric(..., border=True)` and
`st.container(border=True)` give native bordered "card" styling with zero
CSS. Also replaced the deprecated `use_container_width=True` with
`width="stretch"` throughout while touching this code.

**Tried and reverted**: colour-coding the Raw Data table's status column
by distinct value via pandas' `Styler` API (`df.style.map(...)`, which
`st.dataframe` supports natively). Worked correctly — verified visually
after learning the dataframe grid is canvas-rendered and paints
asynchronously, so a screenshot taken immediately after switching tabs can
look empty even when it's actually fine — but the user felt it didn't add
enough visually and asked to revert. Reverted via `git revert` (not reset)
to keep history; the commit before it (`cb13c82`) is the clean fallback
point if this or a similar idea comes up again.

## Database integration (Neon Postgres) — schema built and tested, not yet wired into the UI (2026-07-14/15)
Motivation: scale from "analyse one uploaded file per session, nothing
persisted" to a real system — a database of JTC construction/design-phase
projects, each with multiple data files uploaded over time, with
classification/insights results persisted instead of recomputed every
session. Files are added manually (curated from Optimus exports, which
have no API access — see "What this is" above, and see the "Optimus API
feasibility" investigation below); that manual step isn't going away,
only what happens after it.

**Governance note — dummy data only so far**: everything built and tested
below used purely synthetic test data (fake project/rows), not real
Optimus exports. Persisting real project data across all JTC
projects/files is a materially bigger governance commitment than the
earlier one-file/one-AI-call authorization already granted — still
pending: (1) an app-classification exercise for O2 Data Analytics itself
(the same exercise already done for EDM Infographics — never done for
this app, and given it handles real names/real construction data, it
likely doesn't land at "Non-Sensitive"), and (2) explicit governance
sign-off specifically for persistent storage on Neon (an external,
non-GCC-vetted service — GCC itself runs on the same commercial cloud
hyperscalers underneath, so the real distinction is vendor vetting, not
"cloud vs. not cloud"). Do not point this at real data until both are done.

**Provider**: Neon (serverless Postgres, AWS `ap-southeast-1`) — provisioned
directly/independently (not through RabbitDeploy, though it's the same
underlying service RabbitDeploy also offers). Connection string in
`DATABASE_URL` (`.env`/`.env.staging`), same bring-your-own-DB pattern
already established for `GEMINI_API_KEY` — Airbase itself still has no
managed database.

**GCC egress to Neon — confirmed working.** This was flagged as the one
thing that could block the whole plan (outbound HTTPS was already known
to work via live Gemini calls, but Postgres uses a different port, and
GCC has shown tighter controls elsewhere — CSP enforcement, mandatory
hardened base images). Verified via a temporary sidebar connectivity-test
button (added, confirmed working live on Airbase staging, then removed
once confirmed — see git history around 2026-07-15 if this needs
resurrecting for any reason). Egress is not a blocker.

**Schema implemented** (`schema.sql`, applied directly to the Neon
instance) — hybrid relational + JSONB, not fully normalized:
```
projects(id, name, description, created_at)
files(id, project_id FK, form_type, filename, detected_columns JSONB, uploaded_at)
records(id, file_id FK, row_index, data JSONB)   -- one row per original spreadsheet row
analyses(id, file_id FK, type CHECK IN ('insights','classification'), result JSONB, created_at)
```
`projects`/`files` are normal typed columns since they have a genuinely
stable shape. `records.data` is JSONB specifically because Optimus forms
have completely different, unrelated column sets (Quality Defects: 28
columns incl. trade-specific pairs; Safety Observation: 8; Contract
Cashflow: unconfirmed) — a fully normalized table-per-form-type schema
would force a migration every time a new form type appears, reintroducing
at the database layer the exact per-form hardcoding problem the app's code
already deliberately moved away from once (see "Architecture decision"
above). `detected_columns` caches `guess_columns()`'s output per file so
it doesn't need recomputing. GIN index on `records.data` for querying into
JSONB fields (e.g. `data->>'Workflow'`).

**Rejected alternatives**: classic EAV (entity-attribute-value, one row
per cell) — even more flexible than JSONB but notoriously bad for
query/join performance, JSONB is the modern equivalent without that
downside. A NoSQL document DB instead of Postgres — no real gain given
Neon/Postgres is already the chosen provider, and would lose clean
relational structure for `projects`/`files` that don't need flexibility.

**`db.py`** — thin Python layer over the schema, deliberately kept
separate from `app.py` (distinct concern from the Streamlit UI):
`create_project`, `save_file`, `load_file_records`, `save_analysis`,
`list_projects`, `list_files`. One real correctness fix worth noting:
`save_file` serializes rows via `df.to_json(orient="records")`, not a
naive `df.to_dict()` + `json.dumps()` — the naive approach raises
`TypeError` the instant a row contains a numpy `int64`/`float64` or a
`Timestamp`/`NaT`, which is every real Optimus export (dates, numeric
scores, etc.). pandas' own JSON encoder already handles these correctly;
re-inventing that conversion by hand was the wrong instinct.

**Verified end-to-end** against the real Neon instance with synthetic
test data covering the actual edge cases that matter (missing dates,
missing numbers, missing text) — all of create project → save file+records
→ load back → verify round-trip → save both analysis types → list
projects/files passed. Test rows deleted afterward; Neon is clean.

### UI wiring — done (2026-07-15)
Sidebar now has: a Project selector (pick existing / create new — the
create flow explicitly tracks and re-applies the intended selection
across the rerun, since a plain `st.selectbox()` keeps showing stale
widget state otherwise), a "📂 Load a saved file" path as an alternative
to uploading, and a "💾 Save to database" action after processing an
upload. AI Insights/Classification results persist via `save_analysis()`
whenever the current data came from a saved/persisted file. All of this
degrades gracefully — a missing/unreachable `DATABASE_URL` just hides
these sections rather than crashing the app.

**Bug found and fixed while testing the load path**: records loaded back
from the database round-trip through JSONB as plain ISO-format date
strings, not a real datetime dtype. The existing `dayfirst=True` parsing
(tuned for ambiguous Excel exports) actively corrupts these — confirmed
it silently turns `"2026-02-01"` into `"2026-01-02"` and drops
`"2026-01-15"` to `NaT` entirely. Fixed by explicitly parsing DB-loaded
date columns (via the cached `detected_columns`) without `dayfirst` right
at the load boundary, before anything else touches them. Verified this
doesn't get re-corrupted by the existing downstream `dayfirst=True` calls
elsewhere in the app — re-parsing an already-`datetime64` column is a
no-op regardless of `dayfirst`, since that flag only affects string
interpretation.

Verified end-to-end with a synthetic test file (not real data): create
project → upload → save to database → reload the page → load the saved
file back → confirm both the data and the date range match the original
exactly. Test project deleted from Neon afterward.

### Not yet done
- The governance items above (app classification + explicit Neon
  persistence sign-off) — still required before any real data goes in.
  As of 2026-07-15 this remains unresolved — classification attempts in
  conversation were inconsistent (Open → Closed/Sensitive Normal →
  Non-Sensitive across three tries) because no actual classification has
  been done by an authoritative source; needs a real answer from
  whoever owns data governance, not a guess.
- No dedicated read-side UI for browsing/comparing across projects or
  files yet (only load-one-file-at-a-time exists) — cross-file pattern
  recognition (the original motivation for persistence) isn't built.

## Project/file management moved to main content area (2026-07-15)
The sidebar had become a stack of expanders (Project selector, file
uploader, "Load a saved file", "Save to database") — workable but cramped,
and it had no delete/rename story at all. Rather than bolt delete/rename
onto more sidebar expanders, added a `📁 Manage: <project>` section to the
**main content area** (not a new `st.tabs()` entry — the existing
`if uploaded is None and loaded_file_info is None: ... st.stop()` gate that
the Overview/AI Insights/Classification/Raw Data tabs all sit behind would
need larger restructuring to support a tab usable with no file loaded yet;
a plain section above that gate gets the same "give it room" benefit
without that rework). It renders whenever a project is selected — before
the upload/empty-state gate, so it's usable with zero files loaded.

**What it does**: rename project (name + description), delete project
(type-the-project-name-to-confirm, since deletion cascades to every
file/record/analysis under it — chose type-to-confirm over a plain
Yes/No given that blast radius), and a saved-files list with per-row
**Load** and **Delete** (two-click confirm — smaller blast radius than
project delete, so a lighter confirmation pattern is proportionate). The
old sidebar "📂 Load a saved file" expander was removed; the sidebar now
only has the Project selector and file uploader. A `📁 Project: X · 📄
File: Y` caption ("you are here") was added just above the KPI row.

`db.py` gained `get_project`, `rename_project`, `delete_project`,
`delete_file` — all thin, relying on the schema's `ON DELETE CASCADE` for
the cleanup rather than manually deleting child rows.

**Bug found and fixed**: `st.session_state["project_choice"] = ...`
(used to redirect the project dropdown after create/rename/delete) raises
`StreamlitAPIException` when it runs *after* the `project_choice`-keyed
selectbox has already been instantiated earlier in the same script pass —
which the rename/delete code paths do, since the Manage section they live
in renders after the sidebar selectbox. Fixed by writing to an
intermediate `_pending_project_choice` key instead, and applying it to
`project_choice` at the very top of the sidebar block, before the
selectbox exists for that run — the officially-supported way to redirect
a keyed widget's value across a rerun.

**Bug found and fixed**: saving a file to the database didn't call
`st.rerun()`, so the Manage section (which renders earlier in script
order than the save action, by design) kept showing its stale
pre-save file list until some unrelated later interaction triggered a
further rerun. Fixed by calling `st.rerun()` right after the save
(with `st.toast()` for the success message, since it survives one rerun).

Verified end-to-end with Playwright against a synthetic test file/project:
create project → Manage section appears → rename → upload + save file →
"you are here" indicator appears → file shows in Manage's saved-files
list → delete file (confirm prompt → confirm → gone) → delete project
(type-to-confirm → gone). Test data cleaned up from Neon afterward.

## Saved insights/classification: restore on load, one-at-a-time (2026-07-15)
Previously, `save_analysis()` wrote insights/classification to the
`analyses` table on generation, but nothing ever read them back — loading
a saved file always started both AI tabs blank, and clicking "Generate"
again just silently appended another row (no dedupe, no overwrite). Fixed
both problems together:

- **Restore on load**: `db.py` gained `get_latest_analysis(file_id, type)`
  (most recent row by `created_at`) and `delete_analyses(file_id, type)`.
  When a file is loaded from the Manage section, `app.py` now calls
  `get_latest_analysis` for both types right after `db_file_id` is set,
  and pre-populates `st.session_state["ai_insights"]` /
  `["classification"]` if a saved result exists — same session-state keys
  a fresh generation would have used, so the rest of each tab's logic
  doesn't need to know whether the data came from Gemini just now or from
  Neon a week ago.
- **One at a time, delete-to-regenerate**: both tabs now branch on
  whether a result already exists in session state. If it does, the tab
  shows the result plus a "🗑 Delete this insight/classification" button
  and **hides the generate/propose flow entirely** — regenerating (or
  classifying a different column) requires deleting the current one
  first, which also calls `delete_analyses()` so it's actually gone from
  Neon, not just hidden client-side. This replaces the old behaviour of
  letting a fresh generate silently pile up duplicate `analyses` rows for
  the same file.

**Bug found and fixed while wiring this up**: the classification tab's
"1. Propose categories" step used to explicitly pop `st.session_state["classification"]`
on a fresh proposal (to invalidate stale labels tied to an old taxonomy).
That's now dead code/unreachable — the propose/run flow is only ever shown
when no classification exists in the first place — so it was removed
rather than left as defensive dead code.

**Bug found and fixed**: after restructuring both tabs into
show-existing-or-show-generate branches, a fresh "Generate"/"Run
classification" no longer displayed its own result in the same run (the
old code fell through unconditionally into a display block after
generating; the new code is in an `else` branch that a `elif` won't
re-enter on the same pass). Fixed by adding `st.rerun()` right after a
successful generate/classify+save, matching the same rerun-for-freshness
pattern already used for the Manage section's save-to-database action.

Verified with two Playwright passes: (1) a live-Gemini run confirming
Generate/Propose buttons disappear and Delete buttons appear immediately
after a real generate/classify; (2) a seeded-data run (bypassing Gemini
entirely, via direct `db.save_analysis()` calls in a Python script, to
avoid stacking live API rate-limit retries across repeated test runs)
confirming restore-on-load shows the right content with the right tabs
gated, and that deleting actually removes the row from Neon (checked via
`db.get_latest_analysis()` returning `None` afterward) and brings the
generate/propose flow back. Test data cleaned up afterward both times.

## Empty-state message removed, expanders replaced with st.dialog modals (2026-07-15)
The empty-state info message (shown before any file is uploaded/loaded)
was removed per explicit request — the app now just `st.stop()`s silently
before any data exists, no onboarding paragraph.

Separately, the Manage section's `st.expander`-based rename/delete UI was
replaced with real popup modals via `st.dialog` (stable since Streamlit
~1.37, confirmed available on 1.58 which this app runs): "New project"
(sidebar), "Rename project", "Delete project", "Delete file" are each a
`@st.dialog`-decorated function called directly from a button's `if`
block. This was a direct fix for reported UX friction — expanders push
everything below them down the page when opened, which felt heavy for
occasional low-frequency actions, and the app had accumulated four
different expanding/inline-reveal patterns (sidebar "+ New project"
fields, Rename expander, Delete-project expander, two-click inline
delete-confirm per file). A dialog floats over the page as an overlay and
doesn't disturb the surrounding layout at all, so all four collapsed into
one consistent interaction pattern. The per-file delete's old two-click
inline confirm (toggle a warning + Yes/Cancel buttons in place) became a
single 🗑️ icon button that opens the same kind of confirm dialog.

Dialog functions are defined once, near the top of the sidebar/Manage
code (after `cached_list_projects`/`cached_list_files` are already
defined, since the dialogs call them), and invoked with per-row arguments
(e.g. `delete_file_dialog(f)`) from inside the saved-files loop — calling
a `@st.dialog` function is what opens it for that rerun; Streamlit tracks
the open/closed state internally afterward.

Verified with Playwright: new-project dialog opens and creates +
auto-selects the project, rename dialog opens and renames, file
delete-dialog opens and removes the file (confirmed gone from Neon),
project delete-dialog opens and removes the project. Test data cleaned
up afterward.

## Loaded file is now scoped to its owning project (2026-07-15)
Bug reported: load a saved file from Project A, then switch the sidebar
project selector to "(none)" (or to a different project) — the file's
data (KPIs, tabs, everything) kept showing on screen, with no visible
indication it belonged to a project no longer selected. There's no
cross-project comparison feature in this app to justify that as
intentional persistence — it was just stale state.

Fixed by tagging `st.session_state["loaded_file"]` with the `project_id`
it was loaded under (set alongside `file_id`/`filename`/`detected_columns`
when the Manage section's "Load" button is clicked), and clearing
`loaded_file` whenever the currently selected project no longer matches
that tag — checked right where `loaded_file_info` is first read, next to
the existing "a fresh upload clears any loaded file" rule. This only
applies to files loaded from the database; a fresh upload not yet saved
is untouched by project-switching, since choosing a destination project
for an unsaved upload is a normal part of that workflow, not stale state.

Verified with Playwright against two seeded test projects (each with its
own saved file): loading Project A's file shows it; switching to "(none)"
clears it and the tabs disappear; loading it again then switching
straight to Project B also clears it (not just the "(none)" case); B's
own file still loads normally afterward. Test data cleaned up.

## Sidebar removed entirely — everything moved to a top panel (2026-07-15)
Reported UX friction: project selection and upload lived in the sidebar,
while Manage (rename/delete/saved-files) lived in a separate block in the
main content area further down — two locations for what's really one
"set up this session" job, plus a permanent strip of sidebar width that
was empty most of the time. Fix: every remaining `st.sidebar.*` call was
moved into the main content area — Project selector, "+ New project",
file uploader, the multi-sheet "Sheet" selector, "Save to database", and
the category "Filter" — and merged into one bordered panel at the very
top, right under the page title, with Manage appearing inside the same
panel (below a divider) once a project is selected.

Streamlit only reserves sidebar space when something is placed into
`st.sidebar` during a run; with nothing left there, no sidebar renders at
all (confirmed via Playwright — zero `stSidebar`/`stSidebarCollapsedControl`
elements in the DOM), so this is a real width reclaim for the KPI cards
and charts below, not just a relocation.

Also tightened the Manage panel's own footprint per request: the project
name went from `st.subheader` (large heading font) to plain bold
markdown text (`**📁 {name}**`, body-sized), and the "Saved files" label
from a bold `st.write` line to a single `st.caption` that also doubles as
the empty-state message ("No files saved..." vs "Saved files") instead of
two separate lines. The old "💾 Save to database" `st.expander` became a
two-column inline row (label input + button) with no click-to-expand step
at all, consistent with dropping expanders elsewhere in favor of either
dialogs (for occasional, higher-stakes actions) or just always-visible
compact rows (for actions used every time a file is uploaded).

Verified with Playwright: screenshotted the empty state (compact
side-by-side Project/Upload panel, full test_page width, zero sidebar
elements) and the loaded-data state (Manage panel + KPI row + compact
Filter row all visible without scrolling on a 1400×900 viewport). Re-ran
the full create → rename → upload+save → delete-file → delete-project
dialog flow against the restructured layout — all steps still pass
(confirmed project/file deletion directly against Neon where the
Playwright text-match assertion itself was flaky, a known issue with
`text=` locators matching multiple partially-overlapping elements, not an
app bug — see the same caveat noted earlier for file-list assertions).

Also worth knowing for anyone building small test fixtures:
`guess_columns()`'s category-vs-text cardinality threshold is
`1 < nunique <= max(30, len(df) * 0.5)` — with a 3-row test file, that
upper bound is `max(30, 1.5) = 30`, so almost any column (even genuine
free text) gets classified as "category," not "text," and won't show up
in AI Classification's eligible-columns list. Test fixtures for anything
touching classification need enough rows (40+ with genuinely distinct
values worked) for the free-text column to exceed that threshold.

## Alignment, naming, and a duplicate-project-name bug (2026-07-16)
User feedback on the new top-panel layout: the "+ New project" button
didn't line up with the dropdown next to it, and the Manage panel's
Rename/Delete buttons didn't line up with the saved-file rows' Load/
Delete — both previously used a manual blank `st.write("")` as a spacer
to fake vertical alignment, an approximation that didn't quite land.
Fixed properly using `st.columns(..., vertical_alignment="bottom"/"center")`
— a real, documented Streamlit parameter (available since ~1.32, confirmed
present on the 1.58 this app runs) — instead of the spacer hack. The
Manage panel's header row and each saved-file row now also share the
exact same `[3, 2, 1, 1]` column split so their buttons land in identical
x-positions. The saved-file row's delete button changed from a bare 🗑️
icon to "Delete" text, matching the Rename/Delete convention already used
one row up. Title shortened from "Optimus Data Analytics Dashboard" to
just "Optimus Data Analytics", matching the project's existing naming
convention everywhere else (folder name, `page_title`, etc.) — "Analytics"
kept over "Analysis" for the same reason.

**Bug found while testing the alignment fix, not part of the original
ask**: seeded a test project literally named "Testt" to visually compare
against the user's screenshot, without checking first whether that name
already existed — it did: the user's own real project (id 7, with their
actual Contract Cashflow / Safety Observation files) is also named
"Testt". This exposed a real defect: `project_options = {p["name"]: p["id"]
for p in projects}` keyed the project selector by name, so two projects
sharing a name collide in the dict and the selector silently resolves to
whichever one the dict happened to keep — including for Delete. No data
was lost (only reads happened against the wrong project during testing —
create and load are non-destructive; confirmed via direct query that the
real project's two files were untouched throughout), but the bug itself
was real and would bite the user the moment they ever created two
same-named projects by accident.

Fixed in two parts, because the first fix alone wasn't sufficient:
1. Changed the selectbox to use project **IDs** as the option values
   (`format_func` supplies the display name), so the underlying value is
   never ambiguous.
2. That still wasn't enough on its own — confirmed via a standalone
   repro script that Streamlit's selectbox can't distinguish two options
   whose *rendered* `format_func` text is identical, even when the
   backing values differ; it always resolves to the last matching
   option regardless of which visually-identical row was actually
   clicked. So `_format_project_choice` now also disambiguates the
   *label itself* whenever a name collision exists, appending `(#id)` —
   `created_at` was tried first but rejected since two projects can be
   created within the same minute (or even the same second, as the test
   itself did), so it isn't reliably unique enough; the id is the one
   thing actually guaranteed distinct.

Verified with a dedicated test: seeded two projects under a deliberately
non-colliding test name (`TEST-DUPLICATE-NAME`, to avoid ever repeating
the original mistake) with different saved files each, confirmed the
dropdown now shows disambiguated labels (`TEST-DUPLICATE-NAME (#31)` /
`(#30)`), and that selecting each one loads that project's own file, not
the other's. Re-ran the full create/rename/upload/delete-file/
delete-project dialog flow to confirm the id-based rewrite didn't break
the ordinary (non-duplicate) case. All test data cleaned up; the real
"Testt" project (id 7) confirmed untouched throughout.

**Follow-up: duplicate names now prevented outright (2026-07-16).** Asked
the user directly whether duplicate project names should be allowed at
all, now that the selector no longer breaks on them — decided no: there's
no real workflow that wants two identically-named projects, and the
`(#id)` disambiguation suffix is a fallback, not something a user should
ever actually see. Added `CREATE UNIQUE INDEX idx_projects_name_lower ON
projects (LOWER(name))` to `schema.sql` (applied directly to the live
Neon instance too) — case-insensitive, so "Testt" and "TESTT" are treated
as the same collision, not just exact-case duplicates. `db.py`'s
`create_project`/`rename_project` catch `psycopg2.errors.UniqueViolation`
and re-raise as a new `DuplicateProjectNameError` with a clean message;
`new_project_dialog`/`rename_project_dialog` in `app.py` catch it and
show `st.error(...)` instead of crashing. The id-based selection +
label-disambiguation fix from above is kept as a defensive fallback (in
case of old data or any edge case that slips past the constraint), even
though it should no longer be reachable in normal use going forward.

Verified: attempting to create/rename to an existing name (including a
different-case variant) raises `DuplicateProjectNameError` cleanly at the
`db.py` level and shows a proper error message in the dialog at the UI
level, in both cases without creating a stray row. Confirmed renaming a
project to its own unchanged name (e.g. editing just the description)
still works — the unique index doesn't conflict with a row matching
itself. Confirmed no case-insensitive duplicates existed in the live data
before adding the constraint, so the migration applied cleanly.

## Chart and UI color polish (2026-07-16)
User feedback: the color scheme "looks very plain." Ran the existing
`chartCategoricalColors` through the dataviz skill's palette validator
before assuming it just needed a subjective refresh — it actually
**failed** three of four hard checks: the pale blue (`#6FA8DC`) read as
gray (below the chroma floor), two colors were nearly indistinguishable
under protanopia (ΔE 0.7), and two more were barely distinguishable even
with normal color vision (ΔE 13.8, below the 15 floor). Replaced with an
8-color set built from the skill's validated reference ordering
(`references/palette.md`), substituting this app's own brand navy into
slot 1 and re-validating in that position — passes lightness band,
chroma floor, CVD separation, and normal-vision floor against both this
app's surfaces (white and the pale-blue secondary background). Three
slots read below 3:1 contrast on a light surface by design (documented in
the skill as expected for warm/light hues) — mitigated by this app's bar
charts already showing direct value labels (`text="Count"`), not color
alone.

Also added, all via `.streamlit/config.toml` only (no CSS, per this
project's native-styling-only rule): `showWidgetBorder = true` +
`borderColor` so inputs have real definition instead of blending into
white; `dataframeBorderColor` matching; semantic colors
(`greenColor`/`yellowColor`/`orangeColor`/`redColor`/`blueColor`/
`violetColor`/`grayColor`) so `st.success`/`st.warning`/`st.error` and
`:green[]`/`:red[]` markdown read as part of the same palette instead of
Streamlit's unrelated defaults; `linkColor`/`linkUnderline`. Removed the
now-dead `showSidebarBorder` and `[theme.sidebar]` block, since the
sidebar itself was already removed in the previous session's change.

## Streamlit performance model, and caching load_file_records (2026-07-16)
User asked what determines Streamlit's loading speed, then confirmed the
lag they'd noticed switching between projects matched the explanation:
**every interaction reruns the entire script top to bottom**, not just
the part that changed — so any single click's latency is bounded by
whatever the whole script does on that pass, not by the specific widget
touched. The two biggest levers are (1) what's wrapped in
`st.cache_data`/`st.cache_resource` vs. re-executed on every rerun, and
(2) external calls sitting in that rerun path (Neon query latency, Neon's
scale-to-zero cold start, a blocking Gemini call).

Re-reading the app against that lens surfaced a live instance of exactly
this: `df = db.load_file_records(loaded_file_info["file_id"])` in the
main flow was **not** cached, unlike `cached_list_projects()` /
`cached_list_files()` just above it — so every rerun (switching projects,
touching the Filter dropdown, changing tabs, anything) re-fetched and
re-deserialized the entire currently-loaded file's records from Neon,
even though nothing about that file had changed.

Fixed with `cached_load_file_records()`, deliberately **not** given a
`ttl` like the other two caches — those exist because their underlying
data changes (create/rename/delete project or file), so a short TTL
balances freshness against re-query cost. A file's records have no
mutation path at all once saved (`db.py` has no "update records in
place" — a re-upload always becomes a new file id), so there's no
freshness tradeoff to make; it's cached with `max_entries=20` only to
cap memory if a session loads many distinct files. `st.cache_data`
(rather than `st.cache_resource`) still applies here since the caller
mutates the returned `df` (the date-column reparse right below it) —
`cache_data` hands back a fresh copy each call, so that mutation can't
corrupt the cached original.

Verified: a bare-Python timing check showed the first call hitting Neon
(~80ms) and the second call returning in ~0ms. Seeded two files of
different row counts under one test project, loaded each in turn via
Playwright, and confirmed no cross-file contamination — each file's own
row count and filename displayed correctly regardless of load order or
how many unrelated reruns happened in between. Test data cleaned up
afterward.

## Visual hierarchy via button type, not just palette (2026-07-16)
User asked for an honest opinion on the current colors, then to make it
more aesthetic. Fair critique: the palette itself was already accessible
and validated (see the earlier color-polish entry), but the page was
still almost entirely white/black/gray — every button (Load, Rename,
Delete, +New project, Save, Generate, Propose/Run) looked visually
identical regardless of importance, so nothing guided the eye and it read
as a generic wireframe.

Fixed mainly with `st.button(..., type=...)` — Streamlit has three native
button types (`primary`/`secondary`/`tertiary`; `tertiary` renders as
plain text with no border/fill, confirmed via `inspect.signature`).
Applied a consistent rule across the whole app: **primary** (filled,
navy) for the one forward-moving action per context — "+ New project",
"Load", "💾 Save", "Generate AI Insights", "1. Propose categories", "2.
Run classification..." — and **tertiary** (ghost/plain-text, de-emphasized)
for secondary/management actions — "Rename", "Delete" (both project- and
file-level triggers on the main page; the confirm buttons *inside* the
delete dialogs stay primary, since at that point deleting is the thing
you came to do), "🗑 Delete this insight/classification". Also two small
`config.toml` refinements: `secondaryBackgroundColor` deepened from
`#EAF1F8` to `#DCE9F5` (the old value was so pale it barely read as blue
on widget surfaces like the uploader box and dropdowns), and `baseRadius`
softened from `0.5rem` to `0.75rem`.

Verified visually via Playwright screenshots (empty state, loaded-data
state, AI Classification tab) — primary actions now clearly pop against
the de-emphasized Rename/Delete text-buttons — and re-ran the full
create/rename/upload/delete-file/delete-project dialog flow to confirm
none of this affected actual behavior (button `type` is purely visual).
Test data cleaned up afterward.

## Five features closing the deliverable-wording gap (2026-07-16)
Internship deliverable #1's actual wording asks for AI-based "data
extraction, pattern recognition, and automated categorisation" (see
`[[project-jtc-internship-context]]` memory). Before this batch,
categorisation was solid (AI Classification) and pattern recognition was
shallow (one prose summary per file, never beyond a single file); data
extraction didn't exist at all. Built five features together, in
dependency order, to close that gap:

### 1. Cross-file/cross-project Portfolio view
New `render_portfolio(project_id)` — a "📊 View portfolio (aggregate all
files)" button in the Manage panel (shown whenever a project has ≥1 saved
file) switches into an aggregated view instead of the single-file tabs.
Pools status/category counts, a combined time trend, and combined
AI-classified categories across **every saved file in the project**,
which nothing in the app could do before (only one loaded file at a
time, even though the DB has held multiple files per project since it
was first built). Different files can have entirely different column
names — Optimus form types don't share a schema — so this pools by
*role* (each file's own detected status/category/date column via its
cached `detected_columns`), not by column name.

Session-state-wise this is its own mode (`portfolio_mode`), mutually
exclusive with `loaded_file`: entering portfolio mode clears any loaded
file and vice versa, and — a bug caught before it shipped — a **fresh
upload** while in portfolio mode needed the same clearing, otherwise the
upload would silently sit unprocessed behind the portfolio view (the
existing "fresh upload takes precedence" block only cleared
`loaded_file`, not `portfolio_mode`, until this was added alongside it).

### 2. Time-series trend charts
New `trend_chart(df, date_col, title)` — previously the Overview tab
showed only a static "Date range: X to Y" caption, no chart of whether
issues were increasing or decreasing. Bucket width adapts to the data's
actual span (daily ≤14 days, weekly ≤120 days, monthly beyond that) so a
one-week upload doesn't render as a single flat monthly dot, and a
year of data doesn't render as 365 daily bars. Wired into both the
single-file Overview tab (using that file's own date column) and the
Portfolio view (using dates pooled across every file).

### 3. AI Extraction (new tab)
New "🔍 AI Extraction" tab, structurally parallel to AI Classification
(propose → review/edit → run) but a genuinely different operation:
classification assigns **one category label** per row; extraction pulls
**several named, structured fields** out of unstructured text into new
columns (e.g. `defect_type`, `location`, `hazard_source` — confirmed via
a live run against synthetic data, Gemini proposed exactly these kinds of
fields unprompted). This is the literal "data extraction" the deliverable
names, and nothing in the app did this before — Classification's one
label per row doesn't capture "this defect happened at location X caused
by Y," multiple independent facts about the same row.

New Gemini helpers `induce_extraction_fields`/`extract_batch`/
`extract_all`, mirroring `induce_taxonomy`/`classify_batch`/`classify_all`'s
established retry-with-backoff and rate-limit pacing exactly (`extract_all`
uses a smaller batch size — 20 vs. classification's 25 — since each row now
returns a whole dict of fields instead of one label, using more output
tokens per call). Same restore-on-load and delete-gating pattern as
Insights/Classification: `db.py`'s `analyses.type` CHECK constraint was
extended to allow `'extraction'` (migrated directly on the live Neon
instance, plus `schema.sql` updated for any future fresh deployment).
Row alignment back to the full (non-dropna'd) dataframe uses the same
`pd.Series` `.loc[values.index] = ...` pattern already established by
Classification, just holding dicts instead of strings per cell — verified
this assignment pattern works correctly for object-dtype Series before
relying on it (a quick standalone check, not assumed).

### 4. Shared/canonical taxonomy per project
New `taxonomies` table (`project_id`, `column_name`, `categories`, unique
on `(project_id, column_name)`) plus `db.get_taxonomy`/`db.save_taxonomy`.
Previously every classification run invented its own category list from
scratch, even for the same column in the same project — meaning
Portfolio's aggregated classification chart (feature 1) would have been
comparing unrelated ad-hoc labels across files, not a real aggregate.
Now, selecting a column in AI Classification checks for an existing saved
taxonomy for that (project, column) pair and, if found, offers "Use saved
taxonomy" as an alternative to proposing a new one from scratch; running
classification (whether via a saved or freshly-proposed taxonomy) saves/
overwrites the project's taxonomy for that column via `db.save_taxonomy`.

### 5. Anomaly/outlier flagging
New `flag_outliers(counts, min_points=4)` (mean + 1.5×std, a simple
explainable spike detector — not a rigorous statistical test, and
deliberately does nothing until there are at least 4 distinct values,
since "usual" is meaningless with 2-3 categories) and
`annotate_outliers(fig, counts, orientation)`, which marks flagged
bars/points with a small "⚠ spike" text annotation rather than a
recolor — per the dataviz principle that flagged meaning should carry a
direct label, not rely on color alone. Wired into both `bar_of_counts`
(single-file Overview charts) and the Portfolio view's aggregated charts.
`bar_of_counts` was refactored to delegate to a new `bar_of_series(counts,
title)` taking an already-computed Series directly, since Portfolio needed
the same chart built from counts pooled across multiple files — which
never existed as one DataFrame column to run `value_counts()` on.

### Verification
Built a synthetic fixture (40 rows, 5 categories with `Safety` deliberately
dominant — 22 of 40 rows — to guarantee the outlier threshold actually
trips) split across two files in one test project. Confirmed via
Playwright + screenshots: the trend chart renders in Overview; the
Portfolio view's KPIs/trend/status/category charts all render correctly
combined across both files; the "⚠ spike" annotation appears exactly next
to the dominant Safety bar; AI Extraction proposes sensible fields and
extracts genuinely differentiated values per row via a live Gemini call;
extraction restores on reload and is delete-gated exactly like Insights/
Classification (confirmed the delete actually removes the row from Neon,
not just client-side). One real bug caught and fixed: the shared-taxonomy
"Use saved taxonomy" flow initially appeared not to work in an early test
— turned out to be the test script's `:has-text()` selector matching
ambiguously, not an app bug (confirmed by a targeted repro using an exact
role-based selector, reproduced twice). All test data cleaned up from
Neon afterward; the real "Testt" project confirmed untouched throughout.

### Production bug found post-deploy: `trend_chart`'s monthly bucket (2026-07-16)
User hit `ValueError: Invalid frequency: ME` clicking into the Portfolio
view on staging. Root cause: `trend_chart`'s bucket-width logic used `"ME"`
for the >120-day-span branch, assuming `Period` and `Offset` frequency
strings share the same aliases — they don't. `Series.dt.to_period()` still
takes the old-style Period alias (`"M"` = month); `"ME"` (month-*end*) is
the newer alias pandas prefers for `resample()`/`date_range()`, and
`to_period()` raises on it rather than accepting it. This is exactly why
it slipped through testing: the original synthetic fixture only spanned
~40-60 days, so the `"W"` (weekly) branch was the only one ever exercised
locally — the `"M"`/`"ME"` branch was never actually run before reaching
production. Not related to any Gemini/API rate limit, despite the
traceback showing up right after a batch of AI feature testing — pure
pandas, no network call involved.

Fixed by using `"M"` (not `"ME"`) in the `to_period()` call. Verified with
a purpose-built fixture spanning 234 days (forcing the monthly branch)
against both the single-file Overview tab and the Portfolio view — this
also incidentally confirmed the outlier "⚠ spike" annotation's *vertical*
(line-chart) code path works, which the original horizontal-bar-only test
hadn't exercised either. Lesson for future chart-bucketing tests: always
construct fixtures that force *every* branch of a threshold-based bucket
choice, not just whichever one the first test dataset happens to land in.

## Optimus API feasibility (investigated, not pursued) (2026-07-15)
Investigated whether Optimus could be integrated with directly (API pull)
instead of manual Excel export, to remove the human export-then-upload
step entirely.

**Finding**: Optimus is almost certainly JTC's tenant of LeapThought's
**FulcrumHQ** platform, not a bespoke system — confirmed via the
`optimus.fulcrumhq.build` subdomain (SaaS vendors host client instances
exactly this way) and via the user's own DevTools Network tab on a live
logged-in session, which showed real internal API calls (`GetAll`,
`Get`, `Count`, `query`, `GetCurrentLoginInformations`) against an
ASP.NET/.NET backend (PascalCase endpoint naming, a SignalR WebSocket
connection). The `IssueTypeId=1544` parameter matched the dashboard URL
exactly, and "Issues" as the core entity name matches this project's own
export filenames (`Issues-51-Quality Defects Inspection Form.xlsx` etc.)
— confirms FulcrumHQ's data model organizes everything as "Issues" with a
`DefinitionType`/`IssueTypeId` distinguishing form types.

**LeapThought's own marketing explicitly advertises API/interoperability
capability** ("Interoperability, APIs & Open Standards"), but there's no
public developer portal, endpoint documentation, or auth specification —
what exists is confirmed to work, but is an internal/private API built
for FulcrumHQ's own frontend, not a published third-party contract.

**Decision: not pursued as unauthorized use.** Calling these endpoints
directly without the vendor's sanction would very likely breach JTC's
actual contract/ToS with LeapThought, has zero stability guarantee (could
change without notice), and is a genuinely different risk category from
automating an internal tool. Declined to assess "how feasible would
unauthorized access be" even as a hypothetical — this needs to go through
whoever manages JTC's LeapThought/FulcrumHQ vendor relationship, not be
attempted unilaterally.

**If pursuing officially, realistic options in rough order of
practicality** (none attempted — all require the vendor relationship
owner, not something buildable from this project alone): a direct,
customer-specific access grant to the already-confirmed-working API; a
scheduled/batch export feed (SFTP/webhook/cloud storage) instead of a
live API; an OData feed or Power BI/Power Automate connector (plausible
given the confirmed Microsoft-stack backend, and this project's own RPA
work already uses Power Automate); webhooks; a paid custom-integration
engagement. Manual export remains the correct approach until/unless one
of these is actually secured — not a stopgap to feel obligated to
engineer around.

## Deployment overview — three platforms coexist (2026-07-01)
All three hosting targets are live simultaneously. No conflicts — each uses its
own entry-point file and they share the same `app.py` / `requirements.txt`:

| Platform | Entry point | Gemini key source | Status |
|---|---|---|---|
| Streamlit Community Cloud | auto-detected `app.py` | `.streamlit/secrets.toml` via SCC Secrets UI | Live |
| RabbitDeploy | `Procfile` | Not configured (AI tab shows setup hint) | Live POC |
| Airbase (GCC) | `Dockerfile` + `airbase.json` | `.env` file, injected by Airbase CLI at runtime | Live POC |

Do **not** remove any of these files — they serve different platforms.

## Airbase deployment (2026-07-01)
Airbase is GovTech's self-serve GCC-hosted PaaS (Pilot stage). It is
Docker-based and CLI-driven — no GitHub import, no web UI deploy button.
Separate from RabbitDeploy and Streamlit Community Cloud.

Live URL (requires TechPass / accessible on normal browser):
**https://o2-data-analytics.fbi-dbe.airbases.gov.sg/** *(confirm current URL
in the Airbase console Environments tab — handle is `fbi-dbe/o2-data-analytics`)*

### CLI workflow
```bash
airbase login          # opens browser TechPass SSO
airbase deploy --yes   # builds Docker image locally, pushes to Airbase registry, deploys
```
Airbase CLI is installed at `C:\Users\jingr\AppData\Local\Airbase\CLI\airbase.exe`.
If `airbase` isn't found in a terminal, open a **new** terminal window after
install (PATH update only applies to terminals opened after the installer ran).

### Key files
- **`Dockerfile`** — uses `gdssingapore/airbase:python-3.13` (GDS-hardened base
  image, mandatory for CSP compliance on GCC). Runs as non-root `USER app`
  (GCC security requirement). Port `${PORT:-3000}` (Airbase injects `$PORT`
  at runtime; default 3000 matches `airbase.json`).
- **`airbase.json`** — `framework: "container"`, `handle: "fbi-dbe/o2-data-analytics"`,
  `port: 3000`. No env var support in this file.
- **`.env`** — gitignored, not in `.dockerignore`. Airbase CLI reads this at
  deploy time and injects values as runtime environment variables. Currently
  contains `GEMINI_API_KEY`. **Never hardcode secrets in Dockerfile ENV
  directives** (Airbase docs explicitly prohibit this).

### Critical gotchas encountered
**Docker image caching**: `airbase deploy` reuses an existing local Docker image
with the same tag even if the Dockerfile changed. If changes aren't taking
effect, delete the cached image first:
```bash
docker rmi local.airbase.sg/o2-data-analytics:<image-id>
# then redeploy
airbase deploy --yes
```
Find the `<image-id>` in Docker Desktop or `docker images`.

**Port mismatch → 502**: `airbase.json` says port 3000 but if the cached image
still runs on the old port (e.g. 8501), Airbase's Kong gateway gets a 502.
Fix: delete cached image, force fresh build.

**`gatherUsageStats = false` required**: Streamlit's metrics utility tries to
write a machine-ID file to a restricted path in the container on first start,
causing a startup error. Fixed by `.streamlit/config.toml` setting
`gatherUsageStats = false`. Without this, the container may appear to crash
before the app comes up.

**CSP warning (non-blocking)**: The browser console shows a CSP violation for
Streamlit's built-in inline script `window.prerenderReady = false` in its
`index.html`. This is Streamlit's own code, not ours. Airbase's own
documentation states "Streamlit generally works fine with CSP" — this warning
does not break any app functionality (file upload, charts, AI Insights all
work). No fix needed; it's an accepted known limitation.

**`gdssingapore/airbase:python-3.13` is mandatory** (not `python:3.11-slim` or
similar). The standard Python Docker images are not GCC-hardened and produce
real CSP violations that do break the app. The GDS base image resolves this.

### `.streamlit/config.toml` — required settings
```toml
[browser]
gatherUsageStats = false   # prevents container startup error (restricted filesystem)
```

## RabbitDeploy POC deployment (2026-06-24)
Live POC URL: **https://o2-analytics-dashboard.cio.sandbox.gov.sg/**

RabbitDeploy is GovTech CIO Office's internal sandbox PaaS, separate from
the public Streamlit Cloud demo — see the "Guide: Direct Claude Path" the
user pasted in-session for the full onboarding flow (TechPass login →
`#ask-rabbit` Slack for workspace assignment → create project). Only
reachable via a SEED/COMET device; the deployed app itself is a normal
public-ish gov-network URL though.

### Getting code in: ZIP upload, not git
There's no GitHub-import option in the UI. Two paths exist: a Repository
Token for `git push`, or **"Update Code" → upload a `.zip`**. We used the
ZIP path specifically because the SEED laptop can't run Claude Code (not
the user's device) and downloads are blocked there — so the workflow is:
develop with Claude on the main machine → `git archive --format=zip -o
out.zip HEAD` to export exactly the git-tracked files (auto-excludes
`.git`, `__pycache__`, venv, real Excel data, secrets — all gitignored) →
transfer just that one ZIP to the SEED laptop → upload via "Update Code".
When file transfer itself was also blocked, the fallback was recreating
each file by hand in VS Code via clipboard paste (text clipboard apparently
isn't blocked, only file downloads) and zipping **locally on the SEED
laptop** (Explorer → Send to → Compressed folder — no download involved).

### Critical deployment gotcha: Procfile required for non-FastAPI/Flask apps
Clicking "Deploy as POC" creates two Northflank services per product:
- **`<project>-main`** — the actual app container
- **`cf-<project>`** — a Cloudflare tunnel sidecar (`cloudflared`) that
  gives the public `*.cio.sandbox.gov.sg` URL without opening inbound
  firewall ports on Northflank. Public request → Cloudflare edge → this
  tunnel → `-main`.

RabbitDeploy's build script (visible in the Build Log) only **auto-detects
FastAPI or Flask** — it greps `main.py`/`app.py`/etc. for `= FastAPI(` or
`= Flask(` patterns, and if neither matches (as for any other framework,
Streamlit included), it falls back to a hardcoded `exec uvicorn main:app
--host 0.0.0.0 --port 3000`. That doesn't exist in a Streamlit project, so
the container crash-loops forever (Server Log: `Could not import module
"main"`, repeating every ~30s). From the browser this shows up as
`upstream connect error... connection timeout` — the Cloudflare tunnel is
working fine, it's just connecting to a container with nothing listening.

**Fix: a `Procfile`** (exact filename, no extension) in the repo root —
the build script checks for this *before* the FastAPI/Flask fallback, and
uses its `web:` line verbatim if present:
```
web: streamlit run app.py --server.port 3000 --server.address 0.0.0.0 --server.headless true --server.enableCORS false --server.enableXsrfProtection false
```
Each flag matters: port `3000` + `0.0.0.0` matches the container's
`EXPOSE 3000` (the ingress won't reach any other port/interface);
`--server.headless true` skips Streamlit's interactive first-run "enter
your email" prompt, which would otherwise hang forever in a
non-interactive container (the exact issue hit locally earlier in this
project, fixed there via a `credentials.toml` — headless mode is the
container-appropriate equivalent); `--server.enableCORS/XsrfProtection
false` is needed because Streamlit's default same-origin checks don't
match the Cloudflare tunnel's external hostname.

After adding the Procfile: re-zip, re-upload via "Update Code", then
"Rebuild" the `-main` service. Confirmed working — Server Log showed a
clean `Uvicorn server started on 0.0.0.0:3000` / `You can now view your
Streamlit app in your browser.`

### Database (not currently used)
"Create Database" offers a **Neon-hosted PostgreSQL** instance (AWS
Singapore, `ap-southeast-1`) with a dev connection string and Neon's
branching/autoscaling. **Not provisioned** — this app is stateless
(processes each upload in-memory per session), so there's nothing to
persist yet. Revisit only if a real cross-session persistence need shows
up (e.g. a history of past AI Insights).

### Production deployment — separately gated
The same project page has a "Deploy as Production" path (also Northflank,
via "GovPaaS Prod"), but it's blocked behind a Security Clearance /
"Production Ready Status" approval that has to be requested from CIOO —
not pursued; still POC-stage only.

## Data governance — IMPORTANT
Real Optimus/O2 records are **government data**. Before pointing this app at
live exports, the following must be decided with whoever owns data governance:
- **Where the app runs** (local laptop / internal gov server / GCC).
- **Whether any AI layer ever sees the data** (and if so, local-only e.g.
  Ollama vs. external endpoints).
Building and testing against sample/dummy data has no such constraint.

### Airbase app classification (2026-07-02)
Airbase requires every app to be classified on two axes: security
classification (Official Open/Closed/Restricted/Confidential+) and
sensitivity level (Non-Sensitive/Sensitive Normal/Sensitive High). This
deployment is classified **Official Open, Non-Sensitive** — appropriate
while only dummy/sample data is uploaded. **Must be re-assessed** (with
whoever owns data governance, per above) before real Optimus exports are
ever uploaded here — real records containing `Created by`/`Modified by`/
observation-notes fields with actual names would very likely push this to
a higher classification, which may in turn affect whether Airbase (max
supported: Restricted / Sensitive Normal) remains a valid hosting choice.

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
App serves at `localhost:8501`. Upload an Optimus Excel export from the
top panel (there is no sidebar — it was removed, see "Sidebar removed
entirely" below).

## Environment setup (reference)
```bash
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS / Linux
pip install streamlit pandas openpyxl plotly
```
