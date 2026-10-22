import hashlib
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave
import zlib

from compose_video import music_candidates, plan_video, probe, render_video, select_music
from image_jobs import write


def png(path, color):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 48, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress((b"\x00" + bytes(color) * 32) * 48)) + chunk(b"IEND", b""))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class VideoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="comic 测试 ' ")
        self.topic = Path(self.temp.name)
        (self.topic / "images/comic").mkdir(parents=True)
        (self.topic / "content.md").write_text("A story")
        self.shots = []
        self.selected = []
        for i, color in enumerate([(240, 10, 10), (10, 10, 240)], 1):
            path = self.topic / f"images/comic/{i}.png"
            png(path, color)
            self.shots.append(dict(id=str(i), title=str(i), text=dict(narration="test", dialogues=[])))
            self.selected.append(dict(id=str(i), file=f"comic/{i}.png", status="generated", review="passed",
                                      sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        write(self.topic / "stage3-prompts.json", dict(source_content="content.md",
              source_sha256=hashlib.sha256(b"A story").hexdigest(), shots=self.shots))
        # Manifest storage order must not change story order.
        write(self.topic / "images/comic-manifest.json", dict(shots=self.selected[::-1]))
        self.music = self.topic / "my music"
        self.music.mkdir()
        self.track = self.music / "古筝 ' test.wav"
        with wave.open(str(self.track), "wb") as out:
            out.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            out.writeframes(b"".join(struct.pack("<h", int(5000 * math.sin(i * 2 * math.pi * 440 / 8000)))
                                      for i in range(2000)))
        write(self.topic / "video.json", dict(width=64, height=96, fps=10, durations={"1": 0.6, "2": 0.8}))
        music_candidates(self.topic, self.music)
        select_music(self.topic, "1", "测试本地音乐", "科普")

    def tearDown(self):
        self.temp.cleanup()

    def test_local_folder_has_priority_and_invalid_folder_does_not_fetch(self):
        with patch("compose_video.fetch", side_effect=AssertionError("Unexpected network")):
            tracks = music_candidates(self.topic, self.music, query="piano")
            self.assertEqual(len(tracks), 1)
            with self.assertRaises(ValueError):
                music_candidates(self.topic, self.topic / "missing", query="piano")

    def test_plan_checks_source_order_hashes_and_default_short_pacing(self):
        config = json.loads((self.topic / "video.json").read_text())
        config.pop("durations")
        write(self.topic / "video.json", config)
        plan = plan_video(self.topic)
        self.assertEqual([s["id"] for s in plan["timeline"]], ["1", "2"])
        self.assertEqual([s["duration"] for s in plan["timeline"]], [3.5, 3.5])
        self.assertEqual(plan["motion"]["type"], "none")
        self.assertEqual(plan["transition"], {"type": "fade", "duration": 0.5})
        (self.topic / "images/comic/1.png").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            plan_video(self.topic)

    def test_missing_shot_changed_music_and_invalid_timing_fail(self):
        config = json.loads((self.topic / "video.json").read_text())
        config["durations"]["1"] = -1
        write(self.topic / "video.json", config)
        with self.assertRaises(ValueError):
            plan_video(self.topic)
        config["durations"]["1"] = 0.6
        write(self.topic / "video.json", config)
        self.track.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            plan_video(self.topic)
        write(self.topic / "images/comic-manifest.json", dict(shots=self.selected[:1]))
        with self.assertRaises(ValueError):
            plan_video(self.topic)

    def test_remote_candidates_exclude_unusable_licenses_and_tiny_effects(self):
        config = json.loads((self.topic / "video.json").read_text())
        config.pop("bgm_dir")
        write(self.topic / "video.json", config)
        data = dict(results=[dict(id="ok", license="cc0", duration=30000),
                             dict(id="no", license="by-nc", duration=30000),
                             dict(id="short", license="cc0", duration=1000)])
        with patch("compose_video.fetch", return_value=json.dumps(data).encode()):
            result = music_candidates(self.topic, query="calm instrumental", source="openverse")
        self.assertEqual([t["id"] for t in result], ["ok"])

    def test_popular_music_mode_does_not_silently_use_stock_music(self):
        config = json.loads((self.topic / "video.json").read_text())
        config.pop("bgm_dir")
        write(self.topic / "video.json", config)
        with patch("compose_video.fetch", side_effect=AssertionError("Unexpected network")):
            with self.assertRaisesRegex(ValueError, "平台热歌"):
                music_candidates(self.topic, query="guzheng")

    def test_platform_music_is_explicit_pending_and_local_selection_restores_audio(self):
        config = json.loads((self.topic / "video.json").read_text())
        config.pop("bgm")
        write(self.topic / "video.json", config)
        with self.assertRaisesRegex(ValueError, "未选择 BGM"):
            plan_video(self.topic)
        config["bgm_mode"] = "platform"
        write(self.topic / "video.json", config)
        output = render_video(self.topic)
        self.assertEqual({s["codec_type"] for s in probe(output)["streams"]}, {"video"})
        metadata = json.loads(output.with_suffix(".json").read_text())
        self.assertEqual(metadata["status"], "awaiting_platform_music")
        self.assertIn("platform", output.name)
        select_music(self.topic, "1", "新音源恢复混音", "科普")
        self.assertEqual(plan_video(self.topic)["bgm_mode"], "embedded")

    def test_real_video_preserves_order_audio_length_and_previous_export(self):
        output = render_video(self.topic)
        info = probe(output)
        self.assertEqual({s["codec_name"] for s in info["streams"]}, {"h264", "aac"})
        self.assertAlmostEqual(float(info["format"]["duration"]), 1.4, delta=0.11)
        for second, dominant in [(0.1, 0), (1.1, 2)]:
            pixels = subprocess.check_output(["ffmpeg", "-v", "error", "-ss", str(second), "-i", str(output),
                                               "-frames:v", "1", "-vf", "scale=2:2", "-pix_fmt", "rgb24",
                                               "-f", "rawvideo", "pipe:1"])
            self.assertGreater(pixels[dominant], 180)
        blended = subprocess.check_output(["ffmpeg", "-v", "error", "-ss", "0.8", "-i", str(output),
                                             "-frames:v", "1", "-vf", "scale=2:2", "-pix_fmt", "rgb24",
                                             "-f", "rawvideo", "pipe:1"])
        self.assertGreater(blended[0], 60)
        self.assertGreater(blended[2], 60)
        audio = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(output), "-map", "0:a:0",
                                         "-f", "s16le", "pipe:1"])
        samples = struct.unpack("<" + "h" * (len(audio) // 2), audio)
        self.assertGreater(max(abs(s) for s in samples), 100)
        before = output.read_bytes()
        config = json.loads((self.topic / "video.json").read_text())
        config["motion"] = {"type": "none"}
        write(self.topic / "video.json", config)
        again = render_video(self.topic)
        self.assertNotEqual(output, again)
        self.assertEqual(output.read_bytes(), before)
        self.assertTrue(again.with_suffix(".json").exists())

    def test_motion_works_without_transitions_and_keeps_full_duration(self):
        config = json.loads((self.topic / "video.json").read_text())
        config.update(transition={"type": "none"}, motion={"type": "zoom", "amount": 0.05})
        write(self.topic / "video.json", config)
        output = render_video(self.topic)
        counts = []
        for second in (0, 0.5):
            raw = subprocess.check_output(["ffmpeg", "-v", "error", "-ss", str(second), "-i", str(output),
                                            "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"])
            counts.append(sum(raw[i] > 180 and raw[i+1] < 60 and raw[i+2] < 60 for i in range(0, len(raw), 3)))
        self.assertGreater(counts[1], counts[0])
        self.assertAlmostEqual(float(probe(output)["format"]["duration"]), 1.4, delta=0.11)

    def test_rejects_excessive_transitions_and_motion(self):
        config = json.loads((self.topic / "video.json").read_text())
        for settings in ({"transition": {"type": "fade", "duration": 0.8}},
                         {"transition": {"type": "invalid"}}, {"motion": {"type": "zoom", "amount": 0.5}}):
            write(self.topic / "video.json", {**config, **settings})
            with self.assertRaises(ValueError):
                plan_video(self.topic)

    def test_blur_and_soft_slide_render_with_motion_disabled(self):
        config = json.loads((self.topic / "video.json").read_text())
        for kind in ("hblur", "smoothleft"):
            with self.subTest(transition=kind):
                write(self.topic / "video.json", {**config, "transition": {"type": kind, "duration": 0.3},
                                                  "motion": {"type": "none"}})
                info = probe(render_video(self.topic))
                self.assertAlmostEqual(float(info["format"]["duration"]), 1.4, delta=0.11)
                self.assertEqual({s["codec_name"] for s in info["streams"]}, {"h264", "aac"})


if __name__ == "__main__":
    unittest.main()
