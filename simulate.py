"""Exercise ZIP checkpoint conversion with synthetic evidence, outside real projects."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import io
import json
import re
import shutil
import time
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

import digest
import launch
import projects

SIMULATION_ROOT = digest.PROJECT_DIR / ".cache" / "simulation" / "latest"
SUMMARY = re.compile(r"Done: (\d+) processed, (\d+) unchanged, (\d+) failed")
BASE_CHAT = (
    "[01/10/26, 10:00] Kurator Contoh: Data SIMULASI untuk menguji checkpoint.\n"
    "[01/10/26, 10:01] Kurator Contoh: <attached: invoice.png>\n"
    "[01/10/26, 10:02] Kurator Contoh: <attached: invoice.pdf>\n"
    "[01/10/26, 10:03] Tim Contoh: OK\n"
    "[01/10/26, 10:03] Tim Contoh: OK\n"
)


def make_invoice(total: str) -> Image.Image:
    image = Image.new("RGB", (1200, 700), "white")
    font_path = Path("/System/Library/Fonts/Helvetica.ttc")
    font = (
        ImageFont.truetype(str(font_path), 42)
        if font_path.exists()
        else ImageFont.load_default(size=42)
    )
    ImageDraw.Draw(image).text(
        (80, 150),
        f"Invoice CHECK-2026\nTotal {total}\nJakarta",
        fill="black",
        font=font,
        spacing=25,
    )
    return image


def image_bytes(image: Image.Image, format: str, **options) -> bytes:
    output = io.BytesIO()
    image.save(output, format=format, **options)
    return output.getvalue()


def write_zip(project: Path, name: str, chat: str, media: dict[str, bytes]) -> Path:
    archive = project / "input" / name
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr("_chat.txt", chat)
        for filename, content in media.items():
            zipped.writestr(filename, content)
    return archive


def prepare_simulation() -> Path:
    if SIMULATION_ROOT.exists():
        marker = SIMULATION_ROOT / ".synthetic-workspace"
        if not marker.is_file():
            raise RuntimeError(
                f"Refusing to replace an unmarked directory: {SIMULATION_ROOT}"
            )
        shutil.rmtree(SIMULATION_ROOT)
    SIMULATION_ROOT.mkdir(parents=True)
    (SIMULATION_ROOT / ".synthetic-workspace").write_text(
        "Generated synthetic data only. This cache is replaced by the next simulation.\n"
    )
    return SIMULATION_ROOT / "projects"


def write_report(project: Path, steps: list[dict], engine: str) -> Path:
    now = dt.datetime.now(ZoneInfo("Asia/Jakarta")).isoformat()
    payload = {
        "generated": now,
        "project": str(project),
        "requested_ocr_engine": engine,
        "complete": len(steps) == 8,
        "ocr_text_failures": sum(
            step.get("ocr_text_check", {}).get("found") is False for step in steps
        ),
        "steps": steps,
    }
    digest.atomic_write(
        SIMULATION_ROOT / "report.json", json.dumps(payload, indent=2) + "\n"
    )
    rows = [
        "# ZIP checkpoint simulation",
        "",
        f"Generated: {now}",
        "",
        "All inputs are synthetic. The real kurasi-arsip-2026 project was not used for these checks.",
        "",
        f"Requested OCR engine: {engine}. Actual processor details are recorded in the generated media Markdown.",
        "",
        "| Step | New conversions | Cached | Checkpoints | Messages | Unique media | OCR worker loaded | Expected OCR number found |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for step in steps:
        rows.append(
            f"| {step['name']} | {step['processed']} | {step['unchanged']} | {step['checkpoints']} "
            f"| {step['messages']} | {step['media']} | {step['ocr_loaded']} | "
            f"{step.get('ocr_text_check', {}).get('found', 'not checked')} |"
        )
    rows.extend(
        [
            "",
            "The invalid-ZIP step expects exit code 1; its successful check means that partial input was rejected.",
            "",
            f"Synthetic output: [combined.md](projects/{project.name}/target/combined.md).",
            "",
            "Each step's complete terminal output is saved beside this report in logs/.",
            "",
            (
                f"OCR content checks that missed the expected number: {payload['ocr_text_failures']}. "
                "These are reported separately from checkpoint, cache, and conversion-lifecycle checks."
            ),
        ]
    )
    if (SIMULATION_ROOT.parent / "ocr-observation.md").is_file():
        rows.extend(
            [
                "",
                (
                    "A preliminary fixture with a different heading lost a numeric line despite successful OCR. "
                    "See [the retained observation](../ocr-observation.md). Successful fixture checks do not establish accuracy on arbitrary documents."
                ),
            ]
        )
    report = SIMULATION_ROOT / "report.md"
    digest.atomic_write(report, "\n".join(rows) + "\n")
    return report


def simulate(engine: str) -> Path:
    previous_root = projects.ROOT
    projects.ROOT = prepare_simulation()
    try:
        project = projects.initialize(
            "kurasi-arsip-2026-simulation",
            "Synthetic test workspace. No real project facts or private messages.",
        )
        commands = [
            "convert",
            project.name,
            "--ocr-engine",
            engine,
            "--ocr-timeout",
            "180",
        ]
        steps = []

        def run_step(name, counts, conversions, expected_exit=0, needs_ocr=False):
            print(f"SIMULATE: {name}", flush=True)
            captured = io.StringIO()
            started = time.monotonic()
            with (
                contextlib.redirect_stdout(captured),
                contextlib.redirect_stderr(captured),
            ):
                result = launch.main(commands)
            output = captured.getvalue()
            digest.atomic_write(
                SIMULATION_ROOT / "logs" / f"{len(steps) + 1:02d}.log", output
            )
            if result != expected_exit:
                raise AssertionError(
                    f"{name}: exit {result}, expected {expected_exit}\n{output}"
                )
            summary = SUMMARY.search(output)
            if summary is None or tuple(map(int, summary.groups())) != (
                *conversions,
                0,
            ):
                raise AssertionError(f"{name}: unexpected conversion counts\n{output}")
            state = projects.load_state(project)
            actual = tuple(
                len(state[key]) for key in ("checkpoints", "messages", "media")
            )
            if actual != counts:
                raise AssertionError(
                    f"{name}: state counts {actual}, expected {counts}"
                )
            ocr_loaded = "Loading OCR models" in output
            if not needs_ocr and (
                ocr_loaded
                or "Starting local OCR server" in output
                or "Using the running local OCR server" in output
            ):
                raise AssertionError(f"{name}: cached media unexpectedly loaded OCR")
            steps.append(
                {
                    "name": name,
                    "exit_code": result,
                    "processed": conversions[0],
                    "unchanged": conversions[1],
                    "checkpoints": actual[0],
                    "messages": actual[1],
                    "media": actual[2],
                    "ocr_loaded": ocr_loaded,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                }
            )
            write_report(project, steps, engine)
            print(
                f"PASS: {name}: {conversions[0]} processed, {conversions[1]} cached",
                flush=True,
            )

        def check_number(content: bytes, expected: str):
            state = projects.load_state(project)
            media_id = hashlib.sha256(content).hexdigest()
            record = state["media"][media_id]
            converted = project / "target" / "markdown" / (record["source"] + ".md")
            body = converted.read_text().split("\n---\n", 1)[-1]
            found = expected in body
            steps[-1]["ocr_text_check"] = {"expected": expected, "found": found}
            if not found:
                evidence = SIMULATION_ROOT / "ocr-observations"
                evidence.mkdir(exist_ok=True)
                shutil.copyfile(
                    project / "target" / "todo" / record["source"],
                    evidence / f"{media_id}.png",
                )
                shutil.copyfile(converted, evidence / f"{media_id}.md")
                steps[-1]["ocr_text_check"]["evidence"] = str(evidence)
                print(
                    f"OCR WARNING: number {expected} was omitted; original and output saved in {evidence}",
                    flush=True,
                )
            write_report(project, steps, engine)

        invoice = make_invoice("125000")
        png = image_bytes(invoice, "PNG")
        pdf = image_bytes(invoice, "PDF", save_all=True, append_images=[invoice])
        first = write_zip(
            project, "01-first.zip", BASE_CHAT, {"invoice.png": png, "invoice.pdf": pdf}
        )
        run_step("Initial PNG and two-page PDF", (1, 5, 2), (3, 0), needs_ocr=True)
        combined = (project / "target" / "combined.md").read_text()
        if "CHECK-2026" not in combined or "<!-- page 2 -->" not in combined:
            raise AssertionError(
                "Initial image/PDF document identity or page coverage is missing"
            )
        check_number(png, "125000")

        shutil.copyfile(first, project / "input" / "02-identical-copy.zip")
        run_step("Identical ZIP under another name", (1, 5, 2), (0, 3))

        newer = BASE_CHAT + "[03/10/26, 10:00] Tim Contoh: Checkpoint terbaru.\n"
        write_zip(
            project,
            "03-newer.zip",
            newer,
            {"renamed-invoice.png": png, "invoice.pdf": pdf},
        )
        run_step("Newer overlap and renamed media", (2, 6, 2), (1, 2))

        older = (
            "[30/09/26, 10:00] Tim Contoh: Riwayat lebih awal.\n"
            + BASE_CHAT.splitlines()[0]
            + "\n"
        )
        write_zip(project, "00-older.zip", older, {"invoice.png": png})
        run_step("Older checkpoint imported later", (3, 7, 2), (1, 2))

        changed = image_bytes(make_invoice("240000"), "PNG")
        write_zip(
            project,
            "04-changed.zip",
            BASE_CHAT + "[04/10/26, 10:00] Tim Contoh: Lampiran berubah.\n",
            {"invoice.png": changed},
        )
        run_step("Same filename with changed bytes", (4, 8, 3), (2, 2), needs_ocr=True)
        check_number(changed, "240000")

        invalid = write_zip(
            project,
            "05-invalid.zip",
            BASE_CHAT,
            {"../escape.txt": b"reject this member"},
        )
        before = (project / ".digester" / "state.json").read_bytes()
        run_step(
            "Invalid ZIP rejected without partial import",
            (4, 8, 3),
            (0, 4),
            expected_exit=1,
        )
        if (project / ".digester" / "state.json").read_bytes() != before:
            raise AssertionError("Rejected ZIP changed the canonical state")
        invalid.unlink()

        state = projects.load_state(project)
        image_id = hashlib.sha256(png).hexdigest()
        output = (
            project
            / "target"
            / "markdown"
            / (state["media"][image_id]["source"] + ".md")
        )
        output.unlink()
        run_step("Missing image output recovered", (4, 8, 3), (1, 3), needs_ocr=True)
        run_step("Final fully cached rerun", (4, 8, 3), (0, 4))
        for checkpoint_id, checkpoint in state["checkpoints"].items():
            for archive in checkpoint["archives"]:
                if digest.sha256(project / archive) != checkpoint_id:
                    raise AssertionError(f"Original ZIP changed: {archive}")
        manifest = json.loads((project / "target" / ".manifest.json").read_text())
        if len(manifest["files"]) != 4 or any(
            record["status"] != "success" for record in manifest["files"].values()
        ):
            raise AssertionError("Final conversion manifest is not complete")
        report = write_report(project, steps, engine)
        print(f"Simulation complete. Report: {report}", flush=True)
        return report
    finally:
        projects.ROOT = previous_root


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ocr-engine", choices=("auto", "paddleocr", "tesseract"), default="auto"
    )
    args = parser.parse_args()
    simulate(args.ocr_engine)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
