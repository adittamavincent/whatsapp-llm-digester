"""Launch digestion with an automatically managed local OCR server when available."""

from __future__ import annotations

import contextlib
import copy
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import digest

OCR_URL = "http://127.0.0.1:8111/"
OCR_MODEL = "PaddlePaddle/PaddleOCR-VL-1.6"
STARTUP_TIMEOUT = 600


def server_ready() -> bool:
    try:
        with urllib.request.urlopen(OCR_URL + "v1/models", timeout=2) as response:
            models = json.load(response).get("data", [])
        return any(
            model.get("id") == OCR_MODEL and model.get("loaded", True)
            for model in models
        )
    except (OSError, ValueError, urllib.error.URLError):
        return False


def port_in_use() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8111), timeout=1):
            return True
    except OSError:
        return False


def can_auto_server(args) -> bool:
    if args.command != "run" or args.ocr_vlm_url or args.ocr_engine == "tesseract":
        return False
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return False
    metal_runtime = digest.PROJECT_DIR / "ocr" / ".venv" / "bin" / "mlx_vlm.server"
    return metal_runtime.is_file() and digest.paddleocr_available()


def needs_local_server(args) -> bool:
    if not can_auto_server(args):
        return False
    sources = [
        path
        for path in digest.TODO_DIR.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and (
            digest.classify(path) in {"image", "video"} or path.suffix.lower() == ".pdf"
        )
    ]
    if not sources:
        return False
    records = digest.load_manifest().get("files", {})
    if args.force or not records:
        return True
    effective = copy.copy(args)
    effective.ocr_vlm_url = OCR_URL
    config = digest.config_for_args(effective)
    return any(
        not digest.cached_source(
            path,
            digest.sha256(path),
            records.get(path.relative_to(digest.TODO_DIR).as_posix(), {}),
            config,
        )
        for path in sources
    )


def stop_server(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


@contextlib.contextmanager
def local_server():
    if server_ready():
        print("Using the running local OCR server.", flush=True)
        yield OCR_URL
        return
    if port_in_use():
        raise RuntimeError(
            "Port 8111 is occupied, but PaddleOCR-VL-1.6 is not ready. Wait for that server or free the port."
        )

    log_path = digest.PROJECT_DIR / ".cache" / "paddleocr" / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(
        f"Starting local OCR server. First use may download model weights. Log: {log_path}",
        flush=True,
    )
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [str(digest.PROJECT_DIR / "ocr-server.sh")],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            start = time.monotonic()
            next_update = start + 15
            while not server_ready():
                if process.poll() is not None:
                    tail = "\n".join(
                        log_path.read_text(
                            encoding="utf-8", errors="replace"
                        ).splitlines()[-15:]
                    )
                    raise RuntimeError(
                        f"OCR server exited with code {process.returncode}.\n{tail}"
                    )
                now = time.monotonic()
                if now - start >= STARTUP_TIMEOUT:
                    raise RuntimeError(
                        f"OCR server did not become ready within {STARTUP_TIMEOUT}s. See {log_path}"
                    )
                if now >= next_update:
                    print(
                        f"Waiting for the OCR model ({int(now - start)}s).", flush=True
                    )
                    next_update = now + 15
                time.sleep(1)
            print("OCR server ready. Starting conversion.", flush=True)
            yield OCR_URL
        finally:
            stop_server(process)


def run_legacy(arguments: list[str]) -> int:
    args = digest.parse_args(arguments)
    try:
        if needs_local_server(args):
            with local_server() as endpoint:
                return digest.main([*arguments, "--ocr-vlm-url", endpoint])
        # Keep the same effective backend on a cached rerun, even when no server is needed.
        if can_auto_server(args):
            return digest.main([*arguments, "--ocr-vlm-url", OCR_URL])
        return digest.main(arguments)
    except KeyboardInterrupt:
        return 130
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in {"new", "convert", "projects"}:
        import projects

        try:
            return projects.main(arguments)
        except KeyboardInterrupt:
            return 130
        except (RuntimeError, ValueError, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    return run_legacy(arguments)


if __name__ == "__main__":

    def interrupt(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    raise SystemExit(main())
