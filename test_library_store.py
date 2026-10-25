import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import library_store


class SnapshotTests(unittest.TestCase):
    def test_snapshot_includes_wal_commits_and_stands_alone(self):
        with tempfile.TemporaryDirectory() as temporary:
            library = Path(temporary)
            source = sqlite3.connect(library / "wacrawl.db")
            try:
                source.execute("PRAGMA journal_mode=WAL")
                source.execute("CREATE TABLE evidence(value)")
                source.execute("INSERT INTO evidence VALUES('committed-in-wal')")
                source.commit()
                folder = library_store.snapshot(library)
                library_store.verify(folder)
                with sqlite3.connect(folder / "wacrawl.db") as copy:
                    self.assertEqual(
                        copy.execute("SELECT value FROM evidence").fetchone()[0],
                        "committed-in-wal",
                    )
                self.assertFalse((folder / "wacrawl.db-wal").exists())
                self.assertFalse((folder / "wacrawl.db-shm").exists())
            finally:
                source.close()

    def test_publications_are_immutable_generations_and_detect_corruption(self):
        with tempfile.TemporaryDirectory() as temporary:
            library = Path(temporary)
            with sqlite3.connect(library / "wacrawl.db") as connection:
                connection.execute("CREATE TABLE evidence(value)")
            first = library_store.snapshot(library)
            second = library_store.snapshot(library)
            self.assertNotEqual(first, second)
            (first / "wacrawl.db").write_bytes(b"invalid")
            with self.assertRaisesRegex(ValueError, "checksum"):
                library_store.verify(first)
            self.assertEqual(library_store.verify(second)["format"], 1)

    def test_rejects_snapshot_manifest_path_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "manifest.json").write_text(
                json.dumps({"format": 1, "databases": [{"file": "../outside"}]})
            )
            with self.assertRaisesRegex(ValueError, "filename"):
                library_store.verify(folder)


if __name__ == "__main__":
    unittest.main()
