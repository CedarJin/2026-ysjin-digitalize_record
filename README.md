# Food Record Digitizer

Turn handwritten food-record PDFs into structured, evidence-linked data, with human review before research use. Version **0.5.0** supports single-record and batch imports, concurrent AI extraction, and offline review packages for collaborators.

The intended workflow is:

**PDFs + metadata → AI drafts → offline human review → owner approval → frozen research release**

The study owner keeps the main workspace on their computer. Collaborators receive ZIP packages, review on their own computers, and return result ZIPs. No shared server is required, and the owner's computer does not need to stay on while collaborators review.

This package transcribes food records. It does not automatically code foods against a nutrient database, estimate nutrient intake, or convert household portions into grams. AI drafts require human checking; a structurally valid result is not proof of transcription accuracy.

## One research engine, two ways to use it

The **Python package** is the research engine. The **desktop app** is a graphical interface that bundles and calls that same engine. They do not maintain separate extraction algorithms or separate review rules.

| | Python package | macOS desktop app |
| --- | --- | --- |
| Intended user | Researchers/developers comfortable with Python and Terminal | Nutritionists, study owners and reviewers who prefer a graphical interface |
| Entry point | `food-record` commands and the package's local review interface | Double-click **Food Record.app** |
| Installation | Python dependencies; Poppler for the default CLI renderer | Bundled Python and PDF renderer; no Terminal or developer tools required |
| Responsibilities | Import, source versioning, image preparation, AI extraction, evidence validation, review, offline exchange and releases | Study selection, native file dialogs, protected credential storage, processing queue and graphical access to the engine |
| Study storage | A local workspace with source files, metadata, drafts and review history | The same workspace format; local SQLite studies can be opened directly |
| Collaboration | Export reviewer packages and import result packages | Open reviewer ZIPs in the same app without an account or API key |

Choose the **desktop app** for everyday graphical use. Choose the **package** for scripted imports, batch automation or integration into another research workflow. Installing the package alone does not install the Mac app. Downloading the GitHub source ZIP is also not the same as downloading a built app.

### How data moves through the system

```text
PDF scans + metadata
        |
        v
Study workspace -- explicit AI processing --> Evidence-linked draft
        |                                           |
        |                               Human review and corrections
        |                                           |
        +-- reviewer ZIP --> offline reviewer ------+
                              |                     |
                              +-- returned ZIP --> Owner approval
                                                    |
                                             Frozen research release
                                             (JSON / CSV / XLSX)
```

A **record** consists of a main PDF and its clarification attachments. A **review unit** groups records for the same participant and known intake date; undated sources remain separate. A **study** owns the source versions, drafts, review history and releases. A **review package** contains only assigned review units and supporting sources; it is not a replacement for the owner's full study.

Import and review do not call AI. Only explicitly confirmed processing sends scans to the configured OpenAI API and incurs API charges; ChatGPT/Codex subscriptions do not cover these calls. Current-version drafts are reused. New or changed sources are versioned, affected reviews need checking again, and previously frozen releases remain unchanged.

### Desktop architecture

- `desktop/`: Electron main process and restricted preload bridge. Handles native dialogs, window lifecycle, Keychain-protected credentials and the bundled backend.
- `src/food_record_digitizer/`: shared Python engine plus desktop service, persistent queue, import preview and spreadsheet export adapters.
- `src/food_record_digitizer/static/`: graphical study management and shared owner/reviewer interfaces.
- `scripts/desktop/`: arm64 build and synthetic acceptance tools.

The app starts a private service bound only to the local computer, with a fresh launch credential and origin checks. The renderer is sandboxed and has no general shell or filesystem access. No shared server or cloud database is required. The app and a study are separate folders: replacing the app does not replace research data. Keep only one writer open for a study.

## macOS desktop app — Apple Silicon preview

The desktop app wraps this same Python package. It includes Python and a PDF renderer; end users do **not** need Terminal, Homebrew, Node.js, or a Python installation. This build targets Apple Silicon (including M1) and macOS 13 or newer. It is an internal, unsigned preview, not a signed/notarized public release. Intel Macs are not supported by this build.

### Install the app

Download the installer from the [v0.5.0 testing pre-release](https://github.com/CedarJin/2026-ysjin-digitalize_record/releases/tag/v0.5.0): choose **Food-Record-0.5.0-macOS-arm64.zip**, not the automatically generated Source code archive. A SHA-256 checksum file is provided alongside it. This is a testing preview; read the release notes for compatibility and pending validation.

1. Unzip the **installer ZIP**.
2. Drag **Food Record.app** into **Applications** and open it.
3. This internal build is unsigned and unnotarized. If macOS reports an unidentified developer, use **System Settings → Privacy & Security → Open Anyway** only after confirming the installer came from your trusted study owner. See [Apple's opening-app guidance](https://support.apple.com/en-us/102445). Report other launch errors rather than disabling system-wide protection.

### Start with your own data

**A new researcher does not need an existing workspace, database or specially arranged input folder.** Enter a study name and click **Create study**, then choose a new folder such as `Documents/My Nutrition Study`. The app creates `workspace.json`, `records.db` and its managed source storage automatically. Use **Add records** to import your existing PDFs and metadata from wherever they are currently stored.

| What you have | Use this entry point |
| --- | --- |
| Your own PDFs and metadata, but no study yet | **Create study**, then **Add records** |
| A study previously created by this app or the package | **Open study**; select the whole folder containing `workspace.json` |
| An assigned reviewer ZIP from a colleague | **Open review package**; do not unzip the review ZIP |
| A full study backup ZIP | **Restore backup** into a new folder |

You do not need the original developer's workspace or research data. A raw PDF folder is not an existing study. Reviewers receive the app installer and their assigned review ZIP, not the owner's database or API key.

### Owner and reviewer workflow

1. Open **Food Record.app**. Choose **Create study**, enter a name and choose a new folder. For an existing package workspace, use **Open study** and select the folder containing `workspace.json`. The app backs up an older workspace before adding desktop state. Keep one writable instance per study; do not open the same study simultaneously in the CLI/web server or a cloud-synced shared folder.
2. Choose **Add records**. Select one or many PDFs and optional CSV/XLSX/JSON metadata. Download the CSV template if needed. In the association list, assign a main file an ID such as `R0001` and its clarifications `R0001-A1`, `R0001-A2`, etc. Edit participant/date metadata, check the import report, then import. Original filenames are unchanged. Unknown dates may remain blank. Metadata without a PDF does not become a review task. Importing does not call AI.
3. Select records and choose **Process selected**. The confirmation shows how many current drafts will be reused and how many records/pages require paid requests. **Existing candidates for the current input version are reused even if the default model changed.** Opening or reopening a study never starts processing. For new extraction, configure the API key and model in **Settings**. The saved key is encrypted with macOS Keychain protection outside the study. No key is needed to review existing drafts.
4. **Pause queue** stops new dispatches; already sent requests finish and are saved. After interruption, queued work stays paused. Unknown remote outcomes require explicit retry confirmation because they may already have incurred charges. **Select failed** excludes unknown-outcome requests. Timings appear per record. **Edit manually** creates a baseline without an AI request when readable sources and participant metadata are present.
5. Open **Review & collaboration**. Review complete participant/date groups, compare scans with food items, and edit time and label independently. Start your review, check every page, then submit. Approve only after required reviews are complete. **Edit review** reopens your submitted review; current approval is withdrawn and prior frozen releases remain unchanged.
6. Use **Export** for selected or all draft baselines, or preview approved days and create a frozen release. Archives contain XLSX, CSV and full JSON. The workbook separates food items, events, days, sources, issues and evidence. Blank values retain missing reasons; one-to-many evidence does not duplicate food rows. Original draft exports do not incorporate later human edits; approved releases contain the approved review results.
7. For collaborators, register an offline reviewer under **People & policy**, select days, and choose **Export review packages**. Set **days per package**; whole review units stay together. Downloaded task packages include the reviewer name. The collaborator opens the ZIP through **Open review package** in the same Mac app, works offline without a key or account, submits each day, and exports the result ZIP. They can reopen and revise submitted reviews. Import returns in **Offline packages**; duplicate/stale/conflicting returns follow the package's existing review rules and do not automatically approve data.
8. Use **Export → Save backup** to save a verified study backup. **Restore backup** on the start screen restores into a new folder. Recent studies appear on the start screen. Closing while processing offers **Keep processing** or **Stop after current requests**; forced termination never silently restarts requests.

The owner computer can be off while collaborators review. Review and exports work offline; new AI extraction requires an internet connection and API billing. The application has no nutrient-database coding or automatic nutrient calculation.

**Validation boundary:** packaged arm64 startup, bundled PDF rendering, isolated owner/reviewer workflows and automated regression tests have been exercised on the development Mac. A separate clean Mac/account, two physical Macs, minimum-version hardware, Keychain behavior across signed updates and a nutritionist usability session remain release acceptance checks. Do not treat this preview as D5 distribution certification.

### Building the desktop app (developers only)

Build on an Apple Silicon Mac with Python 3.11+ and Node.js 22.12+:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[desktop,dev]"
./scripts/desktop/build.sh
```

The output is `dist/macos/Food Record-darwin-arm64/Food Record.app`. The build rewrites in-bundle absolute symlinks to relative paths and stops if a bundled library link is broken or points outside the app. JavaScript dependencies are pinned in `desktop/package-lock.json`; Python build/PDF dependencies are pinned in the `desktop` extra. The build does not include local studies, `.env`, or `docs`. To run the synthetic checks, use `scripts/desktop/smoke_backend.py`, `scripts/desktop/prepare_qa.py`, `desktop/smoke.cjs`, `desktop/workflow-test.cjs` and `scripts/desktop/verify_return.py`; these never call AI. The remaining numbered sections are **Python package / CLI instructions**. Desktop users do not need to run those installation commands.

## 1. Install on the study owner's computer

Requirements:

- Python 3.11 or newer.
- Poppler, with `pdftoppm` available on your PATH, for PDF rendering.
- An OpenAI API key and network access for AI extraction. Extraction uses API billing, not a ChatGPT or Codex subscription.
- Local storage for PDFs, drafts, review history, and backups.

Clone the repository and install from its root directory:

```bash
git clone https://github.com/CedarJin/2026-ysjin-digitalize_record.git
cd 2026-ysjin-digitalize_record
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
food-record --version
food-record --help
```

On Windows, create the environment with `py -3 -m venv .venv` and activate it with `.venv\Scripts\Activate.ps1` in PowerShell. The examples below use a macOS/Linux shell; on Windows, enter multiline commands on one line.

Install Poppler using your operating system's package manager, for example:

```bash
# macOS
brew install poppler
# Ubuntu / Debian
sudo apt-get install poppler-utils
```

On Windows, install a Poppler distribution and add its executable directory to PATH. Check that `pdftoppm -h` works before extraction. The workflow has been exercised on macOS; Windows/Linux launcher scripts are provided, but have not had equivalent end-to-end testing on those operating systems.

Run subsequent commands from the repository root with the virtual environment activated. After closing a terminal, activate `.venv` again. You do not need to reinstall each time.

## 2. Prepare your study inputs

Use stable record IDs in PDF filenames:

```text
incoming/batch-001/
  R0001.pdf
  R0001-A1.pdf
  R0002.pdf
  metadata.csv
```

`R0001.pdf` is the main record. `R0001-A1.pdf` is a clarification attachment to that record, not a separate food record. Additional attachments can use `-A2`, `-A3`, and so on. An attachment without its main PDF cannot be extracted.

Prepare metadata as CSV, XLSX, or JSON. Example CSV:

```csv
record_id,subject_id,data_type,date,timepoint,flag
R0001,P001,hab,2026-01-15,baseline,
R0002,P002,hab,,baseline,
```

| Column | Meaning |
| --- | --- |
| `record_id` | Record identifier matching the PDF stem, without `.pdf`. Required. |
| `subject_id` | Stable participant identifier used to group records. |
| `data_type` | Your study's record type, such as `hab` or `pat`. |
| `date` | Actual intake date, preferably `YYYY-MM-DD`; leave blank if unknown. |
| `timepoint` | Study visit or timepoint. |
| `flag` | Source notes or known issues; `don't use` puts a record into exclusion-pending status. |

Keep identifiers as text, including leading zeros. XLSX uses the first worksheet; CSV uses UTF-8. JSON accepts a single object or a list of objects. Duplicate IDs, unexpected columns, and malformed rows are rejected rather than silently discarded. An attachment does not require its own metadata row; if supplied, its metadata is retained.

**Participant and intake date follow metadata.** Records for the same participant and known date are reviewed together. If the date is absent or invalid, it remains unknown: the page shows **Date unknown**, and the structured date is `null`. You do not have to invent or confirm a date. Undated source days remain separate review units so unrelated records are not merged. Missing participant identifiers still need attention.

Metadata-only records with no PDFs remain in the import report but do not appear in the review queue or review packages. If a record has a PDF but a specifically listed attachment is missing, that missing evidence still needs attention.

## 3. Create a workspace and import records

Use a new workspace and project identifier for each study. Do not reuse this project's local pilot workspace for an unrelated study.

```bash
food-record init --workspace work/my-study --project my-study --name 'My diet study'
food-record inspect --input incoming/batch-001 --metadata incoming/batch-001/metadata.csv --pages
food-record ingest --workspace work/my-study --input incoming/batch-001 --metadata incoming/batch-001/metadata.csv
food-record report --workspace work/my-study
```

`inspect` checks inputs without importing them. `ingest` archives source files and metadata in the workspace and returns a batch ID. Keep that ID if you want to extract only that batch. Review missing sources, duplicates, and exclusion flags in `report` before proceeding.

For one record, use a metadata JSON file such as:

```json
{"record_id":"R0003","subject_id":"P003","data_type":"hab","date":null,"timepoint":"baseline","flag":null}
```

```bash
food-record ingest-one --workspace work/my-study --pdf incoming/R0003.pdf --metadata incoming/R0003.json --attachment incoming/R0003-A1.pdf
```

Omit `--attachment` when there is none; repeat it for multiple attachments.

## 4. Configure and run AI extraction

Copy `.env.example` to `.env` and edit it locally:

```dotenv
OPENAI_API_KEY=your-key
OPENAI_MODEL=gpt-6.1-sol
```

Use a model available to your API project and compatible with this package's image and structured-output requests. `OPENAI_BASE_URL` can specify the endpoint required by your project. Existing environment variables override `.env`. Never include `.env` in a reviewer package or commit it to Git.

For reproducible settings, create `configs/my-study.toml`:

```toml
[extraction]
model = "gpt-6.1-sol"
workers = 3
limit = 20
max_pages = 20
dpi = 150
image_max_edge = 1200
jpeg_quality = 70
max_output_tokens = 32000
max_output_ceiling = 64000
request_timeout = 1200
```

First preview a small scope without contacting the API:

```bash
food-record extract --workspace work/my-study --record-id R0001 --config configs/my-study.toml --dry-run
```

After confirming the files and your study's authorization to send them to the provider, remove `--dry-run`:

```bash
food-record extract --workspace work/my-study --record-id R0001 --config configs/my-study.toml
```

For a batch, replace `BATCH_ID` with the value returned by ingestion:

```bash
food-record extract --workspace work/my-study --batch-id BATCH_ID --limit 100 --config configs/my-study.toml --dry-run
food-record extract --workspace work/my-study --batch-id BATCH_ID --limit 100 --config configs/my-study.toml
food-record report --workspace work/my-study
```

`--limit` is the maximum number of records selected by that command, not a package size or page count. Set it to cover the intended batch. CLI options override TOML settings. You can repeat `--record-id` to select several records or use `--pending-only` for pending/failed records.

Each record is a separate request containing its main scan and current attachments. `--workers 3` processes up to three records concurrently; the supported range is 1–8, with a default of 1 when not configured. Count other running extraction commands separately. Authentication, configuration, and rate-limit errors stop new dispatch; requests already in flight finish.

The standard image preparation preserves original PDFs and full-page rendered images, then creates smaller transport images for AI: maximum edge 1200 pixels and JPEG quality 70. For small handwriting, configure a larger edge or use `--image-max-edge 0` to send the original rendered image. Compression reduces upload size but does not guarantee a proportionate reduction in model time or cost.

The versioned execution policy allows a 32,000-token attempt followed by a 64,000-token attempt **only after confirmed output truncation**. Timeouts are not automatically retried because the remote outcome and billing may be unknown. Invalid output is saved for diagnosis and is never accepted as a valid draft.

Matching successful inputs and configuration are cached. Avoid `--force` unless you intend to create another billable candidate. The package retains input revisions, prompts, model settings, raw responses, and run durations for traceability.

## 5. Open the owner's review workspace

Initialize the first owner account once:

```bash
food-record review-init-owner --workspace work/my-study --email owner@example.org --name 'Study owner'
```

The command prints a private, one-time `invitation_url` valid for 24 hours. Start the local server:

```bash
food-record review --workspace work/my-study --port 8765
```

Keep this terminal running. Open the printed invitation URL in a browser, set a password of 12–256 characters, and log in with the owner email you supplied. For later sessions, start the same server command and open **http://127.0.0.1:8765**. This address is local to your computer; it is not a collaborator invitation.

The initial owner setup is separate from registering offline reviewers. Reviewing existing drafts makes no AI requests and needs no API key.

In **People & policy**:

- Use **Add an offline reviewer** to register each collaborator's name and email. No email is sent, and collaborators do not need accounts or passwords.
- Set the number of required independent reviews, high-risk review requirements, and optional double-review sampling before assigning work. Saving a new policy invalidates affected current reviews and approvals; frozen releases remain unchanged.

## 6. Export offline review packages

1. Open **Day review queue** and select the units to assign.
2. Click **Export review packages**, choose a reviewer, and set **Days per package**.
3. Click **Create review packages**.
4. Under **Offline packages**, use **Download review package** and send the ZIP to that reviewer through your study's chosen file-sharing channel.

The package-size parameter counts complete review units, not food items or PDFs. A known participant/date unit includes all its records and attachments; undated units are kept separate. For example, 23 selected units with a package size of 10 produce packages of 10, 10, and 3. Filenames include the reviewer's name and a package identifier.

For independent double review, export assignments for two different reviewers. Each receives the original frozen draft, without the other's answers. If a reviewer already has a current task for a selected unit, download the existing package or return/reassign the task instead of creating a duplicate.

Do not send your workspace database, API key, or owner credentials to reviewers.

## 7. Give collaborators the reviewer tool

A review task ZIP contains data; it is **not** the reviewer application. First-time reviewers need both the tool and their task package.

Build a launcher bundle from this checkout:

```bash
python -m pip install build
python -m build --wheel
python scripts/build_offline_bundle.py
```

Send `dist/offline-reviewer-0.4.4.zip` together with the reviewer's task ZIP. Generated bundles are local build outputs and are not included in a fresh Git clone. Use version 0.4.4 or newer for packages containing unknown dates.

Reviewer instructions:

1. Install Python 3.11+ if needed. Extract the **tool ZIP** to a writable folder.
2. Run **Open Reviewer.command** on macOS or **Open Reviewer.bat** on Windows. On Linux, run `python3 start_reviewer.py` from the extracted folder.
3. First launch requires internet access to install dependencies. Later review sessions work offline. No API key or Poppler is needed for current packages with page previews.
4. On **Open review package**, select the **task ZIP**, without unpacking or repacking it.
5. Open a review unit, compare the scans and metadata, and correct the food entries. **Time** and **Label (optional)** are separate editable fields within **Food items**. Either may remain blank when not recorded; a meal label is not inferred from time.
6. After checking a source page, click **I checked the whole page**. Check attachment usage and issue reminders. Ordinary reminders do not require a note for every issue; structural errors must be corrected.
7. Changes save automatically. Confirm **All changes saved**, then **Submit review** for the unit.
8. When every unit is complete, return to **Package overview**, click **Export results**, and send the result ZIP back to the owner.

Keep the launcher terminal open during review. Reopening the same task package on the same computer resumes saved progress, normally stored under `~/.food-record-reviewer/`. Computers do not synchronize automatically. Keep the original task ZIP and local saved data until the owner has received the results.

If a reviewer finds a correction after submitting, use **Edit review**, make the change, submit again, and export a new result ZIP. Tell the owner it is a revision. Previously downloaded ZIPs do not update themselves.

Alternatively, a collaborator with the package already installed can run:

```bash
food-record offline-review --input path/to/review-package.zip --workspace work/local-review
```

Use the same explicit workspace on later runs to resume that work.

## 8. Import reviews, approve, and export research data

1. In **Offline packages → Import returned results**, select the returned result ZIP.
2. Check its receipt. **Accepted for comparison** means the review was received, not that the data is approved.
3. Open the corresponding unit and inspect submissions in **Review & decisions**. Compare independent reviews and record an adjudication when needed.
4. Once the required current reviews and checks are complete, use **Approve for research** (or **Approve selected** in the queue).
5. Select approved units and click **Create release**. Download the **CSV + JSON archive**, also available under **Research releases**.

Repeated import of the same result is detected. Outdated inputs/tasks require a new review. If a task has a different returned result, both versions are retained for comparison rather than silently overwriting the first.

A release is a frozen snapshot. Later input changes or reviews do not rewrite it. Use a new release for revised research data.

### Use the release in an analysis

| File | Contents |
| --- | --- |
| `records.json` | Approved structured records with nested values and provenance. |
| `food_items.csv` | Food items, their event references, and participant/date/source identifiers. |
| `eating_events.csv` | Separate time, meal-label, and place fields. |
| `record_days.csv` | Intake-day fields. |
| `issues.csv` | Recorded issues and resolutions. |
| `export_manifest.json` | Release identity and file checksums. |
| `review_history.json`, `actors.json` | Review history and attribution. |
| `processing_report.*`, `source_processing_report.json` | Included scope and source-processing status. |

CSV fields containing structured values or evidence are JSON-encoded. For example, parse `description` and read its `normalized` property rather than treating the entire cell as the food name. Unknown values are `null`, not zero. To associate foods with time/label, join food items to events on **both `record_id` and `event_id`**. Release `record_id` identifies the review unit; `source_record_ids` retains original source IDs.

Example after extracting a release ZIP:

```python
import csv
import json
from pathlib import Path

release = Path("outputs/release-001")
with (release / "food_items.csv").open(encoding="utf-8", newline="") as stream:
    foods = list(csv.DictReader(stream))
for food in foods:
    description = json.loads(food["description"])
    print(food["subject_id"], food["date"], description["normalized"])
```

Use `records.json` when you need the full nested representation. CSV text is escaped where necessary to prevent spreadsheet formula execution. This output is a reviewed transcription dataset; study-specific food coding, nutrient linkage, and statistical analysis are subsequent steps.

For inspection before review, a separate draft export is available:

```bash
food-record export-draft --workspace work/my-study --output outputs/draft-001.json
food-record schema --output outputs/schema.json
```

Draft exports explicitly have `draft: true` and `research_approved: false`. Do not substitute them for approved releases. Export commands do not overwrite an existing output file.

## 9. Add more records later

Continue using the same study workspace. Import a new folder and metadata, then extract the returned batch ID as in steps 3–4. You do not need to initialize a new study or re-extract unchanged records.

To add an attachment to an existing record:

```bash
food-record attach --workspace work/my-study --record-id R0001 --attachment incoming/R0001-A2.pdf
food-record extract --workspace work/my-study --record-id R0001 --config configs/my-study.toml --dry-run
```

After checking the scope, run without `--dry-run`. Changed sources or metadata create a new input revision. Affected reviews become stale and need fresh assignments; prior history and releases remain available.

Both metadata import modes (`delta` and `snapshot`) merge supplied rows without deleting absent records. Treat a supplied row as a complete replacement of that row's fields: empty values clear prior values. Do not send partial rows unless that clearing is intended.

## 10. Timing, backups, and recovery

Export timing reports from this checkout:

```bash
python scripts/export_extraction_timings.py --workspace work/my-study --output outputs/timings
```

`record-timings.csv` summarizes each record; `extraction-runs.csv` lists individual attempts. Durations include preprocessing, API waiting, and saving, not just model generation. Manual retries have separate rows. Unavailable historical durations remain blank.

Keep the whole workspace together: `records.db`, `sources/`, `runs/`, `offline/`, and `workspace.json` hold the study's state and evidence. Rendered images can be regenerated from archived sources. Do not share a live SQLite database file across computers.

Stop the review server with Ctrl+C and stop or finish all import/extraction jobs before backup:

```bash
food-record backup --workspace work/my-study --output outputs/my-study-backup.zip --server-stopped
food-record restore --input outputs/my-study-backup.zip --output work/my-study-restored
```

Restore into a new directory. Keep an additional copy of backups outside the working computer. Source files, workspaces, exports, `.env`, and local `docs/` are excluded from Git; pushing the repository does not back up your research data. Review and result ZIPs are not encrypted.

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| `Unexpected archive entry` when opening a ZIP | Select the task ZIP, not the tool bundle or a returned result ZIP. Do not repack it. |
| **Export results** is disabled | Submit every unit in the package; a reopened review must be submitted again. |
| `Review incomplete` | Check all source pages, attachment usage, and structural issues shown in the review. |
| Missing intake date | Leave it unknown. It does not need a fabricated date or confirmation. |
| Missing participant | Correct metadata or use **Participant & date** to resolve the identity. |
| `This reviewer already has a current task...` | Download the issued package, or use **Review & decisions → Assignments → Return / reassign** before creating a new task. |
| Old test review appears in **Previous record reviews** | Use **Hide from history**. This only hides your legacy entry; it preserves saved data and current daily tasks. |
| Failed extraction | Read `report` and the run status. Rerun only the failed record after diagnosis; timeout retries may incur another charge. |
| `incorrect_hostname` from the API | Set `OPENAI_BASE_URL` to the official endpoint required by your API project; do not rotate a key solely for this error. |
| Interrupted worker leaves an active claim | Verify the worker has actually stopped, then use the recovery command below. |

```bash
food-record recover-run --workspace work/my-study --run-id RUN_ID --worker-stopped
```

Do not recover a claim merely because a request is slow. The default per-attempt timeout is 1200 seconds.

## Development and scope

All user-interface text and CLI messages are English; source transcriptions retain the original language. Local Chinese documentation under `docs/` is not distributed in Git; this README is intended to be sufficient for a new study owner.

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
python -m ruff check src tests
```

Tests use synthetic sources and mock providers, without billable AI calls. `requirements-tested.txt` records tested dependencies. Start with a small study-specific pilot and check legibility, transcription quality, and the complete review/return/release workflow before processing a large dataset.
