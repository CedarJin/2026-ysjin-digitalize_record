# Food Record Digitizer

Convert handwritten food records into evidence-linked structured drafts for research review.

**离线审核（0.4.0）已实现。** 负责人按可配置天数发包，合作者本地续审并回传结果，无需共享服务器或 API key。见[中文使用指南](docs/offline_review_guide.md)。审核工具启动包：`dist/offline-reviewer-0.4.0.zip`；首次安装需 Python 3.11+ 和联网安装依赖，后续审核离线。

**Current milestone: P0–P2.** The package includes incremental ingestion and AI drafts,
plus the authenticated review workbench: source comparison, editing,
participant/date assignments, independent review, clarification help, adjudication,
approval, audit history and frozen research releases. The existing 10-record pilot
remains **unreviewed** until humans complete these steps.

Start the local review page:

```bash
.venv/bin/food-record review --workspace work/pilot --port 8765 --env-file /dev/null
```

Open http://127.0.0.1:8765. See [review user guide](docs/review_user_guide.md),
[deployment and recovery](docs/review_deployment.md), and [P2 acceptance](docs/p2_status.md).
Reviewing existing results makes no AI requests and needs no model key.

Version 0.3.0 reviews all source records for the same participant and intake date together.
Participant and date follow metadata (0.3.3); only missing or invalid metadata needs attention in **Participant & date**. See [day review and migration](docs/day_review_design.md);
previous record-level reviews are retained as history and require fresh daily assignments.

All product pages, CLI help, errors and review instructions use English. Raw source text
is preserved in its original language.

## Install

Requires Python 3.11+ and Poppler (`pdftoppm` on PATH).

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
# macOS: brew install poppler
# Ubuntu/Debian: sudo apt-get install poppler-utils
food-record --help
```

The development environment was tested on Python 3.12. `requirements-tested.txt` records
its resolved runtime and test dependencies. PostgreSQL review transactions and concurrent-save conflicts have been integration-tested.
Use the deployment guide for a team server.
Local use can retain a SQLite workspace behind one server process; do not share its database file
across reviewers. The public Python service functions use SQLAlchemy sessions.

## Configure

Create `.env` in the working directory using `.env.example`. Never commit credentials.
Existing environment variables override `.env`. Dotenv content is parsed as text, not executed.

```dotenv
OPENAI_API_KEY=your-key
OPENAI_MODEL=gpt-6.1-sol
```

API extraction sends the selected scans and attachments to the configured provider and
uses that provider's billing. It does not consume a Codex subscription. No model request
is made by inspect, init, ingest, report, schema or export-draft. `extract --dry-run` is also
local. Explicitly confirm institutional data handling and the selected scope before a live run.

## Inspect and import a batch

```bash
food-record inspect --input clean_record --metadata clean_record/record_mata.xlsx \
  --pages --output work/inventory.json
food-record init --workspace work/study --project study --name 'Diet study'
food-record ingest --workspace work/study --input clean_record \
  --metadata clean_record/record_mata.xlsx
food-record report --workspace work/study
```

Metadata columns: `record_id`, `subject_id`, `data_type`, `date`, `timepoint`, `flag`.
XLSX reads the first worksheet; CSV uses UTF-8; JSON accepts an object or a list of objects.
Keep identifiers as text. ISO dates are preferred; unresolved raw dates such as `NA` are
retained and flagged, not guessed. Duplicate metadata IDs, duplicated/unnamed columns,
unexpected fields and inconsistent table row widths reject the import rather than silently
discarding or overwriting values. Duplicate JSON field names are rejected too.

`R0251.pdf` is the main source for `R0251`. `R0251-A1.pdf` and `R0251-A2.pdf` are
clarification attachments, never separate intake records. An attachment without its main
PDF is registered but cannot be extracted. An attachment may arrive without its own metadata
row; existing main metadata is retained. File-specific metadata rows, when supplied, are preserved.

Both metadata modes (`delta` and `snapshot`) merge supplied complete rows without deleting
absent records. A supplied empty field clears that field. No fuzzy identity matching occurs.
The input report retains file/metadata mismatches and duplicate file contents for review.
Duplicate-content detection also compares sources imported in earlier batches; the processing
report lists duplicates among all current project sources. A listed attachment with no scan
creates an explicit missing-source issue in the draft.

## Import one record or add an attachment

```bash
food-record ingest-one --workspace work/study --pdf incoming/R0401.pdf \
  --metadata incoming/R0401.json --attachment incoming/R0401-A1.pdf
food-record attach --workspace work/study --record-id R0401 \
  --attachment incoming/R0401-A2.pdf
```

Single-record metadata example:

```json
{"record_id":"R0401","subject_id":"S001","data_type":"hab","date":"2026-10-01","timepoint":"pre","flag":null}
```

## Extract selected drafts

Preview the scope, then run the same command without `--dry-run`:

```bash
food-record extract --workspace work/study --record-id R0401 --dry-run
food-record extract --workspace work/study --record-id R0401 --config configs/pilot.toml
# Alternatively select a returned batch ID, or only pending/failed records:
food-record extract --workspace work/study --batch-id BATCH_ID --limit 10
food-record extract --workspace work/study --pending-only --limit 10
```

Repeated `--record-id` selects several complete record groups. A request includes the main
PDF and all current attachments. The model reads images before deterministic metadata
comparison; metadata values are not inserted into the model's transcription prompt.
Maximum pages, rendering DPI and output tokens are bounded. Oversized groups fail explicitly;
attachments are not silently omitted. Blank or malformed PDFs are retained with error status.

A matching successful input-revision/model/prompt/schema/render configuration is cached. Changing
input creates a new revision. Existing candidates remain immutable. `--force` explicitly
creates a new candidate; it never overwrites old content. Model outputs cannot set human
review completion or resolve issues. A record changed during extraction rejects the stale
result. Current CLI processing is sequential, with a database claim preventing two workers
from extracting the same record concurrently.

Rendering verifies archived source hashes and cached PNG checksums/completeness. Incomplete
or changed image caches are regenerated, and new images are published only after verification.
If an archived source fails its hash check, extraction fails before contacting the model;
restore that source from a verified backup. Restoring old metadata values creates a new input
revision and cannot silently reuse a candidate bound to a prior revision.

Failures are recorded per run. Authentication/availability/rate-limit failures stop the batch.
There are no automatic billable retries in P1: after inspecting `report`, rerun the affected
record. Timeouts have an unknown remote outcome and may incur costs on retry.
If a worker was interrupted, first verify that it has stopped, then release its claim:

```bash
food-record recover-run --workspace work/study --run-id RUN_ID --worker-stopped
```

## Inspect the result

```bash
food-record export-draft --workspace work/study --output outputs/draft-001.json
food-record schema --output outputs/schema.json
```

Draft exports include current input snapshots, evidence, candidate provenance and a full
processing report. They explicitly contain `draft: true` and `research_approved: false`.
Export files cannot silently overwrite existing outputs. No approved research export command
exists yet. Unknown portions must remain unknown; bowls are not converted to grams.

## Files and recovery

- `workspace.json`: project and storage version, without database credentials.
- `sources/`: source PDFs addressed by SHA-256, retained across revisions.
- `records.db`: local P0/P1 state, input revisions, candidates and run/audit history.
- `rendered/`: cached page images, replaceable from source PDFs.
- `runs/<run_id>/response.json`: restricted local raw responses, including invalid output.

Back up the database and sources together while no importer/extractor is running. These files,
real sources, exports and `.env` are ignored by Git. The local workspace contains research
data and should remain in an approved storage location. Schema migration beyond storage
version 1 is not yet supported; do not silently open future versions with older code.

## Development checks

```bash
python -m pytest -q
python -m ruff check src tests
```

Tests use synthetic PDFs and mock provider responses; they make no external API requests.
See [P0 input contract](docs/input_contract.md), [data dictionary](docs/data_dictionary.md),
[P0–P1 verification status](docs/p0_p1_status.md) and [development plan](docs/development_plan.md).

### Regional endpoint authentication

If OpenAI returns `incorrect_hostname`, set `OPENAI_BASE_URL` in your local `.env`
to the official regional endpoint required by your project (for example,
`https://us.api.openai.com/v1`). A hostname mismatch can return HTTP 401 even with
a valid key. The CLI preserves allowlisted error details without storing raw error
messages that may contain credentials. Do not rotate a key solely for this error.

Execution uses the versioned `bounded-extraction-v1` policy described in
[Reproducible execution](docs/reproducible_execution.md). Defaults are 1200 seconds per
attempt and a fixed 32,000 → 64,000 output budget schedule. Only a confirmed output-token
truncation triggers the second attempt. Timeouts have unknown billing outcomes and are
not retried automatically. Configuration changes invalidate the workflow cache.

### Standard image preparation

Every extraction preserves the original PDF and verified full-page PNG rendering, then
prepares a transport copy for AI: maximum edge 1200 pixels, aspect ratio preserved, no
cropping or upscaling, JPEG quality 70. A smaller PNG is retained if JPEG offers no saving
and resizing is unnecessary. Page labels and attachments are unchanged.
Use `--image-max-edge 2000 --jpeg-quality 90` for small handwriting, or
`--image-max-edge 0` to send the original rendered PNG. Allowed nonzero edges are 1000–4096;
JPEG quality is 60–95. The same settings are available as `image_max_edge` and
`jpeg_quality` in extraction TOML. Resolution loss can hide handwriting; retained originals
remain the review reference. Compression reduces upload bytes, not necessarily output
generation time or billed image tokens.

Preprocessing version and parameters participate in the candidate cache key. Each run
records original/sent pixel dimensions, bytes and format per page. Changed settings create
a new candidate when explicitly extracted; old candidates remain in history. Existing
pre-compression pilot candidates remain valid historical drafts with their original settings.

The default output ceiling is 32,000 tokens per record request. Override it with
`--max-output-tokens` or `max_output_tokens` in TOML. This is a maximum, not a required
output size. Long outputs may need a longer request timeout; truncated JSON is never
accepted as a valid candidate.
