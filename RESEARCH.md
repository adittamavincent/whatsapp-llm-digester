# Research notes

Checked on 25 September 2026. The goal was a private, quick local pipeline rather than the most elaborate document-understanding stack.

## Chosen stack

### MarkItDown for documents and archives

Microsoft describes MarkItDown as a lightweight converter focused on structured, token-efficient Markdown for LLM analysis. It supports PDF, Word, PowerPoint, Excel, images, audio, text formats, EPUB, and ZIP traversal. Its built-in video support is absent and its audio transcription is basic, so this project uses it only where it is strongest: documents and archives.

- [MarkItDown README](https://github.com/microsoft/markitdown/blob/main/README.md)

### MLX-Whisper on Apple silicon

Apple's MLX implementation runs Whisper through Metal and accepts local audio paths directly. Its official package supports timestamps, automatic language detection, and pre-converted Hugging Face models. The project selects `whisper-turbo` on Apple silicon and keeps Faster-Whisper as the cross-platform route.

- [MLX Whisper README](https://github.com/ml-explore/mlx-examples/blob/main/whisper/README.md)
- [Faster-Whisper README and benchmarks](https://github.com/SYSTRAN/faster-whisper)

Forum reports consistently favor the turbo model for fast multilingual batch transcription on Macs. A recurring warning is hallucinated text during silence, so this project enables silence/hallucination controls and disables previous-text conditioning. Faster-Whisper's non-Mac path also enables VAD.

- [LocalLLaMA discussion: Whisper Turbo on M1 Pro](https://www.reddit.com/r/LocalLLaMA/comments/1fvb83n/open_ais_new_whisper_turbo_model_runs_54_times/)
- [LocalLLaMA discussion: local transcription and VAD](https://www.reddit.com/r/LocalLLaMA/comments/1rkuzfy/local_transcription/)

### Tesseract plus optional Ollama vision

Tesseract is small and deterministic for visible text. A local vision-language model is optional because its multi-gigabyte download conflicts with a quick base setup. `qwen3-vl:2b` is the smallest current Ollama Qwen3-VL image model (about 1.9 GB) and supports OCR plus scene understanding.

- [Ollama Qwen3-VL model page](https://ollama.com/library/qwen3-vl)

## Docling (now used for layout-heavy documents)

Docling was originally deferred. It is now the default converter for PDF, DOCX, PPTX, XLSX, and HTML because tables, scans, and figures need layout analysis. MarkItDown remains the fallback and handles the other formats.

## Original assessment of Docling

Docling now has broad local support for PDFs, Office files, images, audio, and video, with strong layout, table, OCR, and representative-frame handling. It is the better upgrade when complex scanned documents are the main workload. For this mixed WhatsApp export, the heavier dependency and model footprint were unnecessary because dedicated Whisper handles the dominant audio workload and MarkItDown handles the small document set.

- [Docling README](https://github.com/docling-project/docling)
- [Forum comparison of PDF/DOCX-to-Markdown tools](https://www.reddit.com/r/Rag/comments/1sl515w/tools_for_working_with_docdocx_and_pdf_files/)

## Privacy & security boundaries

The pipeline is designed specifically for sensitive personal or corporate chat histories:
- **Zero cloud API usage**: The core pipeline relies exclusively on local binaries (`ffmpeg`, `ffprobe`, `tesseract`) and local Python packages. Files are never uploaded or streamed to any remote SaaS service.
- **Offline operation**: The only network usage occurs during initial installation (`uv sync`, downloading Whisper weights from Hugging Face). Once cached in `.cache/`, the pipeline can run completely offline.
- **Loopback-only vision model**: When `--vision-model` is enabled, HTTP calls are restricted to the local loopback address (`http://127.0.0.1:11434`), eliminating third-party model leaks.
- **Immutability of originals**: Source inputs in `todo/` are accessed in read-only mode to prevent accidental corruption or data loss.
- **Strict storage boundary**: Output and cache paths are segregated into `target/` outside the code repository, guarding against inadvertent commits of private data to version control.
