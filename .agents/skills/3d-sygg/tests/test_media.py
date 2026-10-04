from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from media import (  # noqa: E402
    image_pixel_hash,
    mix_narration,
    normalize_clip,
    probe_media,
    stitch_clips,
    write_qa_report,
)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg is unavailable")
class MediaPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _fixture(self, name: str, color: str, *, with_audio: bool) -> Path:
        target = self.root / name
        command = [
            shutil.which("ffmpeg"), "-y", "-f", "lavfi", "-i", f"color=c={color}:s=320x240:r=24:d=2",
        ]
        if with_audio:
            command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2"]
        command += ["-t", "2", "-c:v", "mpeg4"]
        if with_audio:
            command += ["-c:a", "aac"]
        command += [str(target)]
        subprocess.run(command, check=True, capture_output=True)
        return target

    def test_normalize_and_stitch_yields_decodable_vertical_720p_mp4(self) -> None:
        first = normalize_clip(
            self._fixture("a.mp4", "red", with_audio=True), self.root / "n1.mp4", 1,
            target_width=720, target_height=1280,
        )
        second = normalize_clip(
            self._fixture("b.mp4", "blue", with_audio=True), self.root / "n2.mp4", 1,
            target_width=720, target_height=1280,
        )
        final = stitch_clips([first, second], self.root / "final.mp4")
        info = probe_media(final)
        self.assertEqual((info["width"], info["height"]), (720, 1280))
        self.assertTrue(info["has_audio"])
        self.assertAlmostEqual(info["duration"], 2.0, delta=0.35)
        report = write_qa_report(
            final, self.root / "qa.json", expected_duration=2, narration_expected=False,
            expected_width=720, expected_height=1280,
        )
        self.assertEqual(report["technical_status"], "pass")
        self.assertEqual(report["status"], "awaiting_manual_review")

    def test_narration_is_loudness_normalized_ducked_and_mixed(self) -> None:
        video = normalize_clip(
            self._fixture("ambient.mp4", "black", with_audio=True), self.root / "ambient-normalized.mp4", 1,
            target_width=720, target_height=1280,
        )
        narration = self.root / "narration.mp3"
        subprocess.run(
            [
                shutil.which("ffmpeg"), "-y", "-f", "lavfi", "-i",
                "sine=frequency=880:sample_rate=32000:duration=0.8",
                "-c:a", "libmp3lame", "-b:a", "128k", str(narration),
            ],
            check=True,
            capture_output=True,
        )
        mixed = mix_narration(video, narration, self.root / "mixed.mp4")
        info = probe_media(mixed)
        self.assertEqual((info["width"], info["height"]), (720, 1280))
        self.assertTrue(info["has_audio"])
        self.assertAlmostEqual(info["duration"], 1.0, delta=0.35)

    def test_image_pixel_hash_recognizes_reencoded_identical_pixels(self) -> None:
        source = self._fixture("source.mp4", "red", with_audio=False)
        first = self.root / "first.png"
        second = self.root / "second.png"
        ffmpeg = shutil.which("ffmpeg")
        subprocess.run([ffmpeg, "-y", "-i", str(source), "-frames:v", "1", str(first)], check=True, capture_output=True)
        subprocess.run(
            [ffmpeg, "-y", "-i", str(source), "-frames:v", "1", "-compression_level", "9", str(second)],
            check=True,
            capture_output=True,
        )
        self.assertNotEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(image_pixel_hash(first), image_pixel_hash(second))

    def test_normalize_yields_decodable_horizontal_720p_mp4(self) -> None:
        final = normalize_clip(
            self._fixture("horizontal-source.mp4", "green", with_audio=True),
            self.root / "horizontal.mp4",
            1,
            target_width=1280,
            target_height=720,
        )
        info = probe_media(final)
        self.assertEqual((info["width"], info["height"]), (1280, 720))
        report = write_qa_report(
            final,
            self.root / "horizontal-qa.json",
            expected_duration=1,
            narration_expected=False,
            expected_width=1280,
            expected_height=720,
        )
        self.assertTrue(report["checks"]["resolution_matches_target"])


if __name__ == "__main__":
    unittest.main()
