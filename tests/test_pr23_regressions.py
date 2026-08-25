import os
import tempfile
import threading
import unittest
from unittest.mock import patch

import m3u8dl
import utils


class FakeResponse:
    status_code = 200
    headers = {"Content-Length": "3"}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def iter_content(self, chunk_size):
        yield b"abc"


class SessionTitleTests(unittest.TestCase):
    def test_duplicate_titles_include_start_time(self):
        sessions = [
            {
                "id": 1,
                "title": "第10周 星期四 第2大节 (2026.05.07)",
                "started_at": "2026-05-07 09:55:00",
            },
            {
                "id": 2,
                "title": "第10周 星期四 第2大节 (2026.05.07)",
                "started_at": "2026-05-07T11:35:00+08:00",
            },
            {"id": 3, "title": "第10周 星期五 第1大节"},
        ]

        utils.disambiguate_session_titles(sessions)

        self.assertEqual(
            sessions[0]["title"],
            "第10周 星期四 第2大节 (2026.05.07) (09-55)",
        )
        self.assertEqual(
            sessions[1]["title"],
            "第10周 星期四 第2大节 (2026.05.07) (11-35)",
        )
        self.assertEqual(sessions[2]["title"], "第10周 星期五 第1大节")

    def test_missing_or_duplicate_times_fall_back_to_session_id(self):
        sessions = [
            {"id": 10, "title": "重复课程"},
            {"id": 11, "title": "重复课程"},
        ]

        utils.disambiguate_session_titles(sessions)

        self.assertEqual(sessions[0]["title"], "重复课程 (session-10)")
        self.assertEqual(sessions[1]["title"], "重复课程 (session-11)")


class DownloaderRegressionTests(unittest.TestCase):
    def test_part_sort_key_handles_numeric_and_text_prefixes(self):
        filenames = ["abc.ts.part", "10.ts.part", "2.ts.part"]
        self.assertEqual(
            sorted(filenames, key=m3u8dl.M3u8Download._part_sort_key),
            ["2.ts.part", "10.ts.part", "abc.ts.part"],
        )

    @patch("m3u8dl.requests.get", return_value=FakeResponse())
    def test_callback_failure_does_not_delete_completed_segment(self, _get):
        downloader = object.__new__(m3u8dl.M3u8Download)
        downloader._token = "token"
        downloader._headers = {}
        downloader._success_lock = threading.Lock()
        downloader._success_sum = 0
        downloader._ts_sum = 1
        downloader._last_progress_time = 0
        downloader._current_signature = lambda: ("timestamp", "signature")
        downloader._print_progress = lambda: None

        def fail_callback(*_args):
            raise RuntimeError("callback failed")

        downloader._progress_callback = fail_callback

        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "0.ts")
            with self.assertRaisesRegex(RuntimeError, "callback failed"):
                downloader.download_ts("https://example.test/0.ts", target, 2)

            self.assertEqual(downloader._success_sum, 1)
            self.assertTrue(os.path.exists(target))
            self.assertFalse(os.path.exists(target + ".part"))
            with open(target, "rb") as segment:
                self.assertEqual(segment.read(), b"abc")

    @patch("m3u8dl.requests.get", side_effect=OSError("network down"))
    def test_m3u8_retry_exhaustion_raises_root_cause(self, get):
        downloader = object.__new__(m3u8dl.M3u8Download)
        downloader._token = "token"
        downloader._headers = {}
        downloader._current_signature = lambda: ("timestamp", "signature")

        with self.assertRaisesRegex(RuntimeError, "Failed to get m3u8 info"):
            downloader.get_m3u8_info("https://example.test/index.m3u8", 1)

        self.assertEqual(get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
