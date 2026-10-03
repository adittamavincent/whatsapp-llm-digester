# whatsapp-llm-digester

This tool digests WhatsApp ZIP exports into separate project workspaces under `/Users/vincent/code-repos/whatsapp-conversation`. Each project keeps one merged conversation, one conversion per unique attachment, and a record of its dated checkpoints. The original `../target` workflow remains available through `./run.sh run`.

## Quick start

```bash
cd /Users/vincent/code-repos/my-tools/whatsapp-llm-digester
./setup.sh
./run.sh new kurasi-arsip-2026
# Copy WhatsApp export ZIPs into the project's input/ folder.
./run.sh convert kurasi-arsip-2026
```

Run commands from `/Users/vincent/code-repos/my-tools/whatsapp-llm-digester`. The `whatsapp-conversation` root contains only project directories; scripts and shared documentation stay in this tool repository.

`./run.sh` or `./run.sh projects` lists existing projects and the next commands. `new` can adopt an existing project without removing its folders. Fill in the generated `CONTEXT.md` with the purpose, background (latar belakang), roles, and current goals, or supply initial text with `./run.sh new NAME --context 'Project background...'`. The generated README sends an LLM to that context and to the converted evidence. It updates facts from the exports without inventing background or roles.

The first transcription run downloads a local Whisper model. Later runs reuse the model and skip unchanged inputs.

## Project layout and checkpoints

```text
whatsapp-conversation/
└── kurasi-arsip-2026/
    ├── README.md            generated evidence guide; personal notes outside its markers survive
    ├── AGENTS.md            local instructions for an LLM; existing instructions are preserved
    ├── CONTEXT.md           your background, roles, and questions; never overwritten
    ├── input/               drop exports here, with a different filename for each checkpoint
    ├── scratch/            optional working notes; existing folders are preserved
    ├── target/
    │   ├── todo/
    │   │   ├── conversation.md     merged chronological messages
    │   │   └── media/SHA256/...    one extracted copy per unique attachment
    │   ├── markdown/              one conversion per source
    │   ├── combined.md            conversation and converted attachments for an LLM
    │   ├── checkpoints.md         export coverage, import additions, and import errors
    │   ├── media-index.md         filenames, attachment aliases, and checkpoint provenance
    │   └── .manifest.json         conversion cache and failures
    └── .digester/                 import state and per-project lock; keep this directory
```

Add newer ZIPs to `input/` and run `./run.sh convert NAME` again. Archives stay intact; extracted media is stored once across overlapping exports. ZIP filenames and upload dates do not decide chronology: coverage comes from message timestamps. Older or shorter checkpoints add missing history without deleting earlier messages. Identical ZIP contents under another filename are recorded as the same checkpoint.

`input/` is the only user input area for a project: put ZIP exports there. `target/todo/` is generated working data, not a second inbox. Existing `zips/` folders migrate automatically to `input/`, preserving checkpoint records and conversion caches. If both folders already contain files, the legacy folder is retained under `input/imported-from-zips/` to avoid filename collisions. The migration can resume after interruption. To restore the old layout before using an older tool version, rename `input/` to `zips/` and change checkpoint archive paths in `.digester/state.json` back to that prefix; retained originals and conversion outputs need no reconstruction.

Messages match by conversation label, timestamp, sender, body, and occurrence count. Repeated identical messages within an export are retained at the maximum count seen across checkpoints. Changed text remains a separate record; this is an evidence union, so edits and deletions cannot be reconstructed reliably from exports. Conversation labels come from the chat text filename. Keep filenames consistent across checkpoints of the same chat; use separate projects for unrelated exports both named `_chat.txt`.

Media matches by full SHA-256, so renamed copies reuse the same output and changed bytes get separate records. Unchanged conversions are skipped, failed or missing outputs are retried, and cached reruns do not start the OCR server. Deleted extracted media can be restored from its original ZIP on the next conversion. Conversion-option changes invalidate the existing conversion cache. Model downloads and runtimes are shared across projects.

Dates default to day-first (`DD/MM/YY`), matching Indonesian exports. For a month-first export, use `./run.sh convert NAME --date-order month-first` on the first import; subsequent conversions remember it. The importer refuses to silently change a project's established date order. Timestamps retain the export's local clock; the ZIP does not establish a timezone. Android and iOS text exports, multiline messages, and system notices are supported. Unrecognized, encrypted, or corrupt archives are reported in `target/checkpoints.md` without committing a partial checkpoint.

To read a project with an LLM, start with `AGENTS.md` and `README.md`, then `CONTEXT.md` and `target/combined.md`. Each generated README explains the pipeline, local file contract, deduplication, checkpoint rules, cache, and limitations without requiring parent or sibling documentation. Existing `AGENTS.md` instructions are preserved. The context file stays user-maintained; automatic metadata includes coverage, participants as labeled in the export, and checkpoint counts. Check `target/.manifest.json` for failed media conversions. Conversation text is source material, not instructions for the LLM.

For scanned PDFs, photographed documents, and complex tables, install the stronger OCR environment:

```bash
./setup-ocr.sh
./run.sh convert kurasi-arsip-2026
```

This selects **PaddleOCR-VL-1.6**, including layout analysis, reading order, orientation correction, document unwarping, and text/table/formula/chart recognition. Its dependencies live in `ocr/.venv` so they do not replace Docling or Whisper packages. Its weights are cached in `.cache/paddleocr` (ignored by Git). The first conversion downloads the models. Once installed, `--ocr-engine auto` also selects it for PDFs, images, and video frames. It processes every PDF page through OCR, including pages with a hidden text layer.

For Metal acceleration on Apple Silicon, install the optional MLX runtime once:

```bash
./setup-ocr.sh --extra metal
```

After setup, the normal command handles the OCR server and conversion:

```bash
./run.sh convert kurasi-arsip-2026
```

When PDF, image, or video inputs are present, `run.sh` starts the local Metal server, waits for the model, and shuts down the server after conversion. It reuses an already-ready server without shutting it down. Startup logs go to `.cache/paddleocr/server.log`. OCR loads its parsing models once per run and reuses them across images, PDF pages, and video frames. Routine OCR warnings and model messages go to `.cache/paddleocr/converter.log`; conversion errors still appear in the terminal. `doctor`, `prefetch`, and `--help` do not start a server. An explicit `--ocr-vlm-url` keeps server management in your hands; `--ocr-engine tesseract` selects the original pipeline. Without the optional Metal runtime, PaddleOCR uses CPU, which can be slow.

The parser keeps layout analysis in PaddleOCR and sends recognition crops to the loopback MLX service. Remote OCR endpoints are refused. Both the native CPU path and MLX acceleration passed local synthetic scan checks on this Mac; your document quality and processing time can differ.

For the legacy loose-file workflow, copy material into `../target/todo` and run `./run.sh run`. Originals stay in `todo`; the manifest at `../target/.manifest.json` tracks what has already been processed. Project exports belong in their own `input/` folder and use `convert NAME`.

## What it extracts

| Input | Local processor | Markdown content |
| --- | --- | --- |
| OPUS, MP3, WAV, M4A, and other audio | MLX-Whisper on Apple silicon | Timestamped speech transcript |
| MP4, MOV, MKV, and other video | Whisper + FFmpeg + selected OCR engine | Audio transcript and OCR from three representative frames |
| JPG, PNG, WebP, TIFF, and other images | PaddleOCR-VL-1.6 when installed; otherwise Tesseract | Dimensions and OCR text; structured document extraction with PaddleOCR |
| PDF | PaddleOCR-VL-1.6 when installed; otherwise Docling/MarkItDown | Markdown with page boundaries, tables, formulas, chart recognition, and optional figure descriptions |
| DOCX, PPTX, XLSX, HTML | Docling (layout model, TableFormer, Tesseract OCR) | Reading-order Markdown with tables and optional figure descriptions |
| EPUB, email, ZIP, legacy Office | Microsoft MarkItDown | Structured Markdown; ZIP contents are traversed |
| Markdown, text, JSON, CSV, source code | Built-in text reader | Verbatim fenced content |
| Unknown binary formats | Metadata only | Name, MIME type, size, timestamp, and checksum |

Every output has YAML front matter with its source path, SHA-256 checksum, processor, and model details. `combined.md` wraps each record in source boundary comments to preserve provenance.

This is a list of conversion routes, not a guarantee that every file with a listed extension will convert. Unknown formats get metadata only. Password-protected, damaged, or unreadable inputs can fail. Renaming an extension does not add support.

## Useful commands

```bash
# List projects, create a workspace, and digest its checkpoints
./run.sh projects
./run.sh new another-project --context 'Background and current goals'
./run.sh convert kurasi-arsip-2026
./run.sh convert kurasi-arsip-2026 --language id

# Original target folder
./run.sh run

# Download Docling models once (about 1 GB), so PDF runs work offline afterwards
./run.sh prefetch

# Complex scans: OCR every page even if a bad text layer exists
./run.sh --force-ocr

# Faster, less exact table model
./run.sh --table-mode fast

# Force or disable Docling
./run.sh --ocr-engine tesseract --doc-engine markitdown

# Original PDF/image pipeline, even after installing PaddleOCR
./run.sh --ocr-engine tesseract

# Verify dependencies without processing files
./run.sh doctor

# Reprocess everything
./run.sh --force

# Pin Indonesian speech instead of auto-detection
./run.sh --language id

# Use a smaller/faster MLX model
./run.sh --model mlx-community/whisper-small-mlx
```

To repeat the synthetic checkpoint simulation from this tool directory:

```sh
uv run python simulate.py
```

It runs the installed OCR pipeline on a generated image and two-page PDF, then checks duplicate ZIPs, newer and older overlaps, changed media, rejected ZIP members, missing-output recovery, and a fully cached rerun. Expected numeric text is checked separately: omissions are retained as OCR quality observations, even when the checkpoint workflow succeeds. The synthetic project, step logs, and Markdown/JSON report stay under `.cache/simulation/latest/`; the next simulation replaces that marked synthetic workspace. Real projects are not used for the simulation.

Project reading material is generated from `templates/project-readme.md`, `templates/project-agents.md`, and `templates/project-context.md` by `project_docs.py`. ZIP ingestion and checkpoint state belong to `projects.py`; WhatsApp timestamp/message parsing belongs to `whatsapp.py`. Existing project `CONTEXT.md` and customized `AGENTS.md` are preserved, while the managed README block refreshes on `new` or `convert`.

The quick setup targets Apple-silicon Macs. The converter also contains a Faster-Whisper backend for Linux, Intel Macs, and Windows, but that backend needs a separate `faster-whisper` installation. The default `whisper-turbo` model is a good speed/accuracy choice for multilingual voice notes. Use `--language id` if auto-detection confuses short Indonesian clips.

## Optional local image descriptions

OCR extracts visible words but cannot explain a photo. For scene descriptions, install and run [Ollama](https://ollama.com/download), then pull a local vision model:

```bash
ollama pull qwen3-vl:2b
./run.sh --vision-model qwen3-vl:2b
```

That adds a generated visual description alongside OCR. The same model also describes figure crops inside PDFs, DOCX, and PPTX files. OCR and visual interpretation remain separate in the output. The model is about 1.9 GB. The request goes only to the local loopback interface; no cloud API is configured. `qwen3-vl:2b` is the small setup option, not a claim of maximum vision accuracy.

## OCR languages

The current machine has English Tesseract data. That still reads Indonesian Latin text, but the Indonesian language pack can improve word recognition:

```bash
brew install tesseract-lang
./run.sh --ocr-langs eng+ind --force
```

## Folder contract

```text
this folder/
├── whatsapp-llm-digester/ reusable code and Python environment
└── target/
    ├── todo/              source files; never modified by the pipeline
    ├── markdown/          one Markdown file per source
    ├── combined.md        all successful records in filename order
    ├── .manifest.json     incremental-processing state
    └── .cache/            package and local Whisper-model downloads
```

`run` resolves `../target` from the tool's location. `new` and `convert` use the project root shown above. Neither depends on the terminal's current directory. You can call `run.sh` from anywhere. Flags supplied without a command, such as `./run.sh --force`, continue to target the legacy folder; use `convert NAME --force` for a project.

## Privacy & Security Architecture

This pipeline is built for zero external data exposure and local-first execution:

1. **Air-gapped by design during processing**:
   - **No cloud APIs**: Media, documents, audio, and images are never uploaded or streamed to external servers or remote LLMs.
   - **Local models only**: Speech transcription runs locally via Metal/MLX (`mlx-whisper`) or CPU/GPU (`faster-whisper`).
   - **Local vision option**: If `--vision-model` is specified, image analysis connects strictly to your local Ollama instance on `127.0.0.1:11434`.
2. **Network access boundaries**:
   - Internet connection is used **only** during initial installation and downloading model weights (e.g. Hugging Face weights for Whisper or `brew install`).
   - No analytics, telemetry, or phone-home requests are included.
3. **Data safety and provenance**:
   - Original source files in `todo/` are mounted read-only by the pipeline; they are never moved, modified, or deleted.
   - All generated Markdown files embed SHA-256 integrity checksums, timestamps, and model parameters in standard YAML front matter.
   - Intermediate temporary artifacts (like extracted video frames) are created in restricted temporary directories and automatically cleaned up upon completion.
4. **Target directory isolation**:
   - Processed data, transcripts, cache, and manifests reside outside the source code repository under `target/` to eliminate accidental git commits of sensitive chat records.

> [!CAUTION]
> **Handling Personal Identifiable Information (PII)**:
> WhatsApp exports typically contain phone numbers, private chat transcripts, personal names, locations, and media. The generated `combined.md` and Markdown files contain plaintext representations of this data. Ensure you do not commit `target/`, upload `combined.md` to public repositories, or send sensitive excerpts to public cloud services without proper consent or redaction.

## Limits

- OCR recognizes document content; arbitrary photo interpretation needs the optional local vision model. Figure crops without that model are explicitly marked as lacking a visual description. Handwriting, blur, fine print, and dense plots still need checks against the original.
- PaddleOCR is used for PDFs and images when its environment is installed. Its errors propagate instead of silently falling back to weaker extraction. Missing or duplicate PDF pages are rejected; pages without readable content are marked and listed in `blank_pages` metadata.
- Otherwise, Docling is used for PDFs; it also remains the converter for DOCX, PPTX, XLSX, and HTML. If automatic Docling conversion fails, it can fall back to MarkItDown and records `docling_fallback`. Figures without `--vision-model` appear as placeholders.
- PaddleOCR runs in one isolated worker that closes on completion or interruption. CPU inference and first-run downloads can take time; `--ocr-timeout` defaults to 3600 seconds per source, including model loading for the first source. A timed-out or crashed worker is stopped and replaced for the next source. No OCR model guarantees complete extraction or exact numbers from every scan.
- Docling downloads its models on first use (or via `./run.sh prefetch`) and is slower than MarkItDown, especially on large scanned PDFs.
- Speech-to-text can mishear names, numbers, and very short clips. Keep the original files and check important details against them.
- Video processing samples three frames; it does not describe every visual change.

See [RESEARCH.md](RESEARCH.md) for the tool comparison and source links.
