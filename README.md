# Local LLM digester

This project converts files from `../target/todo` into provenance-rich Markdown without uploading the source files. It writes one file per source under `../target/markdown` and rebuilds `../target/combined.md` for direct use with an LLM.

## Quick start

```bash
cd llm-digester
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
| PDF, DOCX, PPTX, XLSX, HTML, EPUB, email, ZIP | Microsoft MarkItDown | Structured Markdown; ZIP contents are traversed |
| Markdown, text, JSON, CSV, source code | Built-in text reader | Verbatim fenced content |
| Unknown binary formats | Metadata only | Name, MIME type, size, timestamp, and checksum |

Every output has YAML front matter with its source path, SHA-256 checksum, processor, and model details. `combined.md` wraps each record in source boundary comments to preserve provenance.

## Useful commands

```bash
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

That adds a factual visual description alongside Tesseract OCR. The model is about 1.9 GB. The request goes only to `127.0.0.1`; no cloud API is configured.

## OCR languages

The current machine has English Tesseract data. That still reads Indonesian Latin text, but the Indonesian language pack can improve word recognition:

```bash
brew install tesseract-lang
./run.sh --ocr-langs eng+ind --force
```

## Folder contract

```text
this folder/
├── llm-digester/       reusable code and Python environment
└── target/
    ├── todo/           source files; never modified by the pipeline
    ├── markdown/       one Markdown file per source
    ├── combined.md     all successful records in filename order
    ├── .manifest.json  incremental-processing state
    └── .cache/         package and local Whisper-model downloads
```

The code resolves `../target` from its own location, not from the terminal's current directory. You can call `run.sh` from anywhere.

## Limits

- Tesseract-only image processing extracts text but does not infer the scene. Enable the optional Ollama vision model when image meaning matters.
- MarkItDown favors token-efficient LLM input over pixel-perfect document reproduction. Complex scanned PDFs and tables may need a heavier Docling pass.
- Speech-to-text can mishear names, numbers, and very short clips. Keep the original files and check important details against them.
- Video processing samples three frames; it does not describe every visual change.

See [RESEARCH.md](RESEARCH.md) for the tool comparison and source links.
