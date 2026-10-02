#!/usr/bin/env python3
"""Convert ../target/todo into per-source Markdown and one combined corpus."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import logging
import mimetypes
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


PIPELINE_VERSION = 2
PROJECT_DIR = Path(__file__).resolve().parent
TARGET_DIR = PROJECT_DIR.parent / "target"
TODO_DIR = TARGET_DIR / "todo"
OUTPUT_DIR = TARGET_DIR / "markdown"
COMBINED_PATH = TARGET_DIR / "combined.md"
MANIFEST_PATH = TARGET_DIR / ".manifest.json"

# Docling reads its settings at import time, so keep its model cache beside the other caches.
os.environ.setdefault("DOCLING_CACHE_DIR", str(TARGET_DIR / ".cache" / "docling"))

IMAGE_EXTENSIONS = {".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
AUDIO_EXTENSIONS = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"}
VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}
PLAIN_TEXT_EXTENSIONS = {
    ".cfg", ".conf", ".css", ".csv", ".ini", ".js", ".json", ".jsonl", ".log",
    ".md", ".py", ".rst", ".sh", ".sql", ".toml", ".ts", ".tsv", ".txt", ".yaml", ".yml",
}
DOCUMENT_EXTENSIONS = {
    ".doc", ".docx", ".eml", ".epub", ".htm", ".html", ".msg", ".odt", ".pdf",
    ".ppt", ".pptx", ".rtf", ".xls", ".xlsx", ".xml", ".zip",
}
# Layout-aware formats that Docling handles better than MarkItDown (tables, scans, figures).
DOCLING_EXTENSIONS = {".docx", ".htm", ".html", ".pdf", ".pptx", ".xlsx"}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Digest ../target/todo into Markdown for LLMs. Sources are never deleted or moved."
    )
    parser.add_argument("command", nargs="?", choices=("run", "doctor", "prefetch"), default="run")
    parser.add_argument("--force", action="store_true", help="Reprocess files even when unchanged.")
    parser.add_argument("--backend", choices=("auto", "mlx", "faster-whisper"), default="auto")
    parser.add_argument(
        "--model",
        default="auto",
        help="Whisper model. Auto uses mlx-community/whisper-turbo on Apple silicon or turbo elsewhere.",
    )
    parser.add_argument("--language", default="auto", help="Speech language code, for example id or en; default auto-detects.")
    parser.add_argument("--ocr-langs", default="auto", help="Tesseract languages, for example eng+ind.")
    parser.add_argument(
        "--vision-model",
        default=None,
        help="Optional Ollama vision model, for example qwen3-vl:2b. Ollama must already be running.",
    )
    parser.add_argument(
        "--ollama-url",
        default="http://127.0.0.1:11434/api/chat",
        help="Local Ollama chat endpoint used only with --vision-model.",
    )
    parser.add_argument(
        "--doc-engine",
        choices=("auto", "docling", "markitdown"),
        default="auto",
        help="Document converter. Auto uses Docling for PDF/DOCX/PPTX/XLSX/HTML when installed, else MarkItDown.",
    )
    parser.add_argument(
        "--force-ocr",
        action="store_true",
        help="OCR every PDF page even when it has a text layer. Use for scans with a garbled hidden text layer.",
    )
    parser.add_argument(
        "--table-mode",
        choices=("accurate", "fast"),
        default="accurate",
        help="Docling table-structure model. Accurate is slower and handles merged cells and dense grids better.",
    )
    return parser.parse_args(argv)


def ensure_layout() -> None:
    TODO_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (TARGET_DIR / ".cache").mkdir(parents=True, exist_ok=True)


def run_command(command: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iso_mtime(path: Path) -> str:
    return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc).isoformat()


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def format_timestamp(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def classify(path: Path) -> str:
    extension = path.suffix.lower()
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    if extension in PLAIN_TEXT_EXTENSIONS:
        return "text"
    if extension in DOCUMENT_EXTENSIONS:
        return "document"
    return "unsupported"


def available_tesseract_languages() -> list[str]:
    if not shutil.which("tesseract"):
        return []
    result = run_command(["tesseract", "--list-langs"], timeout=30)
    return [line.strip() for line in result.stdout.splitlines()[1:] if line.strip()]


def resolve_ocr_languages(requested: str) -> str:
    available = set(available_tesseract_languages())
    if requested != "auto":
        missing = set(requested.split("+")) - available
        if missing:
            raise RuntimeError(f"Tesseract language data is missing: {', '.join(sorted(missing))}")
        return requested
    choices = [language for language in ("eng", "ind") if language in available]
    if not choices and available:
        choices = [sorted(available)[0]]
    if not choices:
        raise RuntimeError("Tesseract is installed but no OCR languages were found")
    return "+".join(choices)


def image_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return image.size
    except Exception:
        return None


def image_ocr(path: Path, languages: str) -> str:
    result = run_command(
        ["tesseract", str(path), "stdout", "-l", languages, "--psm", "11"],
        timeout=300,
    )
    return result.stdout.strip()


def strip_thinking(text: str) -> str:
    while "<think>" in text and "</think>" in text:
        before, rest = text.split("<think>", 1)
        _, after = rest.split("</think>", 1)
        text = before + after
    return text.strip()


def ollama_describe(path: Path, model: str, endpoint: str) -> str:
    prompt = (
        "Describe this image for a later language model. State the visible people, objects, setting, "
        "actions, layout, and every readable word. Preserve names and numbers exactly. Separate direct "
        "observations from uncertainty. Do not invent hidden context."
    )
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [base64.b64encode(path.read_bytes()).decode("ascii")],
            }
        ],
        "options": {"temperature": 0},
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Ollama request failed at {endpoint}: {exc}") from exc
    return strip_thinking(result["message"]["content"])


class SpeechTranscriber:
    def __init__(self, backend: str, model: str, language: str) -> None:
        self.backend = self._resolve_backend(backend)
        self.model_name = self._resolve_model(model)
        self.language = None if language == "auto" else language
        self._model: Any = None

    @staticmethod
    def _resolve_backend(backend: str) -> str:
        if backend != "auto":
            return backend
        if platform.system() == "Darwin" and platform.machine() == "arm64":
            return "mlx"
        return "faster-whisper"

    def _resolve_model(self, model: str) -> str:
        if model != "auto":
            return model
        if self.backend == "mlx":
            return "mlx-community/whisper-turbo"
        return "turbo"

    def transcribe(self, path: Path) -> tuple[str, dict[str, Any]]:
        if self.backend == "mlx":
            return self._transcribe_mlx(path)
        return self._transcribe_faster_whisper(path)

    def _transcribe_mlx(self, path: Path) -> tuple[str, dict[str, Any]]:
        import mlx_whisper

        result = mlx_whisper.transcribe(
            str(path),
            path_or_hf_repo=self.model_name,
            language=self.language,
            verbose=False,
            word_timestamps=False,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            hallucination_silence_threshold=2.0,
        )
        lines = []
        for segment in result.get("segments", []):
            text = segment.get("text", "").strip()
            if text:
                lines.append(
                    f"[{format_timestamp(float(segment['start']))} - {format_timestamp(float(segment['end']))}] {text}"
                )
        metadata = {
            "speech_backend": "mlx-whisper",
            "speech_model": self.model_name,
            "detected_language": result.get("language", self.language or "unknown"),
        }
        return "\n\n".join(lines) or "_No speech detected._", metadata

    def _transcribe_faster_whisper(self, path: Path) -> tuple[str, dict[str, Any]]:
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
        segments, info = self._model.transcribe(
            str(path),
            language=self.language,
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
        )
        lines = []
        for segment in segments:
            text = segment.text.strip()
            if text:
                lines.append(
                    f"[{format_timestamp(segment.start)} - {format_timestamp(segment.end)}] {text}"
                )
        metadata = {
            "speech_backend": "faster-whisper",
            "speech_model": self.model_name,
            "detected_language": getattr(info, "language", self.language or "unknown"),
            "language_probability": round(float(getattr(info, "language_probability", 0.0)), 4),
        }
        return "\n\n".join(lines) or "_No speech detected._", metadata


def docling_available() -> bool:
    try:
        import docling  # noqa: F401
    except ImportError:
        return False
    return True


def loopback_chat_url(ollama_url: str) -> str:
    """Return Ollama's OpenAI-compatible chat URL, refusing any non-loopback host."""
    parts = urllib.parse.urlsplit(ollama_url)
    if parts.hostname not in LOOPBACK_HOSTS:
        raise RuntimeError(f"Refusing non-loopback vision endpoint: {ollama_url}")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/v1/chat/completions", "", ""))


def _drop_osd_noise(record: logging.LogRecord) -> bool:
    # Docling runs Tesseract orientation detection on every OCR crop. Crops with little text (logos,
    # stamps, rules) fail it, but OCR still runs because we always pass explicit languages.
    return not record.getMessage().startswith("OSD failed")


class DoclingConverter:
    """Layout-aware conversion: reading order, table structure, scanned-page OCR, figure descriptions."""

    def __init__(self, args: argparse.Namespace, ocr_languages: str) -> None:
        self.args = args
        self.ocr_languages = ocr_languages
        self._converter: Any = None

    @staticmethod
    def version() -> str:
        from importlib.metadata import version

        return version("docling")

    def _build(self) -> Any:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import (
            PdfPipelineOptions,
            PictureDescriptionApiOptions,
            TableFormerMode,
            TesseractCliOcrOptions,
        )
        from docling.document_converter import DocumentConverter, PdfFormatOption

        logging.getLogger("docling.models.stages.ocr.tesseract_ocr_cli_model").addFilter(_drop_osd_noise)
        options = PdfPipelineOptions()
        options.do_ocr = True
        options.ocr_options = TesseractCliOcrOptions(
            lang=self.ocr_languages.split("+"), force_full_page_ocr=self.args.force_ocr
        )
        options.do_table_structure = True
        options.table_structure_options.mode = (
            TableFormerMode.ACCURATE if self.args.table_mode == "accurate" else TableFormerMode.FAST
        )
        options.table_structure_options.do_cell_matching = True
        if self.args.vision_model:
            options.enable_remote_services = True  # loopback Ollama only; see loopback_chat_url
            options.do_picture_description = True
            options.picture_description_options = PictureDescriptionApiOptions(
                url=loopback_chat_url(self.args.ollama_url),
                params={"model": self.args.vision_model, "temperature": 0},
                prompt=(
                    "Describe this figure for a later language model. If it is a chart or diagram, state its "
                    "type, title, axes, series, and every readable label and number exactly. If it is a photo, "
                    "state the visible objects and text. Do not invent details."
                ),
                timeout=600,
            )
        format_options = {InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
        return DocumentConverter(format_options=format_options)

    def convert(self, path: Path) -> tuple[str, dict[str, Any]]:
        from docling.datamodel.base_models import ConversionStatus

        if self._converter is None:
            self._converter = self._build()
        result = self._converter.convert(str(path), raises_on_error=False)
        if result.status not in (ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS):
            errors = "; ".join(str(getattr(error, "error_message", error)) for error in result.errors)
            raise RuntimeError(f"Docling status {result.status.value}: {errors or 'no details'}")
        document = result.document
        pages = sorted(document.pages)
        if len(pages) > 1:
            chunks = []
            for number in pages:
                text = document.export_to_markdown(page_no=number).strip()
                if text:
                    chunks.append(f"<!-- page {number} -->\n\n{text}")
            markdown = "\n\n".join(chunks)
        else:
            markdown = document.export_to_markdown().strip()
        metadata: dict[str, Any] = {
            "processor": "docling",
            "docling_version": self.version(),
            "docling_status": result.status.value,
            "pages": len(pages),
            "tables": len(document.tables),
            "pictures": len(document.pictures),
            "ocr_languages": self.ocr_languages,
            "table_mode": self.args.table_mode,
        }
        if self.args.vision_model:
            metadata["vision_model"] = self.args.vision_model
        return markdown, metadata


class Converter:
    def __init__(self, args: argparse.Namespace, ocr_languages: str) -> None:
        self.args = args
        self.ocr_languages = ocr_languages
        self.speech = SpeechTranscriber(args.backend, args.model, args.language)
        self.docling: DoclingConverter | None = None
        if args.doc_engine == "docling" and not docling_available():
            raise RuntimeError("--doc-engine docling requested but docling is not installed")
        if args.doc_engine != "markitdown" and docling_available():
            self.docling = DoclingConverter(args, ocr_languages)
        self._markitdown: Any = None

    def convert(self, path: Path, kind: str) -> tuple[str, dict[str, Any]]:
        if kind == "image":
            return self.convert_image(path)
        if kind == "audio":
            transcript, metadata = self.speech.transcribe(path)
            return f"## Transcript\n\n{transcript}", metadata
        if kind == "video":
            return self.convert_video(path)
        if kind == "text":
            return self.convert_text(path), {"processor": "plain-text"}
        if kind == "document":
            return self.convert_document(path)
        return (
            "## Conversion status\n\nThis file type is not supported. The source metadata is preserved above.",
            {"processor": "metadata-only"},
        )

    def convert_image(self, path: Path) -> tuple[str, dict[str, Any]]:
        dimensions = image_dimensions(path)
        ocr = image_ocr(path, self.ocr_languages)
        sections = []
        if self.args.vision_model:
            description = ollama_describe(path, self.args.vision_model, self.args.ollama_url)
            sections.append(f"## Local vision description\n\n{description or '_No description returned._'}")
        sections.append(f"## OCR text\n\n{ocr or '_No readable text detected._'}")
        metadata: dict[str, Any] = {
            "processor": "tesseract" + ("+ollama" if self.args.vision_model else ""),
            "ocr_languages": self.ocr_languages,
        }
        if dimensions:
            metadata["width_px"], metadata["height_px"] = dimensions
        if self.args.vision_model:
            metadata["vision_model"] = self.args.vision_model
        return "\n\n".join(sections), metadata

    def convert_text(self, path: Path) -> str:
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig", errors="replace").strip()
        if path.suffix.lower() == ".md":
            return f"## Source content\n\n{text}"
        language = path.suffix.lower().lstrip(".") or "text"
        fence = "````" if "```" in text else "```"
        return f"## Source content\n\n{fence}{language}\n{text}\n{fence}"

    def convert_document(self, path: Path) -> tuple[str, dict[str, Any]]:
        fallback: dict[str, Any] = {}
        if self.docling and path.suffix.lower() in DOCLING_EXTENSIONS:
            try:
                markdown, metadata = self.docling.convert(path)
                if markdown:
                    return f"## Extracted content\n\n{markdown}", metadata
                fallback["docling_fallback"] = "Docling returned no text"
            except Exception as exc:
                if self.args.doc_engine == "docling":
                    raise
                print(f"  Docling failed, falling back to MarkItDown: {type(exc).__name__}: {exc}", file=sys.stderr)
                fallback["docling_fallback"] = f"{type(exc).__name__}: {exc}"
        return self.convert_markitdown(path), {"processor": "markitdown", **fallback}

    def convert_markitdown(self, path: Path) -> str:
        if self._markitdown is None:
            from markitdown import MarkItDown

            self._markitdown = MarkItDown()
        result = self._markitdown.convert_local(str(path))
        markdown = getattr(result, "markdown", None) or getattr(result, "text_content", None)
        if not markdown or not markdown.strip():
            return "## Extracted content\n\n_No text was extracted from this document._"
        return f"## Extracted content\n\n{markdown.strip()}"

    def convert_video(self, path: Path) -> tuple[str, dict[str, Any]]:
        transcript, metadata = self.speech.transcribe(path)
        sections = [f"## Audio transcript\n\n{transcript}"]
        frames = self.extract_video_frames(path)
        try:
            if frames:
                frame_sections = []
                for timestamp, frame in frames:
                    ocr = image_ocr(frame, self.ocr_languages)
                    detail = f"OCR: {ocr}" if ocr else "OCR: no readable text detected."
                    if self.args.vision_model:
                        detail = ollama_describe(frame, self.args.vision_model, self.args.ollama_url) + f"\n\n{detail}"
                    frame_sections.append(f"### Frame at {format_timestamp(timestamp)}\n\n{detail}")
                sections.append("## Representative video frames\n\n" + "\n\n".join(frame_sections))
        finally:
            if frames:
                shutil.rmtree(frames[0][1].parent, ignore_errors=True)
        metadata.update(
            {
                "processor": metadata["speech_backend"] + "+ffmpeg+tesseract",
                "ocr_languages": self.ocr_languages,
                "sampled_frames": len(frames),
            }
        )
        if self.args.vision_model:
            metadata["vision_model"] = self.args.vision_model
        return "\n\n".join(sections), metadata

    @staticmethod
    def extract_video_frames(path: Path) -> list[tuple[float, Path]]:
        try:
            probe = run_command(
                [
                    "ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(path),
                ],
                timeout=60,
            )
            duration = float(probe.stdout.strip())
        except (ValueError, subprocess.SubprocessError):
            return []
        timestamps = sorted({round(duration * ratio, 3) for ratio in (0.2, 0.5, 0.8) if duration > 0})
        frame_dir = Path(tempfile.mkdtemp(prefix="whatsapp-llm-digester-frames-"))
        frames: list[tuple[float, Path]] = []
        for index, timestamp in enumerate(timestamps, start=1):
            frame = frame_dir / f"frame-{index}.jpg"
            try:
                run_command(
                    [
                        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(timestamp),
                        "-i", str(path), "-frames:v", "1", "-q:v", "2", str(frame),
                    ],
                    timeout=120,
                )
            except subprocess.SubprocessError:
                continue
            if frame.exists():
                frames.append((timestamp, frame))
        if not frames:
            shutil.rmtree(frame_dir, ignore_errors=True)
        return frames


def output_path_for(source: Path) -> Path:
    relative = source.relative_to(TODO_DIR)
    return OUTPUT_DIR / relative.parent / f"{relative.name}.md"


def render_markdown(source: Path, kind: str, digest: str, body: str, extra: dict[str, Any]) -> str:
    relative = source.relative_to(TODO_DIR).as_posix()
    mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    metadata: dict[str, Any] = {
        "source": f"todo/{relative}",
        "source_type": kind,
        "mime_type": mime,
        "size_bytes": source.stat().st_size,
        "modified_utc": iso_mtime(source),
        "sha256": digest,
    }
    metadata.update(extra)
    frontmatter = ["---"]
    for key, value in metadata.items():
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (int, float)):
            rendered = str(value)
        else:
            rendered = yaml_string(str(value))
        frontmatter.append(f"{key}: {rendered}")
    frontmatter.extend(["---", "", f"# {source.name}", "", body.strip(), ""])
    return "\n".join(frontmatter)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def load_manifest() -> dict[str, Any]:
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"pipeline_version": PIPELINE_VERSION, "files": {}}


def save_manifest(manifest: dict[str, Any]) -> None:
    atomic_write(MANIFEST_PATH, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def config_key(args: argparse.Namespace, converter: Converter) -> dict[str, Any]:
    return {
        "pipeline_version": PIPELINE_VERSION,
        "backend": converter.speech.backend,
        "model": converter.speech.model_name,
        "language": args.language,
        "ocr_languages": converter.ocr_languages,
        "vision_model": args.vision_model,
        "doc_engine": "docling" if converter.docling else "markitdown",
        "docling_version": converter.docling.version() if converter.docling else None,
        "force_ocr": args.force_ocr,
        "table_mode": args.table_mode,
    }


def combine_outputs(records: dict[str, Any]) -> int:
    parts = [
        "# LLM digestion corpus",
        "",
        f"Generated: {dt.datetime.now(tz=dt.timezone.utc).isoformat()}",
        "",
        "Each source begins and ends with an HTML comment so an LLM can preserve provenance.",
        "",
    ]
    count = 0
    for relative, record in sorted(records.items()):
        if record.get("status") != "success":
            continue
        output = TARGET_DIR / record["output"]
        if not output.is_file():
            continue
        parts.extend(
            [
                f"<!-- BEGIN SOURCE: todo/{relative} -->",
                "",
                output.read_text(encoding="utf-8").strip(),
                "",
                f"<!-- END SOURCE: todo/{relative} -->",
                "",
            ]
        )
        count += 1
    parts.insert(4, f"Sources included: {count}")
    parts.insert(5, "")
    atomic_write(COMBINED_PATH, "\n".join(parts).rstrip() + "\n")
    return count


def doctor() -> int:
    ensure_layout()
    failures = []
    print(f"Project: {PROJECT_DIR}")
    print(f"Target:  {TARGET_DIR}")
    for tool in ("ffmpeg", "ffprobe", "tesseract"):
        location = shutil.which(tool)
        print(f"{tool}: {location or 'MISSING'}")
        if not location:
            failures.append(tool)
    languages = available_tesseract_languages() if shutil.which("tesseract") else []
    print(f"Tesseract languages: {', '.join(languages) or 'none'}")
    if "ind" not in languages:
        print("Note: Indonesian OCR data is absent; English OCR still recognizes Latin text.")
    try:
        import markitdown  # noqa: F401

        print("MarkItDown: installed")
    except ImportError:
        print("MarkItDown: MISSING")
        failures.append("markitdown")
    if docling_available():
        print(f"Docling: {DoclingConverter.version()} (layout, tables, scanned-page OCR)")
    else:
        print("Docling: not installed (optional; PDFs fall back to MarkItDown)")
    speech_module = "mlx_whisper" if platform.system() == "Darwin" and platform.machine() == "arm64" else "faster_whisper"
    try:
        __import__(speech_module)
        print(f"Speech backend: {speech_module} installed")
    except ImportError:
        print(f"Speech backend: {speech_module} MISSING")
        failures.append(speech_module)
    print(f"Ollama: {shutil.which('ollama') or 'not installed (optional)'}")
    if failures:
        print(f"Doctor failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("Doctor passed.")
    return 0


def prefetch() -> int:
    """Download Docling layout and table models into target/.cache so later runs work offline."""
    if not docling_available():
        print("Docling is not installed.", file=sys.stderr)
        return 1
    from docling.utils.model_downloader import download_models

    ensure_layout()
    path = download_models(with_layout=True, with_tableformer=True, with_code_formula=False,
                           with_picture_classifier=False, with_rapidocr=False, progress=True)
    print(f"Docling models ready: {path}")
    return 0


def process(args: argparse.Namespace) -> int:
    ensure_layout()
    ocr_languages = resolve_ocr_languages(args.ocr_langs)
    converter = Converter(args, ocr_languages)
    current_config = config_key(args, converter)
    manifest = load_manifest()
    records: dict[str, Any] = manifest.setdefault("files", {})
    sources = sorted(
        path for path in TODO_DIR.rglob("*") if path.is_file() and not path.is_symlink() and path.name != ".DS_Store"
    )
    if not sources:
        print(f"No files found in {TODO_DIR}")
        combine_outputs(records)
        return 0

    succeeded = skipped = failed = 0
    for index, source in enumerate(sources, start=1):
        relative = source.relative_to(TODO_DIR).as_posix()
        digest = sha256(source)
        output = output_path_for(source)
        previous = records.get(relative, {})
        unchanged = (
            not args.force
            and previous.get("status") == "success"
            and previous.get("sha256") == digest
            and previous.get("config") == current_config
            and output.is_file()
        )
        if unchanged:
            print(f"[{index}/{len(sources)}] skip {relative}")
            skipped += 1
            continue
        kind = classify(source)
        print(f"[{index}/{len(sources)}] {kind}: {relative}", flush=True)
        try:
            body, extra = converter.convert(source, kind)
            atomic_write(output, render_markdown(source, kind, digest, body, extra))
            records[relative] = {
                "status": "success",
                "sha256": digest,
                "output": output.relative_to(TARGET_DIR).as_posix(),
                "config": current_config,
                "processed_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
            }
            succeeded += 1
        except Exception as exc:
            print(f"  ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            records[relative] = {
                "status": "failed",
                "sha256": digest,
                "output": output.relative_to(TARGET_DIR).as_posix(),
                "config": current_config,
                "error": f"{type(exc).__name__}: {exc}",
                "processed_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
            }
            failed += 1
        save_manifest(manifest)

    included = combine_outputs(records)
    print()
    print(f"Done: {succeeded} processed, {skipped} unchanged, {failed} failed, {included} in combined.md")
    print(f"Combined corpus: {COMBINED_PATH}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.command == "doctor":
        return doctor()
    if args.command == "prefetch":
        return prefetch()
    return process(args)


if __name__ == "__main__":
    raise SystemExit(main())
