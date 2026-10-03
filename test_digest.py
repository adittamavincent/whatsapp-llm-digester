import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import digest
from ocr.convert import collect_pages, pipeline_options, serve

FAKE_OCR_WORKER = """
import json, pathlib, sys, time
print("Creating model: test model", file=sys.stderr, flush=True)
for line in sys.stdin:
    request = json.loads(line)
    source = pathlib.Path(request["source"])
    if source.suffix == ".hang":
        time.sleep(60)
    if source.suffix == ".crash":
        print("test inference crashed", file=sys.stderr, flush=True)
        sys.exit(7)
    if source.suffix == ".noise":
        print("[]", flush=True)
        continue
    if source.suffix == ".bad":
        response = {"ok": False, "error": "test unreadable source"}
    else:
        pathlib.Path(request["output"]).write_text(json.dumps({
            "markdown": "Recognized " + source.name, "metadata": {}, "figures": []
        }))
        response = {"ok": True}
    print(json.dumps(response), flush=True)
"""


class OCRWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "ocr").mkdir()
        (self.root / "ocr" / "convert.py").write_text(FAKE_OCR_WORKER)
        self.project = patch.object(digest, "PROJECT_DIR", self.root)
        self.python = patch.object(digest, "OCR_PYTHON", Path(sys.executable))
        self.project.start()
        self.python.start()
        self.addCleanup(self.project.stop)
        self.addCleanup(self.python.stop)
        self.converter = digest.PaddleOCRConverter(digest.parse_args([]))
        self.addCleanup(self.converter.close)

    def test_models_are_loaded_once_across_images_pdf_and_frames(self):
        terminal = io.StringIO()
        with contextlib.redirect_stdout(terminal):
            for name in ("photo.jpg", "scan.pdf", "frame-1.jpg", "frame-2.jpg"):
                markdown, _ = self.converter.convert(self.root / name)
                self.assertEqual(markdown, "Recognized " + name)
        worker = self.converter._worker
        self.assertEqual(terminal.getvalue().count("Loading OCR models"), 1)
        self.assertNotIn("Creating model", terminal.getvalue())
        self.assertEqual(self.converter.log_path.read_text().count("Creating model"), 1)
        self.converter.close()
        self.assertIsNotNone(worker.poll())

    def test_failed_source_is_visible_and_next_source_reuses_the_worker(self):
        with self.assertRaisesRegex(RuntimeError, "test unreadable source"):
            self.converter.convert(self.root / "unreadable.bad")
        worker = self.converter._worker
        self.converter.convert(self.root / "next.jpg")
        self.assertIs(self.converter._worker, worker)

    def test_timeout_stops_worker_and_next_source_starts_a_fresh_one(self):
        self.converter.args.ocr_timeout = 1
        with self.assertRaisesRegex(RuntimeError, "exceeded 1s"):
            self.converter.convert(self.root / "slow.hang")
        self.assertIsNone(self.converter._worker)
        self.assertIsNone(self.converter._log)
        self.converter.convert(self.root / "next.jpg")
        self.assertEqual(self.converter.log_path.read_text().count("Creating model"), 2)

    def test_worker_crash_reports_the_error_log(self):
        with self.assertRaisesRegex(RuntimeError, "test inference crashed"):
            self.converter.convert(self.root / "broken.crash")
        self.assertIsNone(self.converter._worker)

    def test_malformed_reply_stops_worker_before_the_next_source(self):
        with self.assertRaisesRegex(RuntimeError, "invalid OCR worker response"):
            self.converter.convert(self.root / "broken.noise")
        self.assertIsNone(self.converter._worker)
        self.converter.convert(self.root / "next.jpg")

    def test_run_closes_worker_on_success_failure_and_interruption(self):
        for failure in (None, RuntimeError("failed"), KeyboardInterrupt()):
            converter = Mock()
            with (
                self.subTest(failure=failure),
                patch.object(digest, "ensure_layout"),
                patch.object(digest, "resolve_ocr_languages", return_value="eng"),
                patch.object(digest, "Converter", return_value=converter),
                patch.object(
                    digest, "process_sources", return_value=0, side_effect=failure
                ),
            ):
                if failure is None:
                    self.assertEqual(digest.process(digest.parse_args([])), 0)
                else:
                    with self.assertRaises(type(failure)):
                        digest.process(digest.parse_args([]))
                converter.close.assert_called_once()

    def test_worker_keeps_the_same_pipeline_after_a_source_error(self):
        requests = io.StringIO(
            "\n".join(
                json.dumps({"source": name, "output": "result.json"})
                for name in ("one.jpg", "bad.jpg", "three.pdf")
            )
        )
        responses = io.StringIO()
        pipeline = Mock()
        with (
            patch(
                "ocr.convert.convert_source",
                side_effect=[None, RuntimeError("bad page"), None],
            ) as convert,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            serve(pipeline, None, requests, responses)
        self.assertEqual(
            [json.loads(line)["ok"] for line in responses.getvalue().splitlines()],
            [True, False, True],
        )
        self.assertTrue(
            all(call.args[0] is pipeline for call in convert.call_args_list)
        )


class OCRPage(dict):
    def __init__(self, index, total, text):
        super().__init__(page_index=index, page_count=total, parsing_res_list=[])
        self.markdown = {"markdown_texts": text, "markdown_images": {}}


class DigestTests(unittest.TestCase):
    def test_classifies_current_input_families(self):
        self.assertEqual(digest.classify(Path("photo.jpg")), "image")
        self.assertEqual(digest.classify(Path("voice.opus")), "audio")
        self.assertEqual(digest.classify(Path("clip.mp4")), "video")
        self.assertEqual(digest.classify(Path("offer.pdf")), "document")
        self.assertEqual(digest.classify(Path("chat.txt")), "text")
        self.assertEqual(digest.classify(Path("bundle.zip")), "document")

    def test_formats_short_and_long_timestamps(self):
        self.assertEqual(digest.format_timestamp(65.2), "01:05")
        self.assertEqual(digest.format_timestamp(3661), "01:01:01")

    def test_strips_model_thinking_blocks(self):
        self.assertEqual(
            digest.strip_thinking("<think>hidden</think>Visible"), "Visible"
        )

    def test_yaml_string_is_quoted(self):
        self.assertEqual(digest.yaml_string("a: b"), '"a: b"')

    def test_vision_endpoint_maps_to_openai_path_on_loopback(self):
        self.assertEqual(
            digest.loopback_chat_url("http://127.0.0.1:11434/api/chat"),
            "http://127.0.0.1:11434/v1/chat/completions",
        )

    def test_vision_endpoint_rejects_remote_hosts(self):
        with self.assertRaises(RuntimeError):
            digest.loopback_chat_url("https://example.com/api/chat")

    def test_docling_handles_layout_heavy_formats_only(self):
        self.assertIn(".pdf", digest.DOCLING_EXTENSIONS)
        self.assertNotIn(".eml", digest.DOCLING_EXTENSIONS)
        self.assertNotIn(".zip", digest.DOCLING_EXTENSIONS)

    def test_pdf_pages_are_ordered_and_blank_pages_are_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = collect_pages(
                [OCRPage(1, 2, ""), OCRPage(0, 2, "Budi Santoso")], Path(temporary)
            )
        self.assertTrue(
            result["markdown"].startswith("<!-- page 1 -->\n\nBudi Santoso")
        )
        self.assertIn("<!-- page 2 -->", result["markdown"])
        self.assertIn("check the source", result["markdown"])
        self.assertEqual(result["metadata"]["blank_pages"], "2")

    def test_missing_and_duplicate_pdf_pages_fail(self):
        for pages in (
            [OCRPage(0, 2, "One")],
            [OCRPage(0, 1, "One"), OCRPage(0, 1, "Again")],
            [],
        ):
            with (
                self.subTest(pages=pages),
                tempfile.TemporaryDirectory() as temporary,
                self.assertRaises(RuntimeError),
            ):
                collect_pages(pages, Path(temporary))

    def test_paddleocr_always_uses_layout_and_keeps_footnotes(self):
        options = pipeline_options(None)
        self.assertEqual(options["pipeline_version"], "v1.6")
        self.assertTrue(options["use_layout_detection"])
        self.assertTrue(options["use_chart_recognition"])
        self.assertEqual(options["markdown_ignore_labels"], [])

    def test_ocr_acceleration_refuses_external_services(self):
        options = pipeline_options("http://127.0.0.1:8111/")
        self.assertEqual(options["vl_rec_backend"], "mlx-vlm-server")
        self.assertEqual(options["vl_rec_max_concurrency"], 1)
        for url in (
            "https://example.com",
            "http://user:pass@localhost:8111",
            "http://localhost/?key=secret",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                pipeline_options(url)

    def test_explicit_ocr_request_fails_when_not_installed(self):
        with patch.object(digest, "paddleocr_available", return_value=False):
            self.assertEqual(digest.resolve_ocr_engine("auto"), "tesseract")
            with self.assertRaisesRegex(RuntimeError, "setup-ocr"):
                digest.resolve_ocr_engine("paddleocr")

    def test_pdf_ocr_errors_cannot_fall_back_to_empty_extraction(self):
        converter = digest.Converter.__new__(digest.Converter)
        converter.paddle = Mock()
        converter.paddle.convert.side_effect = RuntimeError("missing page")
        converter.convert_markitdown = Mock()
        with self.assertRaisesRegex(RuntimeError, "missing page"):
            converter.convert_document(Path("scan.pdf"))
        converter.convert_markitdown.assert_not_called()

    def test_changing_ocr_engine_invalidates_previous_output(self):
        args = digest.parse_args([])
        converter = SimpleNamespace(
            speech=SimpleNamespace(backend="mlx", model_name="whisper-turbo"),
            ocr_languages="eng",
            ocr_engine="tesseract",
            paddle=None,
            docling=None,
        )
        before = digest.config_key(args, converter)
        converter.ocr_engine = "paddleocr"
        converter.paddle = SimpleNamespace(versions=lambda: {"paddleocr": "3.7.0"})
        after = digest.config_key(args, converter)
        self.assertNotEqual(before, after)
        self.assertEqual(after["ocr_pipeline"], "v1.6")

    def test_figure_crops_become_text_without_broken_links(self):
        image = Mock()
        page = OCRPage(
            0,
            1,
            'Photo: ![Figure](imgs/figure.png) <table><td><img src="imgs/figure.png"></td></table>',
        )
        page.markdown["markdown_images"] = {"imgs/figure.png": image}
        with tempfile.TemporaryDirectory() as temporary:
            result = collect_pages([page], Path(temporary))
            image.save.assert_called_once()
        self.assertNotIn("imgs/figure.png", result["markdown"])
        self.assertIn("Figure 1 on page 1", result["markdown"])


if __name__ == "__main__":
    unittest.main()
