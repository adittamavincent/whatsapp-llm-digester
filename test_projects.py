import contextlib
import io
import json
import shutil
import stat
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import digest
import launch
import projects
import whatsapp


class ChatTests(unittest.TestCase):
    def test_ios_android_multiline_system_and_twelve_hour_dates(self):
        text = (
            "\ufeff[25/09/26, 12.10.22] Ana: first\nsecond line\n"
            "26/09/2026, 1:05 PM - Ben: hello\n"
            "26/09/2026, 13:06 - Ana joined the group"
        )
        messages = whatsapp.parse_chat(text)
        self.assertEqual(messages[0]["timestamp"], "2026-09-25T12:10:22")
        self.assertEqual(messages[0]["body"], "first\nsecond line")
        self.assertEqual(messages[1]["timestamp"], "2026-09-26T13:05:00")
        self.assertIsNone(messages[2]["sender"])

    def test_date_order_is_explicit_and_invalid_dates_fail(self):
        text = "[10/03/26, 12:00] Ana: hi"
        self.assertEqual(
            whatsapp.parse_chat(text)[0]["timestamp"], "2026-03-10T12:00:00"
        )
        self.assertEqual(
            whatsapp.parse_chat(text, "month-first")[0]["timestamp"],
            "2026-10-03T12:00:00",
        )
        with self.assertRaises(ValueError):
            whatsapp.parse_chat("[31/02/26, 12:00] Ana: impossible")

    def test_repeated_messages_have_distinct_stable_occurrence_ids(self):
        message = {"timestamp": "2026-10-03T12:00:00", "sender": "Ana", "body": "same"}
        ids = [key for key, _ in whatsapp.message_ids([message, message], "_chat")]
        self.assertNotEqual(ids[0], ids[1])
        self.assertEqual(
            ids, [key for key, _ in whatsapp.message_ids([message, message], "_chat")]
        )

    def test_attachment_references(self):
        self.assertEqual(
            whatsapp.attachment_names("<attached: photo.jpg>"), ["photo.jpg"]
        )
        self.assertEqual(
            whatsapp.attachment_names("document.pdf (file attached)"), ["document.pdf"]
        )


class ProjectTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        project_root = patch.object(projects, "ROOT", self.root)
        project_root.start()
        self.addCleanup(project_root.stop)
        self.path = projects.initialize("example-project")

    def zip(self, name, chat, media=None, chat_name="_chat.txt"):
        path = self.path / "input" / name
        with zipfile.ZipFile(path, "w") as zipped:
            zipped.writestr(chat_name, chat)
            for member, data in (media or {}).items():
                zipped.writestr(member, data)
        return path

    def tar(self, name, chat, media=None, chat_name="_chat.txt"):
        path = self.path / "input" / name
        mode = "w:gz" if name.endswith(".gz") or name.endswith(".tgz") else "w"
        with tarfile.open(path, mode) as tar:
            data = chat.encode("utf-8")
            info = tarfile.TarInfo(name=chat_name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            for member, m_data in (media or {}).items():
                minfo = tarfile.TarInfo(name=member)
                minfo.size = len(m_data)
                tar.addfile(minfo, io.BytesIO(m_data))
        return path

    def ingest(self, order="day-first"):
        state = projects.load_state(self.path)
        errors = projects.ingest(self.path, state, order)
        self.assertEqual(errors, [])
        return state

    def test_creation_adopts_existing_structure_and_preserves_user_notes(self):
        scratch = self.path / "scratch"
        scratch.mkdir()
        (scratch / "note").write_text("keep")
        context = self.path / "CONTEXT.md"
        context.write_text("Actual project purpose, supplied by me.\n")
        readme = self.path / "README.md"
        readme.write_text(readme.read_text() + "\nMy own notes.\n")
        projects.initialize("example-project", "should not overwrite")
        self.assertEqual(
            context.read_text(), "Actual project purpose, supplied by me.\n"
        )
        self.assertIn("My own notes.", readme.read_text())
        self.assertEqual((scratch / "note").read_text(), "keep")
        self.assertTrue((self.path / "target/todo").is_dir())

    def test_each_project_has_local_pipeline_and_agent_context(self):
        readme = (self.path / "README.md").read_text()
        for phrase in (
            "Project isolation and background",
            "How conversion works",
            ".digester/state.json",
            "target/.manifest.json",
            "SHA-256",
            "three sampled frames",
            "Changed message text",
        ):
            self.assertIn(phrase, readme)
        agents = self.path / "AGENTS.md"
        self.assertIn("parent or sibling", agents.read_text())
        agents.write_text("My own project instructions.\n")
        projects.initialize("example-project")
        self.assertEqual(agents.read_text(), "My own project instructions.\n")

    def test_overlapping_newer_and_older_exports_deduplicate_and_preserve_history(self):
        early = "[01/10/26, 12:00] Ana: first\n"
        later = "[02/10/26, 12:00] Ben: <attached: photo.jpg>\n"
        latest = "[03/10/26, 12:00] Ana: third\n"
        self.zip("newest.zip", early + later + latest, {"photo.jpg": b"identical"})
        first = self.ingest()
        self.assertEqual(len(first["messages"]), 3)
        self.zip("older.zip", early + later, {"renamed.jpg": b"identical"})
        self.zip("oldest.zip", "[30/09/26, 10:00] Ana: earlier\n" + early)
        state = self.ingest()
        self.assertEqual(len(state["messages"]), 4)
        self.assertEqual(len(state["media"]), 1)
        self.assertEqual(len(list((self.path / "target/todo/media").rglob("*.jpg"))), 1)
        text = (self.path / "target/todo/conversation.md").read_text()
        self.assertLess(text.index("2026-09-30"), text.index("2026-10-01"))
        self.assertEqual(text.count("### 2026-10-02"), 1)
        self.assertIn("target/markdown/media/", text)
        self.assertEqual(len(state["checkpoints"]), 3)
        self.assertEqual(
            sorted(m["body"] for m in state["messages"].values()),
            ["<attached: photo.jpg>", "earlier", "first", "third"],
        )

    def test_duplicate_archive_name_does_not_create_another_checkpoint(self):
        original = self.zip("one.zip", "[01/10/26, 12:00] Ana: first")
        original_hash = digest.sha256(original)
        self.ingest()
        shutil.copyfile(original, self.path / "input" / "same-again.zip")
        state = self.ingest()
        self.assertEqual(len(state["checkpoints"]), 1)
        self.assertEqual(len(state["checkpoints"][original_hash]["archives"]), 2)
        self.assertEqual(digest.sha256(original), original_hash)

    def test_legacy_zip_inbox_migration_preserves_checkpoints_and_conversion_cache(
        self,
    ):
        original = self.zip(
            "one.zip", "[01/10/26, 12:00] Ana: hello", {"attachment.bin": b"one"}
        )
        commands = [
            "convert",
            "example-project",
            "--ocr-engine",
            "tesseract",
            "--doc-engine",
            "markitdown",
        ]
        self.assertEqual(launch.main(commands), 0)
        archive_hash = digest.sha256(original)
        manifest = (self.path / "target/.manifest.json").read_bytes()
        state = projects.load_state(self.path)
        state["checkpoints"][archive_hash]["archives"] = ["zips/one.zip"]
        projects.save_state(self.path, state)
        (self.path / "input").rename(self.path / "zips")
        agents = self.path / "AGENTS.md"
        agents.write_text(agents.read_text().replace("input/", "zips/"))
        projects.initialize("example-project")
        self.assertFalse((self.path / "zips").exists())
        self.assertEqual(digest.sha256(self.path / "input/one.zip"), archive_hash)
        migrated = projects.load_state(self.path)
        self.assertEqual(
            migrated["checkpoints"][archive_hash]["archives"], ["input/one.zip"]
        )
        self.assertEqual(len(migrated["messages"]), 1)
        self.assertEqual((self.path / "target/.manifest.json").read_bytes(), manifest)
        self.assertNotIn("zips/", agents.read_text())
        with patch.object(
            digest.Converter,
            "convert",
            side_effect=AssertionError("cache was invalidated"),
        ):
            self.assertEqual(launch.main(commands), 0)

    def test_migration_preserves_both_inboxes_even_when_zip_names_conflict(self):
        current = self.zip("same.zip", "[01/10/26, 12:00] Ana: current")
        checksum = digest.sha256(current)
        legacy = self.path / "zips"
        legacy.mkdir()
        with zipfile.ZipFile(legacy / "same.zip", "w") as zipped:
            zipped.writestr("_chat.txt", "[02/10/26, 12:00] Ana: legacy")
        legacy_checksum = digest.sha256(legacy / "same.zip")
        projects.initialize("example-project")
        self.assertEqual(digest.sha256(current), checksum)
        self.assertEqual(
            digest.sha256(self.path / "input/imported-from-zips/same.zip"),
            legacy_checksum,
        )
        self.assertFalse(legacy.exists())
        self.assertEqual(len(self.ingest()["checkpoints"]), 2)

    def test_interrupted_input_migration_resumes_after_folder_was_moved(self):
        archive = self.zip("one.zip", "[01/10/26, 12:00] Ana: hello")
        state = self.ingest()
        checksum = digest.sha256(archive)
        state["checkpoints"][checksum]["archives"] = ["zips/one.zip"]
        projects.save_state(self.path, state)
        journal = self.path / ".digester/input-migration.json"
        journal.write_text(json.dumps({"destination": "input"}))
        projects.initialize("example-project")
        self.assertFalse(journal.exists())
        self.assertEqual(
            projects.load_state(self.path)["checkpoints"][checksum]["archives"],
            ["input/one.zip"],
        )

    def test_same_filename_with_changed_bytes_is_a_separate_media_record(self):
        chat = "[01/10/26, 12:00] Ana: <attached: photo.jpg>"
        self.zip("one.zip", chat, {"photo.jpg": b"one"})
        self.ingest()
        self.zip("two.zip", chat, {"photo.jpg": b"two"})
        state = self.ingest()
        self.assertEqual(len(state["media"]), 2)
        self.assertEqual(len(next(iter(state["messages"].values()))["attachments"]), 2)

    def test_identical_repetition_is_retained_at_maximum_checkpoint_count(self):
        line = "[01/10/26, 12:00] Ana: same\n"
        self.zip("one.zip", line * 2)
        self.ingest()
        self.zip("two.zip", line * 3)
        state = self.ingest()
        self.assertEqual(len(state["messages"]), 3)

    def test_changed_text_is_retained_and_different_chat_labels_do_not_merge(self):
        self.zip("one.zip", "[01/10/26, 12:00] Ana: original")
        self.ingest()
        self.zip("two.zip", "[01/10/26, 12:00] Ana: correction")
        self.zip(
            "other.zip", "[01/10/26, 12:00] Ana: original", chat_name="Other Group.txt"
        )
        self.assertEqual(len(self.ingest()["messages"]), 3)

    def test_failed_checkpoint_does_not_commit_any_partial_state(self):
        self.zip(
            "bad.zip", "[31/02/26, 12:00] Ana: impossible", {"photo.jpg": b"photo"}
        )
        state = projects.load_state(self.path)
        errors = projects.ingest(self.path, state, "day-first")
        self.assertEqual(len(errors), 1)
        self.assertEqual(state["checkpoints"], {})
        self.assertFalse((self.path / "target/todo/media").exists())

    def test_plain_text_attachment_is_preserved_without_becoming_chat_history(self):
        self.zip(
            "notes.zip",
            "[01/10/26, 12:00] Ana: hello",
            {"notes.txt": b"Reference notes, not a dated chat."},
        )
        state = self.ingest()
        self.assertEqual(len(state["messages"]), 1)
        self.assertEqual(len(state["media"]), 1)
        record = next(iter(state["media"].values()))
        self.assertEqual(
            (self.path / "target/todo" / record["source"]).read_bytes(),
            b"Reference notes, not a dated chat.",
        )

    def test_named_chat_without_dates_is_rejected_instead_of_converted_as_attachment(
        self,
    ):
        self.zip("bad.zip", "Undated text in _chat.txt")
        state = projects.load_state(self.path)
        errors = projects.ingest(self.path, state, "day-first")
        self.assertEqual(len(errors), 1)
        self.assertIn("No dated WhatsApp messages", errors[0])
        self.assertEqual(state["checkpoints"], {})

    def test_zip_paths_and_symlinks_are_rejected(self):
        for member in (
            "../outside.txt",
            "/absolute.txt",
            "C:/outside.txt",
            "dir\\outside.txt",
        ):
            with self.subTest(member=member):
                info = zipfile.ZipInfo(member)
                with self.assertRaises(ValueError):
                    projects.safe_member(info)
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(ValueError):
            projects.safe_member(info)

    def test_missing_extracted_media_is_restored_without_new_messages(self):
        self.zip("one.zip", "[01/10/26, 12:00] Ana: hello", {"photo.jpg": b"photo"})
        state = self.ingest()
        media = next(iter(state["media"].values()))
        source = self.path / "target/todo" / media["source"]
        source.unlink()
        repaired = self.ingest()
        self.assertEqual(source.read_bytes(), b"photo")
        self.assertEqual(len(repaired["messages"]), 1)
        self.assertEqual(len(repaired["checkpoints"]), 1)

    def test_project_date_order_cannot_silently_change_after_import(self):
        self.zip("one.zip", "[10/03/26, 12:00] Ana: hello")
        state = self.ingest("month-first")
        with self.assertRaisesRegex(ValueError, "month-first"):
            projects.ingest(self.path, state, "day-first")

    def test_convert_remembers_first_import_date_order(self):
        self.zip("one.zip", "[10/03/26, 12:00] Ana: hello")
        commands = [
            "convert",
            "example-project",
            "--ocr-engine",
            "tesseract",
            "--doc-engine",
            "markitdown",
        ]
        self.assertEqual(launch.main([*commands, "--date-order", "month-first"]), 0)
        self.assertEqual(launch.main(commands), 0)
        state = projects.load_state(self.path)
        self.assertEqual(state["date_order"], "month-first")
        self.assertEqual(
            next(iter(state["messages"].values()))["timestamp"], "2026-10-03T12:00:00"
        )

    def test_project_names_and_concurrent_conversion_are_guarded(self):
        for name in ("../escape", "two words", "", "Upper"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                projects.project_path(name)
        with (
            projects.project_lock(self.path),
            self.assertRaisesRegex(RuntimeError, "Another conversion"),
            projects.project_lock(self.path),
        ):
            pass

    def test_target_context_restores_legacy_paths_after_failure(self):
        original = digest.TARGET_DIR, digest.TODO_DIR, digest.OUTPUT_DIR
        with (
            self.assertRaises(RuntimeError),
            digest.target_directory(self.path / "target"),
        ):
            self.assertEqual(digest.TODO_DIR, (self.path / "target/todo").resolve())
            raise RuntimeError("failed")
        self.assertEqual(
            (digest.TARGET_DIR, digest.TODO_DIR, digest.OUTPUT_DIR), original
        )

    def test_end_to_end_incremental_conversion_retries_missing_outputs(self):
        self.zip("one.zip", "[01/10/26, 12:00] Ana: hello", {"attachment.bin": b"one"})
        commands = [
            "convert",
            "example-project",
            "--ocr-engine",
            "tesseract",
            "--doc-engine",
            "markitdown",
        ]
        original_convert = digest.Converter.convert
        calls = []

        def counted(converter, path, kind):
            calls.append(path.name)
            return original_convert(converter, path, kind)

        with patch.object(digest.Converter, "convert", counted):
            self.assertEqual(launch.main(commands), 0)
            self.assertEqual(len(calls), 2)
            self.assertEqual(launch.main(commands), 0)
            self.assertEqual(len(calls), 2)
            self.zip(
                "two.zip",
                "[01/10/26, 12:00] Ana: hello\n[02/10/26, 12:00] Ana: new",
                {"renamed.bin": b"one"},
            )
            self.assertEqual(launch.main(commands), 0)
            self.assertEqual(len(calls), 3)
            media_output = next((self.path / "target/markdown/media").rglob("*.md"))
            media_output.unlink()
            self.assertEqual(launch.main(commands), 0)
            self.assertEqual(len(calls), 4)
            self.assertEqual(launch.main([*commands, "--language", "id"]), 0)
            self.assertEqual(len(calls), 6)
        combined = (self.path / "target/combined.md").read_text()
        self.assertEqual(combined.count("### 2026-10-01"), 1)
        self.assertEqual(combined.count("### 2026-10-02"), 1)
        self.assertEqual(combined.count("<!-- BEGIN SOURCE: todo/media/"), 1)

    def test_default_lists_projects_and_new_works_through_launcher(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(launch.main([]), 0)
            self.assertEqual(
                launch.main(
                    ["new", "another-project", "--context", "Archival curation."]
                ),
                0,
            )
        self.assertIn("example-project", output.getvalue())
        self.assertEqual(
            (self.root / "another-project/CONTEXT.md").read_text(),
            "Archival curation.\n",
        )

    def test_loose_media_file_in_input_is_imported_and_restored_on_deletion(self):
        audio_file = self.path / "input" / "System Audio 20261004 1602.mp3"
        audio_file.write_bytes(b"synthetic audio content")
        state = self.ingest()
        self.assertEqual(len(state["checkpoints"]), 1)
        self.assertEqual(len(state["media"]), 1)
        self.assertEqual(len(state["messages"]), 0)
        media_record = next(iter(state["media"].values()))
        staged = self.path / "target/todo" / media_record["source"]
        self.assertTrue(staged.is_file())
        self.assertEqual(staged.read_bytes(), b"synthetic audio content")
        checkpoints_md = (self.path / "target/checkpoints.md").read_text()
        self.assertIn("System Audio 20261004 1602.mp3", checkpoints_md)

        # Deleting the staged media is restored from original in input/
        staged.unlink()
        self.assertFalse(staged.exists())
        self.ingest()
        self.assertTrue(staged.is_file())
        self.assertEqual(staged.read_bytes(), b"synthetic audio content")

    def test_unzipped_export_directory_is_imported_as_checkpoint(self):
        export_dir = self.path / "input" / "WhatsApp Chat - Project"
        export_dir.mkdir(parents=True)
        (export_dir / "_chat.txt").write_text("[01/10/26, 12:00] Ana: hello <attached: photo.jpg>\n", encoding="utf-8")
        (export_dir / "photo.jpg").write_bytes(b"photo bytes")
        state = self.ingest()
        self.assertEqual(len(state["checkpoints"]), 1)
        self.assertEqual(len(state["messages"]), 1)
        self.assertEqual(len(state["media"]), 1)
        checkpoint = next(iter(state["checkpoints"].values()))
        self.assertEqual(checkpoint["archives"], ["input/WhatsApp Chat - Project"])
        msg = next(iter(state["messages"].values()))
        self.assertEqual(len(msg["attachments"]), 1)

        # Restores missing media from the unzipped directory
        staged = self.path / "target/todo" / next(iter(state["media"].values()))["source"]
        staged.unlink()
        self.ingest()
        self.assertTrue(staged.is_file())

    def test_tar_archive_is_imported_as_checkpoint(self):
        self.tar(
            "export.tar.gz",
            "[01/10/26, 12:00] Ana: hello from tar",
            {"attachment.png": b"png data"},
        )
        state = self.ingest()
        self.assertEqual(len(state["checkpoints"]), 1)
        self.assertEqual(len(state["messages"]), 1)
        self.assertEqual(len(state["media"]), 1)
        checkpoint = next(iter(state["checkpoints"].values()))
        self.assertEqual(checkpoint["archives"], ["input/export.tar.gz"])

    def test_standalone_chat_txt_file_and_loose_media_link_attachments(self):
        (self.path / "input" / "photo.jpg").write_bytes(b"photo data")
        (self.path / "input" / "WhatsApp Chat with Bob.txt").write_text(
            "[01/10/26, 12:00] Bob: look here <attached: photo.jpg>\n", encoding="utf-8"
        )
        state = self.ingest()
        self.assertEqual(len(state["checkpoints"]), 2)
        self.assertEqual(len(state["messages"]), 1)
        self.assertEqual(len(state["media"]), 1)
        msg = next(iter(state["messages"].values()))
        self.assertEqual(len(msg["attachments"]), 1)

    def test_archive_without_chat_file_imports_media_instead_of_failing(self):
        archive = self.path / "input" / "just-photos.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("photo.jpg", b"photo bytes")
        state = self.ingest()
        self.assertEqual(len(state["checkpoints"]), 1)
        self.assertEqual(len(state["media"]), 1)
        self.assertEqual(len(state["messages"]), 0)

    def test_count_inputs_with_diverse_input_types(self):
        # 1. Zip
        self.zip("one.zip", "[01/10/26, 12:00] Ana: hello")
        # 2. Loose file
        (self.path / "input" / "audio.mp3").write_bytes(b"audio")
        # 3. Export dir
        export_dir = self.path / "input" / "export-folder"
        export_dir.mkdir()
        (export_dir / "_chat.txt").write_text("[01/10/26, 12:00] Ana: hi", encoding="utf-8")
        # 4. Folder of loose files (not an export dir)
        loose_dir = self.path / "input" / "recordings"
        loose_dir.mkdir()
        (loose_dir / "rec1.mp3").write_bytes(b"rec1")
        (loose_dir / "rec2.mp3").write_bytes(b"rec2")

        count = projects.count_inputs(self.path / "input")
        # one.zip (1) + audio.mp3 (1) + export-folder (1) + rec1.mp3 (1) + rec2.mp3 (1) = 5
        self.assertEqual(count, 5)


if __name__ == "__main__":
    unittest.main()
