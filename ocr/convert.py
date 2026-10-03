"""Run the complete PaddleOCR-VL pipeline in its own dependency environment."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit

PIPELINE = "v1.6"
MODEL = "PaddlePaddle/PaddleOCR-VL-1.6"


def pipeline_options(vlm_url: str | None) -> dict:
    options = {
        "pipeline_version": PIPELINE,
        "device": "cpu",
        "use_layout_detection": True,
        "use_doc_orientation_classify": True,
        "use_doc_unwarping": True,
        "use_chart_recognition": True,
        "use_seal_recognition": True,
        "use_ocr_for_image_block": True,
        # Retain footnotes, page numbers, headers, and marginal text for the corpus.
        "markdown_ignore_labels": [],
        "format_block_content": True,
        "use_queues": False,
    }
    if vlm_url:
        parts = urlsplit(vlm_url)
        if parts.scheme != "http" or parts.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError(
                "The MLX-VLM endpoint must use HTTP on the local loopback address"
            )
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError(
                "The MLX-VLM URL must not contain credentials, a query, or a fragment"
            )
        options.update(
            vl_rec_backend="mlx-vlm-server",
            vl_rec_server_url=vlm_url,
            vl_rec_api_model_name=MODEL,
            vl_rec_max_concurrency=1,
        )
    return options


def collect_pages(results, assets_dir: Path) -> dict:
    pages: dict[int, str] = {}
    figures = []
    expected_pages = None
    counts = {"tables": 0, "formulas": 0, "charts": 0, "pictures": 0}
    blank_pages = []
    labels = {
        "table": "tables",
        "display_formula": "formulas",
        "inline_formula": "formulas",
        "chart": "charts",
        "image": "pictures",
    }
    for result in results:
        if result.get("error"):
            raise RuntimeError(f"PaddleOCR failed: {result['error']}")
        index = result.get("page_index")
        number = int(index) + 1 if index is not None else len(pages) + 1
        if number in pages:
            raise RuntimeError(f"PaddleOCR returned page {number} twice")
        page_count = result.get("page_count")
        if page_count is not None:
            if expected_pages is not None and int(page_count) != expected_pages:
                raise RuntimeError("PaddleOCR returned inconsistent page counts")
            expected_pages = int(page_count)
        exported = result.markdown
        markdown = exported["markdown_texts"].strip()
        for name, image in exported.get("markdown_images", {}).items():
            figure_number = len(figures) + 1
            asset = assets_dir / f"page-{number}-figure-{figure_number}.png"
            image.save(asset)
            figures.append(
                {"page": number, "number": figure_number, "path": str(asset)}
            )
            # Temporary crops are consumed by local vision; never leave broken image links in text.
            pattern = r"!\[[^\]]*\]\(" + re.escape(name) + r"\)"
            markdown = re.sub(
                pattern, f"_[Figure {figure_number} on page {number}]_", markdown
            )
            html_pattern = r"<img\b[^>]*\bsrc=[\"']" + re.escape(name) + r"[\"'][^>]*>"
            markdown = re.sub(
                html_pattern,
                f"_[Figure {figure_number} on page {number}]_",
                markdown,
                flags=re.IGNORECASE,
            )
        for block in result.get("parsing_res_list", []):
            label = getattr(block, "label", None)
            if label in labels:
                counts[labels[label]] += 1
        if not markdown:
            blank_pages.append(number)
            markdown = "_No readable content detected on this page; check the source._"
        pages[number] = markdown
    if not pages:
        raise RuntimeError("PaddleOCR returned no pages")
    if expected_pages is not None and set(pages) != set(range(1, expected_pages + 1)):
        raise RuntimeError(
            f"Incomplete PDF conversion: expected {expected_pages} pages, received {sorted(pages)}"
        )
    return {
        "markdown": "\n\n".join(
            f"<!-- page {number} -->\n\n{pages[number]}" for number in sorted(pages)
        ),
        "figures": figures,
        "metadata": {
            "pages": len(pages),
            "blank_pages": ",".join(map(str, blank_pages)),
            **counts,
        },
    }


def build_pipeline(vlm_url: str | None):
    options = pipeline_options(vlm_url)
    cache = Path(__file__).resolve().parent.parent / ".cache" / "paddleocr"
    os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(cache / "paddlex"))
    os.environ.setdefault("PADDLE_HOME", str(cache / "paddle"))
    os.environ.setdefault("HF_HOME", str(cache / "huggingface"))
    # Cached runs need no host-availability probe; uncached models still download normally.
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    from paddleocr import PaddleOCRVL

    return PaddleOCRVL(**options)


def convert_source(pipeline, source: Path, output: Path, vlm_url: str | None) -> None:
    generation = {"max_new_tokens": 8192}
    if vlm_url:
        generation["temperature"] = (
            0  # The native Paddle engine does not accept this option.
        )
    result = collect_pages(
        pipeline.predict(str(source), **generation),
        output.parent,
    )
    result["metadata"].update(
        processor="paddleocr-vl",
        ocr_model=MODEL,
        ocr_pipeline=PIPELINE,
        paddleocr_version=version("paddleocr"),
        paddlepaddle_version=version("paddlepaddle"),
        ocr_backend="mlx-vlm-server" if vlm_url else "paddle-cpu",
        orientation_correction=True,
        document_unwarping=True,
    )
    output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


def serve(pipeline, vlm_url, requests, responses) -> None:
    for line in requests:
        try:
            request = json.loads(line)
            convert_source(
                pipeline, Path(request["source"]), Path(request["output"]), vlm_url
            )
            response = {"ok": True}
        except Exception as exc:  # noqa: BLE001 - Report one failed source while keeping the OCR worker available.
            traceback.print_exc(file=sys.stderr)
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        responses.write(json.dumps(response) + "\n")
        responses.flush()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path, nargs="?")
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--vlm-url")
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if args.worker:
        # Reserve the original pipe for replies. Native libraries may also write to fd 1.
        with os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8") as responses:
            os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
            serve(build_pipeline(args.vlm_url), args.vlm_url, sys.stdin, responses)
    else:
        if args.source is None or args.output is None:
            parser.error("source and output are required unless --worker is used")
        convert_source(
            build_pipeline(args.vlm_url), args.source, args.output, args.vlm_url
        )


if __name__ == "__main__":
    main()
