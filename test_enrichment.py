import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import digest
import enrichment as e
import launch


class EnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "private/enrichment.sqlite"
        self.connection = e.connect(self.db, write=True)
        self.addCleanup(self.connection.close)

    def source(self, name="source.txt", text="source evidence"):
        path = self.root / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text(text)
        return path

    def add(self, path, ref_id="1"):
        e.register(self.connection, "fixture", ref_id, path, {})
        self.connection.commit()

    def run_worker(self, options=None, limit=0):
        with contextlib.redirect_stdout(io.StringIO()):
            return e.run_queue(
                self.connection,
                options or ["--ocr-engine", "tesseract", "--doc-engine", "markitdown"],
                limit,
            )

    def test_duplicates_convert_once_and_preserve_provenance(self):
        self.add(self.source("one.txt"), "1")
        self.add(self.source("two.txt"), "2")
        self.assertEqual(self.run_worker(), 0)
        stats = e.summary(self.connection)
        self.assertEqual(stats["assets"], {"done": 1})
        self.assertEqual(stats["references"], {"available": 2})
        self.assertEqual(stats["results"], 1)
        self.assertEqual(self.run_worker(), 0)
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM attempts").fetchone()[0], 1
        )
        result = self.connection.execute(
            "SELECT markdown,metadata_json FROM results"
        ).fetchone()
        self.assertIn("source evidence", result[0])
        self.assertEqual(json.loads(result[1])["processor"], "plain-text")

    def test_config_changes_keep_old_results_and_force_runs(self):
        self.add(self.source())
        options = ["--ocr-engine", "tesseract", "--doc-engine", "markitdown"]
        self.run_worker(options)
        self.run_worker([*options, "--language", "id"])
        self.assertEqual(e.summary(self.connection)["results"], 2)
        self.run_worker([*options, "--language", "id", "--force"])
        self.assertEqual(
            self.connection.execute("SELECT attempts FROM assets").fetchone()[0], 3
        )

    def test_missing_media_recovers_and_changed_bytes_are_distinct(self):
        path = self.root / "later.txt"
        self.add(path)
        self.assertEqual(e.summary(self.connection)["references"], {"missing": 1})
        path.write_text("first")
        self.add(path)
        self.run_worker()
        path.write_text("second")
        self.add(path)
        self.run_worker()
        self.assertEqual(e.summary(self.connection)["results"], 2)
        self.assertEqual(e.summary(self.connection)["assets"], {"done": 2})

    def test_extensionless_media_sniffing_and_unsupported(self):
        png = self.root / "hashwithoutasuffix"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fixture")
        self.add(png, "image")
        asset = self.connection.execute(
            "SELECT kind,suffix,state FROM assets"
        ).fetchone()
        self.assertEqual(tuple(asset), ("image", ".png", "pending"))
        unknown = self.root / "unknown"
        unknown.write_bytes(b"\x00\x01\x02")
        self.add(unknown, "unknown")
        self.assertEqual(e.summary(self.connection)["assets"]["unsupported"], 1)

    def test_failed_conversion_requires_explicit_retry(self):
        self.add(self.source())
        with (
            patch.object(
                digest.Converter,
                "convert",
                side_effect=RuntimeError("processor failed"),
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(self.run_worker(), 1)
        self.assertEqual(e.summary(self.connection)["assets"], {"failed": 1})
        self.assertEqual(self.run_worker(), 0)
        self.assertEqual(
            self.connection.execute("SELECT attempts FROM assets").fetchone()[0], 1
        )
        with contextlib.redirect_stdout(io.StringIO()):
            e.main(["--db", str(self.db), "retry"])
        self.assertEqual(self.run_worker(), 0)
        self.assertEqual(e.summary(self.connection)["assets"], {"done": 1})

    def test_interrupt_and_crash_recovery(self):
        self.add(self.source())
        with (
            patch.object(digest.Converter, "convert", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.run_worker()
        self.assertEqual(e.summary(self.connection)["assets"], {"pending": 1})
        self.connection.execute("UPDATE assets SET state='running'")
        self.connection.commit()
        self.assertEqual(self.run_worker(), 0)
        self.assertEqual(e.summary(self.connection)["assets"], {"done": 1})

    def test_worker_lock_blocks_writers_but_allows_status(self):
        with e.writer_lock(self.db):
            with self.assertRaises(RuntimeError), e.writer_lock(self.db):
                pass
            with contextlib.closing(e.connect(self.db)) as reader:
                self.assertEqual(e.summary(reader)["results"], 0)

    def test_wacrawl_import_is_readonly_and_exposes_missing_media(self):
        archive = self.root / "archive/wacrawl.db"
        media = self.source("archive/media/file.txt")
        with sqlite3.connect(archive) as c:
            c.execute(
                "CREATE TABLE messages(event_id,msg_id,chat_jid,ts,media_path,media_type,media_title)"
            )
            c.executemany(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?)",
                [
                    ("one", "id1", "group", 1, str(media), "document", "file"),
                    ("two", "id2", "group", 2, "", "image", ""),
                    (
                        "three",
                        "id3",
                        "group",
                        3,
                        str(self.source("outside.txt")),
                        "document",
                        "outside",
                    ),
                ],
            )
        checksum = digest.sha256(archive)
        self.assertEqual(e.sync_wacrawl(self.connection, archive), 3)
        self.assertEqual(e.sync_wacrawl(self.connection, archive), 3)
        self.assertEqual(digest.sha256(archive), checksum)
        self.assertEqual(
            e.summary(self.connection)["references"], {"available": 1, "missing": 2}
        )
        self.assertEqual(e.summary(self.connection)["assets"], {"pending": 1})

    def test_source_disappears_before_worker_and_recovers_on_rescan(self):
        path = self.source()
        self.add(path)
        path.unlink()
        self.assertEqual(self.run_worker(), 1)
        self.assertEqual(e.summary(self.connection)["assets"], {"missing": 1})
        path.write_text("source evidence")
        self.add(path)
        self.run_worker()
        self.assertEqual(e.summary(self.connection)["assets"], {"done": 1})

    def test_control_commands_never_start_models(self):
        self.add(self.source())
        with (
            patch.object(
                digest, "Converter", side_effect=AssertionError("model should not load")
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(launch.main(["enrich", "--db", str(self.db), "status"]), 0)
            self.assertEqual(
                launch.main(["enrich", "--db", str(self.db), "queue", "--json"]), 0
            )

    def test_auto_ocr_server_is_owned_for_ocr_work(self):
        image = self.root / "image"
        image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        self.add(image)

        @contextlib.contextmanager
        def server():
            yield launch.OCR_URL

        with (
            patch.object(launch, "can_auto_server", return_value=True),
            patch.object(launch, "local_server", side_effect=server) as start,
            patch.object(digest, "config_for_args", return_value={"test": 1}),
            patch.object(
                digest,
                "Converter",
                return_value=Mock(
                    convert=Mock(return_value=("OCR result", {"processor": "test"}))
                ),
            ),
        ):
            self.assertEqual(
                self.run_worker(
                    ["--ocr-engine", "tesseract", "--doc-engine", "markitdown"]
                ),
                0,
            )
            start.assert_called_once()

    def test_rejects_writes_to_another_database(self):
        other = self.root / "wacrawl.db"
        with sqlite3.connect(other) as connection:
            connection.execute("CREATE TABLE messages(text)")
        checksum = digest.sha256(other)
        with self.assertRaisesRegex(ValueError, "Refusing"):
            e.connect(other, write=True)
        self.assertEqual(digest.sha256(other), checksum)

    def test_iphone_snapshot_keeps_reply_and_missing_media_provenance(self):
        snapshot = self.root / "iphone"
        media = self.source("iphone/domain/Message/Media/file.txt")
        data = {
            "chat": {
                "media_base": "domain/",
                "messages": {
                    "1": {
                        "media": True,
                        "data": "Message/Media/file.txt",
                        "reply": "original-id",
                        "caption": "receipt",
                    },
                    "2": {
                        "media": True,
                        "data": "The media is missing",
                        "mime": "media",
                    },
                    "3": {"media": True, "data": "../../outside.txt"},
                    "4": {"media": False, "data": "message text"},
                },
            }
        }
        self.source("outside.txt")
        (snapshot / "result.json").write_text(json.dumps(data))
        checksum = digest.sha256(media)
        self.assertEqual(e.sync_iphone(self.connection, snapshot), 3)
        self.assertEqual(e.sync_iphone(self.connection, snapshot), 3)
        self.assertEqual(
            e.summary(self.connection)["references"], {"available": 1, "missing": 2}
        )
        metadata = json.loads(
            self.connection.execute(
                "SELECT metadata_json FROM refs WHERE sha256=?", (checksum,)
            ).fetchone()[0]
        )
        self.assertEqual(metadata["reply"], "original-id")
        self.assertEqual(self.run_worker(), 0)


if __name__ == "__main__":
    unittest.main()
