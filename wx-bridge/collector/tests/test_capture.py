import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image

from collector.capture import CapturedImage, capture_message


class FakeRect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class FakeControl:
    def __init__(self, rect):
        self.BoundingRectangle = rect


class FakeMessage:
    def __init__(self, rect, rolled=True):
        self.control = FakeControl(rect)
        self.rolled = rolled
        self.rolls = 0

    def roll_into_view(self):
        self.rolls += 1
        if not self.rolled:
            raise RuntimeError("can't scroll")


class CapturedImageTest(unittest.TestCase):
    def test_download_writes_bytes(self):
        shot = CapturedImage(b"\x89PNG\r\n\x1a\npayload", "capture.png")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(shot.download(dir_path=folder))
            self.assertEqual(path.read_bytes(), shot.data)
            self.assertEqual(path.parent, Path(folder))

    def test_capture_returns_png_and_scrolls_first(self):
        message = FakeMessage(FakeRect(10, 20, 170, 180))
        with patch("PIL.ImageGrab.grab", return_value=Image.new("RGB", (160, 160), "white")):
            shot = capture_message(message)
        self.assertIsNotNone(shot)
        self.assertEqual(message.rolls, 1)
        self.assertTrue(shot.data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(shot.name, "capture.png")

    def test_capture_survives_unscrollable_message(self):
        message = FakeMessage(FakeRect(10, 20, 170, 180), rolled=False)
        with patch("PIL.ImageGrab.grab", return_value=Image.new("RGB", (160, 160), "white")):
            self.assertIsNotNone(capture_message(message))

    def test_rejects_tiny_or_absurd_rects(self):
        for rect in (FakeRect(0, 0, 5, 5), FakeRect(0, 0, 5000, 100)):
            message = FakeMessage(rect)
            with patch("PIL.ImageGrab.grab") as grab:
                self.assertIsNone(capture_message(message))
                grab.assert_not_called()

    def test_grab_failure_is_none(self):
        message = FakeMessage(FakeRect(0, 0, 120, 120))
        with patch("PIL.ImageGrab.grab", side_effect=OSError("no desktop")):
            self.assertIsNone(capture_message(message))

    def test_refuses_capture_that_cannot_fit_the_budget(self):
        # 噪声图压不下去，给个极小的预算就应当直接放弃，而不是塞一张超限的图进去。
        message = FakeMessage(FakeRect(0, 0, 900, 900))
        noisy = Image.effect_noise((800, 800), 120).convert("RGB")
        with patch("PIL.ImageGrab.grab", return_value=noisy):
            self.assertIsNone(capture_message(message, max_bytes=2000))
