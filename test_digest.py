import unittest
from pathlib import Path

import digest


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
        self.assertEqual(digest.strip_thinking("<think>hidden</think>Visible"), "Visible")

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


if __name__ == "__main__":
    unittest.main()
