import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import digest
import launch


class LauncherTests(unittest.TestCase):
    def test_one_command_adds_the_local_backend_and_preserves_options(self):
        @contextlib.contextmanager
        def server():
            yield launch.OCR_URL

        with (
            patch.object(launch, "needs_local_server", return_value=True),
            patch.object(launch, "local_server", server),
            patch.object(digest, "main", return_value=1) as run,
        ):
            self.assertEqual(launch.main(["--force", "--language", "id"]), 1)
        run.assert_called_once_with(
            ["--force", "--language", "id", "--ocr-vlm-url", launch.OCR_URL]
        )

    def test_control_commands_and_explicit_backends_do_not_start_a_server(self):
        for arguments in (
            ["doctor"],
            ["prefetch"],
            ["--ocr-engine", "tesseract"],
            ["--ocr-vlm-url", "http://127.0.0.1:8112/"],
        ):
            with (
                self.subTest(arguments=arguments),
                patch.object(launch, "local_server") as server,
                patch.object(digest, "main", return_value=0),
            ):
                self.assertEqual(launch.main(arguments), 0)
                server.assert_not_called()

    def test_only_ocr_inputs_require_the_server(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "ocr" / ".venv" / "bin" / "mlx_vlm.server"
            runtime.parent.mkdir(parents=True)
            runtime.touch()
            todo = root / "todo"
            todo.mkdir()
            with (
                patch.object(digest, "PROJECT_DIR", root),
                patch.object(digest, "TODO_DIR", todo),
                patch.object(digest, "paddleocr_available", return_value=True),
                patch.object(launch.platform, "system", return_value="Darwin"),
                patch.object(launch.platform, "machine", return_value="arm64"),
            ):
                args = digest.parse_args([])
                (todo / "chat.txt").touch()
                self.assertFalse(launch.needs_local_server(args))
                (todo / "scan.pdf").touch()
                self.assertTrue(launch.needs_local_server(args))
                runtime.unlink()
                self.assertFalse(launch.needs_local_server(args))

    def test_existing_ready_server_is_reused_without_stopping_it(self):
        with (
            patch.object(launch, "server_ready", return_value=True),
            patch.object(launch.subprocess, "Popen") as start,
            patch.object(launch, "stop_server") as stop,
            launch.local_server() as url,
        ):
            self.assertEqual(url, launch.OCR_URL)
        start.assert_not_called()
        stop.assert_not_called()

    def test_occupied_port_is_reported_without_starting_a_second_server(self):
        with (
            patch.object(launch, "server_ready", return_value=False),
            patch.object(launch, "port_in_use", return_value=True),
            patch.object(launch.subprocess, "Popen") as start,
            self.assertRaisesRegex(RuntimeError, "Port 8111"),
            launch.local_server(),
        ):
            pass
        start.assert_not_called()

    def test_owned_server_is_cleaned_up_when_conversion_fails(self):
        process = Mock()
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(digest, "PROJECT_DIR", Path(temporary)),
            patch.object(launch, "server_ready", side_effect=[False, True]),
            patch.object(launch, "port_in_use", return_value=False),
            patch.object(launch.subprocess, "Popen", return_value=process),
            patch.object(launch, "stop_server") as stop,
            self.assertRaisesRegex(RuntimeError, "conversion failed"),
            launch.local_server(),
        ):
            raise RuntimeError("conversion failed")
        stop.assert_called_once_with(process)

    def test_server_startup_failure_stops_the_owned_process(self):
        process = Mock(returncode=7)
        process.poll.return_value = 7
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(digest, "PROJECT_DIR", Path(temporary)),
            patch.object(launch, "server_ready", return_value=False),
            patch.object(launch, "port_in_use", return_value=False),
            patch.object(launch.subprocess, "Popen", return_value=process),
            patch.object(launch, "stop_server") as stop,
            self.assertRaisesRegex(RuntimeError, "exited with code 7"),
            launch.local_server(),
        ):
            self.fail("A failed server must not start conversion")
        stop.assert_called_once_with(process)

    def test_cached_ocr_does_not_start_server_but_missing_output_does(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            digest.target_directory(Path(temporary)),
        ):
            digest.TODO_DIR.mkdir()
            source = digest.TODO_DIR / "image.jpg"
            source.write_bytes(b"image")
            output = digest.output_path_for(source)
            output.parent.mkdir(parents=True)
            output.write_text("already converted")
            config = {"ocr_vlm_url": launch.OCR_URL}
            digest.save_manifest(
                {
                    "files": {
                        "image.jpg": {
                            "status": "success",
                            "sha256": digest.sha256(source),
                            "config": config,
                        }
                    }
                }
            )
            with (
                patch.object(launch, "can_auto_server", return_value=True),
                patch.object(digest, "config_for_args", return_value=config),
            ):
                self.assertFalse(launch.needs_local_server(digest.parse_args([])))
                self.assertTrue(
                    launch.needs_local_server(digest.parse_args(["--force"]))
                )
                output.unlink()
                self.assertTrue(launch.needs_local_server(digest.parse_args([])))

    def test_cached_run_keeps_backend_configuration_without_starting_server(self):
        with (
            patch.object(launch, "needs_local_server", return_value=False),
            patch.object(launch, "can_auto_server", return_value=True),
            patch.object(launch, "local_server") as server,
            patch.object(digest, "main", return_value=0) as run,
        ):
            self.assertEqual(launch.run_legacy(["run"]), 0)
        server.assert_not_called()
        run.assert_called_once_with(["run", "--ocr-vlm-url", launch.OCR_URL])


if __name__ == "__main__":
    unittest.main()
