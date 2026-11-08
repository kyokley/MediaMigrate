import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mediamigrate import media


class MediaPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "already-encoded video.bin"
        self.source.write_bytes(b"already encoded video bytes")
        self.subtitle = self.root / "captions.vtt"
        self.subtitle.write_text(
            "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n", encoding="utf-8"
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_preparation_returns_original_paths_and_preserves_bytes_without_tools(self):
        originals = self.source.read_bytes(), self.subtitle.read_bytes()
        with patch.dict(os.environ, {"PATH": ""}):
            prepared = media.prepare_video(self.source, [self.subtitle])

        self.assertEqual(prepared.video, self.source)
        self.assertEqual(prepared.subtitles, (self.subtitle,))
        self.assertEqual(
            prepared.fingerprint, media.source_fingerprint(self.source, [self.subtitle])
        )
        self.assertEqual(
            (self.source.read_bytes(), self.subtitle.read_bytes()), originals
        )

    def test_fingerprint_is_stable_and_tracks_video_and_subtitle_bytes(self):
        initial = media.source_fingerprint(self.source, [self.subtitle])
        self.assertEqual(
            initial, media.source_fingerprint(self.source, [self.subtitle])
        )

        self.source.write_bytes(b"changed video")
        changed_video = media.source_fingerprint(self.source, [self.subtitle])
        self.assertNotEqual(initial, changed_video)

        self.subtitle.write_text("WEBVTT\n\nchanged caption\n", encoding="utf-8")
        self.assertNotEqual(
            changed_video, media.source_fingerprint(self.source, [self.subtitle])
        )

    def test_fingerprint_tracks_source_identity(self):
        other = self.root / "other-name.bin"
        other.write_bytes(self.source.read_bytes())
        self.assertNotEqual(
            media.source_fingerprint(self.source, []),
            media.source_fingerprint(other, []),
        )

    def test_inspection_rejects_missing_empty_or_nonregular_video(self):
        with self.assertRaises(media.PreparationError):
            media.inspect_video(self.root / "missing.bin", [])

        empty = self.root / "empty.bin"
        empty.touch()
        with self.assertRaises(media.PreparationError):
            media.inspect_video(empty, [])

        with self.assertRaises(media.PreparationError):
            media.inspect_video(self.root, [])

    def test_inspection_rejects_missing_invalid_empty_and_unsupported_captions(self):
        with self.assertRaises(media.PreparationError):
            media.inspect_video(self.source, [self.root / "missing.vtt"])

        empty = self.root / "empty.vtt"
        empty.touch()
        with self.assertRaises(media.PreparationError):
            media.inspect_video(self.source, [empty])

        invalid = self.root / "invalid.vtt"
        invalid.write_text("not a webvtt file\n", encoding="utf-8")
        with self.assertRaises(media.PreparationError):
            media.inspect_video(self.source, [invalid])

        srt = self.root / "captions.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        with self.assertRaises(media.PreparationError):
            media.inspect_video(self.source, [srt])

    def test_inspection_rejects_duplicate_source_references(self):
        with self.assertRaises(media.PreparationError):
            media.inspect_video(self.source, [self.subtitle, self.subtitle])

        with self.assertRaises(media.PreparationError):
            media.inspect_video(self.source, [self.source])

    def test_prepare_rechecks_inputs_and_preserves_original_files(self):
        originals = self.source.read_bytes(), self.subtitle.read_bytes()
        prepared = media.prepare_video(self.source, [self.subtitle])
        self.assertEqual(
            prepared.fingerprint, media.source_fingerprint(self.source, [self.subtitle])
        )
        self.assertEqual(self.source.read_bytes(), originals[0])
        self.assertEqual(self.subtitle.read_bytes(), originals[1])


if __name__ == "__main__":
    unittest.main()
