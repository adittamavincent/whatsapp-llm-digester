import contextlib
import io
import json
import plistlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import digest
import iphone_backup as iphone


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backup = self.root / "backup"
        self.backup.mkdir()

    def fixture(self, encrypted=False, state="finished", payload=True):
        for filename, content in {
            "Manifest.plist": {"IsEncrypted": encrypted},
            "Info.plist": {"Product Version": "26.0"},
            "Status.plist": {"SnapshotState": state},
        }.items():
            (self.backup / filename).write_bytes(plistlib.dumps(content))
        if encrypted:
            (self.backup / "Manifest.db").write_bytes(b"encrypted fixture")
        else:
            with sqlite3.connect(self.backup / "Manifest.db") as c:
                c.execute("CREATE TABLE Files(fileID,domain,relativePath,flags)")
                c.execute(
                    "INSERT INTO Files VALUES(?,?,?,1)",
                    (
                        "a" * 40,
                        "AppDomainGroup-group.net.whatsapp.WhatsApp.shared",
                        "ChatStorage.sqlite",
                    ),
                )
            if payload:
                (self.backup / "aa").mkdir()
                (self.backup / "aa" / ("a" * 40)).write_bytes(b"fixture")

    def test_empty_folders_are_not_backups(self):
        result = iphone.inspect_backup(self.backup)
        self.assertFalse(result["usable"])
        self.assertIn("Manifest.db", result["missing_files"])

    def test_manifest_is_readonly_and_absent_payloads_are_reported(self):
        self.fixture(payload=False)
        checksum = digest.sha256(self.backup / "Manifest.db")
        result = iphone.inspect_backup(self.backup)
        self.assertFalse(result["usable"])
        self.assertEqual(result["absent_payload_files"], 1)
        self.assertEqual(digest.sha256(self.backup / "Manifest.db"), checksum)

    def test_complete_manifest_is_candidate_not_claim_of_full_history(self):
        self.fixture()
        result = iphone.inspect_backup(self.backup)
        self.assertTrue(result["usable"])
        self.assertEqual(result["coverage"], "unverified")

    def test_unfinished_backup_is_rejected(self):
        self.fixture(state="new")
        self.assertFalse(iphone.inspect_backup(self.backup)["usable"])

    def test_encrypted_backup_needs_terminal_without_collecting_password(self):
        self.fixture(encrypted=True)
        with (
            patch.object(iphone.sys.stdin, "isatty", return_value=False),
            self.assertRaisesRegex(ValueError, "Terminal"),
        ):
            iphone.export_backup(self.backup, self.root / "snapshots")
        self.assertFalse((self.root / "snapshots").exists())

    def test_export_failure_keeps_report_and_source_unchanged(self):
        self.fixture()
        checksum = digest.sha256(self.backup / "Manifest.db")
        with (
            patch.object(iphone.subprocess, "call", return_value=7),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code, folder = iphone.export_backup(self.backup, self.root / "snapshots")
        self.assertEqual(code, 7)
        self.assertEqual(
            json.loads((folder / "snapshot.json").read_text())["state"], "failed"
        )
        self.assertEqual(digest.sha256(self.backup / "Manifest.db"), checksum)

    def test_export_to_overlapping_directory_is_rejected(self):
        self.fixture()
        with self.assertRaisesRegex(ValueError, "overlap"):
            iphone.export_backup(self.backup, self.backup / "output")

    def test_export_success_records_counts_and_pinned_runtime(self):
        self.fixture()

        def exporter(command, cwd):
            self.assertIn(iphone.EXPORTER, command)
            self.assertIn("--no-html", command)
            self.assertNotIn("--no-reply", command)
            self.assertNotIn("-c", command)
            (cwd / "result.json").write_text(
                json.dumps(
                    {
                        "chat": {
                            "messages": {
                                "1": {
                                    "media": True,
                                    "mime": "media",
                                    "reply": "old-id",
                                },
                                "2": {"media": False, "data": "text"},
                            }
                        }
                    }
                )
            )
            return 0

        with (
            patch.object(iphone.subprocess, "call", side_effect=exporter),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code, folder = iphone.export_backup(self.backup, self.root / "snapshots")
        report = json.loads((folder / "snapshot.json").read_text())
        self.assertEqual(code, 0)
        self.assertEqual(report["state"], "exported")
        self.assertEqual(report["message_records"], 2)
        self.assertEqual(report["reply_records"], 1)
        self.assertEqual(report["missing_media_reported_by_exporter"], 1)
        self.assertEqual(report["coverage"], "unverified")


if __name__ == "__main__":
    unittest.main()
