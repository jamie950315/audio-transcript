"""Exercise audio preparation and ordering with ffmpeg and unpaid fixtures."""
import base64
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class AudioPreparation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.short = cls.root / "short.m4a"
        cls.long = cls.root / "long.m4a"
        for path, duration in ((cls.short, 2), (cls.long, 241)):
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=16000",
                            "-t", str(duration), "-c:a", "aac", str(path)], capture_output=True, check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_short_audio_encodes_once(self):
        real_run = subprocess.run
        with patch.object(main.subprocess, "run", wraps=real_run) as run, \
                patch.object(main, "_openrouter_transcribe", return_value="Short fixture.") as provider:
            segments = main._transcribe_audio_file(self.short)
        encodes = [call for call in run.call_args_list if call.args[0][0] == "ffmpeg"]
        self.assertEqual(len(encodes), 1)
        provider.assert_called_once()
        self.assertEqual(segments[0]["text"], "Short fixture.")
        self.assertAlmostEqual(segments[0]["duration"], 2, places=1)

    def test_long_audio_encodes_once_and_preserves_order(self):
        real_run = subprocess.run
        payload_indexes = {}
        formats = []
        barrier = threading.Barrier(3)

        def run(args, **kwargs):
            result = real_run(args, **kwargs)
            if args[0] == "ffmpeg":
                for index, path in enumerate(sorted(Path(args[-1]).parent.glob("chunk_*.mp3"))):
                    payload_indexes[base64.b64encode(path.read_bytes()).decode()] = index
                    probe = real_run(["ffprobe", "-v", "error", "-show_entries", "stream=sample_rate,channels,codec_name",
                                      "-of", "json", str(path)], capture_output=True, text=True, check=True)
                    formats.append(json.loads(probe.stdout)["streams"][0])
            return result

        def transcribe(audio, fmt="mp3"):
            barrier.wait(timeout=5)
            return f"Chunk {payload_indexes[audio]}."

        with patch.object(main.subprocess, "run", side_effect=run) as commands, \
                patch.object(main, "_silence_cut_points", return_value=[120.0, 240.0]), \
                patch.object(main, "_openrouter_transcribe", side_effect=transcribe):
            segments = main._transcribe_audio_file(self.long)
        self.assertEqual(len([call for call in commands.call_args_list if call.args[0][0] == "ffmpeg"]), 1)
        self.assertEqual([segment["text"] for segment in segments], ["Chunk 0.", "Chunk 1.", "Chunk 2."])
        self.assertEqual([segment["start"] for segment in segments], [0, 120, 240])
        self.assertAlmostEqual(sum(segment["duration"] for segment in segments), 241, places=1)
        self.assertEqual(formats, [{"codec_name": "mp3", "sample_rate": "16000", "channels": 1}] * 3)

    def test_chunk_failure_rejects_partial_transcript(self):
        with patch.object(main, "_openrouter_transcribe", side_effect=RuntimeError("Fixture provider failed")):
            with self.assertRaisesRegex(RuntimeError, "failed for chunks"):
                main._transcribe_audio_file(self.long)

    def test_download_passes_native_audio_to_single_encoder(self):
        options = []
        native = self.short

        class Downloader:
            def __init__(self, opts):
                options.append(opts)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def extract_info(self, url, download):
                self.download = download
                return {"id": "fixture1234", "ext": "m4a"}
            def prepare_filename(self, info):
                return str(native)

        with patch.object(main.yt_dlp, "YoutubeDL", Downloader), \
                patch.object(main, "_transcribe_audio_file", return_value=[{"text": "Fixture"}]) as transcribe:
            result = main._download_and_transcribe("https://www.youtube.com/watch?v=fixture1234")
        self.assertNotIn("postprocessors", options[0])
        transcribe.assert_called_once_with(native)
        self.assertEqual(result, [{"text": "Fixture"}])

    def test_invalid_duration_fails_explicitly(self):
        for value in ("N/A", "nan", "inf", "0", "-1"):
            with self.subTest(value=value), \
                    patch.object(main.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=value)):
                with self.assertRaisesRegex(RuntimeError, "Cannot determine audio duration"):
                    main._get_duration(self.short)



class SegmentTiming(unittest.TestCase):
    def test_sentence_split_keeps_decimals(self):
        segs = main._text_to_segments("電壓是 0.3，所以 Vi 等於 0.673。對不對？Done. Next!", 0, 10)
        self.assertEqual([s["text"] for s in segs],
                         ["電壓是 0.3，所以 Vi 等於 0.673。", "對不對？", "Done.", "Next!"])

    def test_duration_is_weighted_by_length_and_fills_chunk(self):
        segs = main._text_to_segments("短。" + "這是一個比較長很多的句子。", 100, 10)
        self.assertLess(segs[0]["duration"], segs[1]["duration"])
        self.assertEqual(segs[0]["start"], 100)
        self.assertAlmostEqual(segs[-1]["start"] + segs[-1]["duration"], 110, places=2)

    def test_empty_text_gives_no_segments(self):
        self.assertEqual(main._text_to_segments("", 0, 30), [])

    def _cuts(self, stderr, duration):
        fake = type("R", (), {"stderr": stderr})()
        with patch.object(main.subprocess, "run", return_value=fake):
            return main._silence_cut_points(Path("x.mp3"), duration)

    def test_cut_points_prefer_silence_near_target(self):
        stderr = "\n".join([
            "[silencedetect] silence_start: 21.0", "[silencedetect] silence_end: 21.4 | d",
            "[silencedetect] silence_start: 29.8", "[silencedetect] silence_end: 30.4 | d",
            "[silencedetect] silence_start: 61.0", "[silencedetect] silence_end: 61.2 | d",
        ])
        self.assertEqual(self._cuts(stderr, 100), [30.1, 61.1])

    def test_cut_points_hard_cut_without_silence(self):
        self.assertEqual(self._cuts("", 100), [45.0, 90.0])

    def test_short_audio_has_no_cuts(self):
        self.assertEqual(self._cuts("", 40), [])


if __name__ == "__main__":
    unittest.main()
