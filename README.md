# whatsapp-llm-digester

This project converts files from `../target/todo` into provenance-rich Markdown without uploading the source files. It writes one file per source under `../target/markdown` and rebuilds `../target/combined.md` for direct use with an LLM.

## Quick start

```bash
cd whatsapp-llm-digester
./setup.sh
./run.sh
```

The first transcription run downloads a local Whisper model. Later runs reuse the model and skip unchanged inputs.

To add more material, copy it into `../target/todo` and run `./run.sh` again. Originals stay in `todo`; the manifest at `../target/.manifest.json` tracks what has already been processed.

## What it extracts

| Input | Local processor | Markdown content |
| --- | --- | --- |
| OPUS, MP3, WAV, M4A, and other audio | MLX-Whisper on Apple silicon | Timestamped speech transcript |
| MP4, MOV, MKV, and other video | Whisper + FFmpeg + Tesseract | Audio transcript and OCR from three representative frames |
| JPG, PNG, WebP, TIFF, and other images | Tesseract | Dimensions and OCR text |
| PDF, DOCX, PPTX, XLSX, HTML | Docling (layout model, TableFormer, Tesseract OCR) | Reading-order Markdown with real tables, scanned-page OCR, `<!-- page N -->` markers, optional figure descriptions |
| EPUB, email, ZIP, legacy Office | Microsoft MarkItDown | Structured Markdown; ZIP contents are traversed |
| Markdown, text, JSON, CSV, source code | Built-in text reader | Verbatim fenced content |
| Unknown binary formats | Metadata only | Name, MIME type, size, timestamp, and checksum |

Every output has YAML front matter with its source path, SHA-256 checksum, processor, and model details. `combined.md` wraps each record in source boundary comments to preserve provenance.

## Useful commands

```bash
# Download Docling models once (about 1 GB), so PDF runs work offline afterwards
./run.sh prefetch

# Complex scans: OCR every page even if a bad text layer exists
./run.sh --force-ocr

# Faster, less exact table model
./run.sh --table-mode fast

# Force or disable Docling
./run.sh --doc-engine markitdown

# Verify dependencies without processing files
./run.sh doctor

# Reprocess everything
./run.sh --force

# Pin Indonesian speech instead of auto-detection
./run.sh --language id

# Use a smaller/faster MLX model
./run.sh --model mlx-community/whisper-small-mlx
```

The quick setup targets Apple-silicon Macs. The converter also contains a Faster-Whisper backend for Linux, Intel Macs, and Windows, but that backend needs a separate `faster-whisper` installation. The default `whisper-turbo` model is a good speed/accuracy choice for multilingual voice notes. Use `--language id` if auto-detection confuses short Indonesian clips.

## Optional local image descriptions

OCR extracts visible words but cannot explain a photo. For scene descriptions, install and run [Ollama](https://ollama.com/download), then pull a local vision model:

```bash
ollama pull qwen3-vl:2b
./run.sh --vision-model qwen3-vl:2b
```

That adds a factual visual description alongside Tesseract OCR. The same model also describes figures and charts inside PDFs, DOCX, and PPTX files. The model is about 1.9 GB. The request goes only to `127.0.0.1`; no cloud API is configured.

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

The code resolves `../target` from its own location, not from the terminal's current directory. You can call `run.sh` from anywhere.

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

- Tesseract-only image processing extracts text but does not infer the scene. Enable the optional Ollama vision model when image meaning matters.
- Docling is used for PDF, DOCX, PPTX, XLSX, and HTML. If it fails on a file, the pipeline falls back to MarkItDown and records `docling_fallback` in the front matter. Figures without `--vision-model` appear as `<!-- image -->` placeholders.
- Docling downloads its models on first use (or via `./run.sh prefetch`) and is slower than MarkItDown, especially on large scanned PDFs.
- Speech-to-text can mishear names, numbers, and very short clips. Keep the original files and check important details against them.
- Video processing samples three frames; it does not describe every visual change.

See [RESEARCH.md](RESEARCH.md) for the tool comparison and source links.
