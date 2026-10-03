# Research notes

Checked on 25 September 2026. The goal was a private, quick local pipeline rather than the most elaborate document-understanding stack.

## PDF and image OCR update: 2 October 2026

The new quality route uses the complete **PaddleOCR-VL-1.6** pipeline. The developer reports 96.33 on OmniDocBench v1.6 and tests across scan, skew, warping, screen photography, and uneven lighting. This benchmark result is not a measured accuracy for this project's Indonesian documents. The compact 0.9B recognition model is a reasonable candidate for this Apple-silicon Mac with 16 GB RAM.

The full pipeline combines layout analysis and region recognition. Running the VLM alone does not reproduce it. This integration retains footnotes and marginal text, enables chart recognition, and uses the official Apple CPU runtime. A loopback MLX-VLM backend is optional for acceleration. The Apple guide reports validation on M4; compatibility and speed on another Mac still need local checks.

- [PaddleOCR-VL-1.6 technical documentation](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/algorithm/PaddleOCR-VL/PaddleOCR-VL-1.6.en.md)
- [Full pipeline and Python API](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/PaddleOCR-VL.en.md)
- [Official Apple Silicon setup](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/PaddleOCR-VL-Apple-Silicon.en.md)

Other candidates were checked before choosing this route:

| Candidate | Evidence and fit |
| --- | --- |
| GLM-OCR | Compact 0.9B parser with an official MLX deployment example. A viable alternative for local evaluation. |
| Jina-OCR-v1 | September 2026 release, 3B model. Its paper reports 91.14 on OmniDocBench v1.6 and emphasizes inference throughput. Newer release date alone does not establish better extraction for this workload. |
| olmOCR 2 | 7B model; its official local toolkit targets NVIDIA GPUs with at least 12 GB VRAM. Less convenient for this machine than the compact Paddle pipeline. |
| MinerU | Complete document parsing toolkit with local Apple support; worth comparing on scientific documents. Its pipeline and quality tiers differ from this integration. |
| Docling | Already installed here. Retained for Office/HTML and the original PDF route. Installing it does not replace the selected Tesseract OCR with a newer OCR VLM. |

- [GLM-OCR official MLX deployment](https://github.com/zai-org/GLM-OCR/blob/main/examples/mlx-deploy/README.md)
- [olmOCR official requirements and evaluation](https://github.com/allenai/olmocr)
- [MinerU official project](https://github.com/opendatalab/MinerU)
- [MLX-VLM supported models](https://github.com/Blaizzy/mlx-vlm)
- [Jina-OCR-v1 paper](https://arxiv.org/abs/2609.03181)

An independent September evaluation reported 95.25 for a different orchestration of the Paddle weights and asked about a formula-score discrepancy. The author explicitly declines a leaderboard ranking. Our settings also differ from the benchmark defaults because they retain footnotes and enable charts, so the published score must not be presented as this integration's accuracy.

- [OmniDocBench issue 258: independent evaluation and configuration differences](https://github.com/opendatalab/OmniDocBench/issues/258)

Forum searches were used to find practical concerns, not to establish benchmark rankings. A recent r/Rag comparison reports different winners for scans, scientific formulas, and general document extraction. A reported MLX-VLM continuous-batching failure with PaddleOCR motivated serial recognition requests in this integration. These reports support testing representative files; they do not prove one universal best model.

- [r/Rag: comparison of self-hosted OCR models](https://www.reddit.com/r/Rag/comments/1wmyoiz/best_open_source_ocr_models_to_replace_textract/)
- [MLX-VLM issue 1283: variable-batch PaddleOCR failure](https://github.com/Blaizzy/mlx-vlm/issues/1283)

### Local verification

On 2 October 2026, PaddleOCR 3.7.0 / PaddlePaddle 3.3.1 converted a raster-only synthetic Indonesian PDF through the native CPU pipeline. Exact names, invoice identifier, city, prices, and table structure matched the fixture. A second run through MLX-VLM 0.7.4 processed a two-page scanned PDF, a PNG document, and plain chat text into per-source Markdown, a manifest, and `combined.md` in 28.4 seconds with a loaded server and cached models. Source checksums stayed unchanged, and the next run skipped all three unchanged inputs.

The focused suite covers missing/duplicate pages, blank-page markers, retained footnotes, figure-link replacement, local endpoint restrictions, explicit dependency failure, OCR failure propagation, and cache invalidation. Project doctor passed with Metal access. The sandbox itself cannot load MLX's GPU device.

These checks are integration evidence. No comparison on the private WhatsApp corpus or production benchmark was run. Recognition of handwriting, equations, charts, and arbitrary photograph meaning was not measured by these fixtures; figure descriptions still require the separate optional vision model.

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
