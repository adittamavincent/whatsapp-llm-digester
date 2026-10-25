# WhatsApp archive and extraction pipeline

Run commands from `/Users/vincent/code-repos/projects/whatsapp-llm-digester`. The absolute `run.sh` path also works from another directory.

## Current source decision

Use a Finder backup of the primary iPhone as the candidate historical source. Use wacrawl for available Desktop history and later updates. A Desktop import cannot establish full iPhone coverage. Keep the raw iPhone databases and media produced by the exporter; the exporter JSON is a derived representation and may omit metadata its parser does not understand.

The iPhone exporter reads Finder/iTunes backups. It does not download the WhatsApp iCloud backup. The maintainer confirms that it does not rely on the in-app Chat Backup feature: <https://github.com/KnugiHK/WhatsApp-Chat-Exporter/discussions/183>. iCloud device backup, WhatsApp's in-app iCloud backup, and a Finder computer backup are separate sources. This tool implements the Finder route only. Do not reinstall WhatsApp or replace the current iPhone data to produce an archive.

On 22 October 2026, the local MobileSync root contained four dated directories, none with the required manifests. Three were empty. They were not usable backups. A new Finder backup is needed unless a complete backup exists elsewhere.

## 1. Prepare and inspect the iPhone backup

Connect the iPhone by cable, unlock it, and trust the Mac if prompted. In Finder select the iPhone, then General → Back up all of the data on your iPhone to this Mac → Back Up Now. An encrypted Finder backup is supported; retain its password. Wait for Finder to finish. Apple instructions: <https://support.apple.com/en-us/108796>.

```sh
./run.sh iphone inspect
```

This reads metadata without changing the backup. It reports missing manifests, completion state, encryption, and backup date. For an unencrypted manifest it also checks WhatsApp database entries and whether their payload files exist. An encrypted candidate's WhatsApp coverage remains unverified until decryption. `usable: true` means a candidate passes these preliminary checks; it does not mean all historical messages and attachments are present.

For a backup stored elsewhere:

```sh
./run.sh iphone inspect --backup "/absolute/path/to/device-backup"
```

If macOS denies access, grant Full Disk Access to the launching application (Terminal or Codex), then relaunch it. Never paste a backup password into chat, a command argument, or a configuration file.

## 2. Export a new preserved snapshot

Use the exact backup directory reported by inspect. Run encrypted exports interactively in Terminal; the exporter asks for the Finder backup password using a hidden prompt.

```sh
./run.sh iphone export --backup "/absolute/path/to/device-backup"
```

Each export gets a new directory under `/Users/vincent/code-repos/library/whatsapp/snapshots/iphone/`. Existing snapshots are not overwritten. The pinned isolated runtime is WhatsApp-Chat-Exporter 0.13.1, with a pinned `iphone_backup_decrypt` revision for encrypted backups. First use downloads code/dependencies; extraction itself is local. The digester's environment is not changed.

The snapshot retains the exporter output, extracted databases/media, `result.json`, and `snapshot.json`. A failed/interrupted export keeps its report and partial output for diagnosis; do not treat it as complete. The source Manifest.db checksum is compared before/after export to detect a backup being replaced during extraction.

`snapshot.json` records message/media/reply counts and exporter-reported missing media. Its coverage remains `unverified`. Verify a sample against the iPhone: oldest and newest messages, old group conversations, quoted replies, PDFs, voice notes, and images. The exporter currently parses some reply metadata but support is not a guarantee for every WhatsApp version. Known missing-contacts/version issues are discussed in the maintainer thread above. Do not disable encryption merely to work around an unexplained error.

WhatsApp Business backups use `iphone export --business --backup PATH`.

## 3. Populate the extraction queue

From an exported iPhone snapshot:

```sh
./run.sh enrich iphone "/absolute/path/to/the/new-snapshot"
```

This reads `result.json`, indexes attachment bytes, and retains the export's chat/message IDs, caption, sender, timestamp, reply ID, and quoted data with each media reference. It does not reconstruct reply metadata missing from the export. It does not merge all iPhone chat messages into wacrawl's schema.

From the existing Desktop archive:

```sh
wacrawl --db /Users/vincent/code-repos/library/whatsapp/wacrawl.db sync --copy-media
./run.sh enrich sync
```

`enrich sync` is distinct from `wacrawl sync`: it reads the existing archive and never initiates WhatsApp synchronization or writes to wacrawl. Missing files are tracked as missing references. Link messages without a local preview path are excluded from attachment counts.

For other preserved source files or media folders:

```sh
./run.sh enrich add "/absolute/path/to/media-folder"
```

`add` does not copy originals; retain that source directory. Prefer `enrich iphone` for exporter snapshots so message associations are preserved. Symlink files and SQLite databases are excluded from generic folder scans. No automatic iCloud downloads are implemented.

## 4. Run, inspect, resume, retry

```sh
./run.sh enrich status
./run.sh enrich queue --limit 30
./run.sh enrich queue --state missing --limit 10
./run.sh enrich run --limit 5
```

`run` without a limit processes all pending assets, serially. Status and queue can be read in another Terminal while the worker runs. Model/server startup appears in worker stdout; `running` identifies an asset once conversion begins. SHA-256 deduplicates identical files across messages and sources. Files named only with a hash are classified from signatures such as PDF, JPEG, PNG, Ogg, WebP, and MP4 before conversion.

The worker uses the existing converters and locally managed PaddleOCR server. OCR starts only for selected OCR work; status/queue/import do not load inference models. Existing converter flags remain available:

```sh
./run.sh enrich run --limit 5 --language id
```

Changing conversion configuration requeues completed assets that have no result for that configuration, retaining older results. `--force` requeues completed assets for a fresh conversion. Failed assets require explicit retry:

```sh
./run.sh enrich retry
./run.sh enrich run
```

Use `enrich retry HASH_PREFIX` to retry one failed asset. Ctrl-C returns the active asset to pending. A killed worker's abandoned running state is recovered on the next run. An OS lock permits one writer; concurrent sync/add/run/retry attempts are rejected while status/queue remain readable. Failures include attempt counts and error messages.

After restoring/downloading missing source files, rescan with the same `enrich sync`, `enrich iphone`, or `enrich add` command. Retry alone cannot recover absent bytes.

Read a result using its unique hash prefix from queue:

```sh
./run.sh enrich show HASH_PREFIX
./run.sh enrich show HASH_PREFIX --json
```

`done` means the processor returned and the result was stored; it does not certify OCR/transcription accuracy. Check names, numbers, dates, and clauses against originals. Unsupported formats remain `unsupported`. Photo descriptions still need the optional local vision model. Video processing retains the existing sampled-frame behavior; it does not analyze every frame.

## SQLite snapshots before Unison

Live wacrawl/enrichment database files and their sidecars are excluded by the code-repos Unison profile. Publish a completed, verified generation before syncing:

```sh
./run.sh library snapshot
unison code-repos
```

The generation is stored under `library/whatsapp/snapshots/sqlite/` and includes standalone SQLite files plus checksums. Media and iPhone exports sync separately. The helper uses the SQLite backup API; each database is consistent independently. Verify a generation on either drive with `./run.sh library verify /absolute/path/to/generation`. No automatic restore or retention deletion is performed. Do not treat the external replica as a second independently writable SQL archive.

## Data locations and ownership

| Location | Owner and contents |
| --- | --- |
| `/Users/vincent/code-repos/library/whatsapp/wacrawl.db` | wacrawl message archive; read-only input to enrichment |
| `/Users/vincent/code-repos/library/whatsapp/media/` | wacrawl-preserved attachment files |
| `/Users/vincent/code-repos/library/whatsapp/snapshots/iphone/` | New iPhone export snapshots and raw extracted evidence |
| `/Users/vincent/code-repos/library/whatsapp/enrichment.sqlite` | Queue, references, extraction results, processor configurations, and attempts |
| `/Users/vincent/code-repos/projects/whatsapp-llm-digester/.cache/paddleocr/` | Existing OCR runtime logs/models |

No per-message Markdown files or combined corpus are generated by enrichment. Text and metadata live in SQLite. Original media and temporary worker staging still use files. Database sidecars and the worker lock are expected SQLite/runtime files. To override defaults, put `--db PATH` before the enrich subcommand; `enrich sync --archive PATH` selects another wacrawl database.

For a simple backup of enrichment, stop its worker and other writers, then use SQLite's backup API (for example Python `sqlite3.Connection.backup`) rather than copying only a live `.sqlite` file and ignoring its WAL. Retain the original media/snapshots separately; enrichment is not a backup of their bytes.

## Remaining work

The current interface is CLI, not a dashboard. Historical source completeness cannot be established until a real iPhone backup is exported and compared. Cross-source conversation reconciliation, a full WhatsApp clone, and the project-scoped MCP interface are not implemented. Their input should be preserved source databases and message identifiers, not inferred links between adjacent texts.

## Verification

```sh
.venv/bin/python -m unittest test_enrichment test_iphone_backup test_launch test_digest test_projects
uv tool run ruff check enrichment.py iphone_backup.py test_enrichment.py test_iphone_backup.py launch.py
git diff --check
```

The tests use isolated synthetic sources. They cover duplicate imports, changed content/configuration, missing media, read-only source preservation, worker locks, failures/retry, interruption recovery, extensionless media, iPhone media/reply provenance, and backup validation. Exporter dependency installation/help was exercised; encrypted extraction itself still needs a real backup and password entered locally.
