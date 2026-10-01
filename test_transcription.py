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


if __name__ == "__main__":
    unittest.main()
