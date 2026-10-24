"""把消息气泡截成图，喂给视觉链路。

微信 4.x 的表情（emotion）不落普通文件，本地缓存是加密 .dat，免费 API 也没有
下载方法——但屏幕上是渲染好的像素。所以对这条消息的气泡取 UIA 边界框、截图，
再包成和 ImageMessage 一样「有 download()」的对象，视觉和收件箱都不用改。

限制：窗口必须可见、消息要先滚进视野；表情本身小，截出来可能糊。
"""

import io
from pathlib import Path

MAX_SIDE = 800
MIN_SIDE = 24
MAX_SIDE_CAPTURE = 1600


class CapturedImage:
    """和 wxauto4 的图片消息同形：只暴露 download(dir_path)。"""

    def __init__(self, data, name="capture.png"):
        self.data = data
        self.name = name

    def download(self, dir_path=None):
        folder = Path(dir_path)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / self.name
        path.write_bytes(self.data)
        return str(path)


def capture_message(message, *, max_bytes=5 * 1024 * 1024):
    """返回 CapturedImage；任何一步失败就返回 None，调用方退回纯文字。"""
    try:
        from PIL import ImageGrab
    except ImportError:  # pragma: no cover - PIL 是硬依赖，这里只是兜底
        return None
    try:
        message.roll_into_view()
    except Exception:
        # 滚不动也继续试：消息可能本来就在视野里。
        pass
    try:
        rect = message.control.BoundingRectangle
        left, top, right, bottom = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    except Exception:
        return None
    width, height = right - left, bottom - top
    if width < MIN_SIDE or height < MIN_SIDE or width > MAX_SIDE_CAPTURE or height > MAX_SIDE_CAPTURE:
        return None
    try:
        image = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
    except Exception:
        return None
    image = image.convert("RGB")
    if max(image.size) > MAX_SIDE:
        image.thumbnail((MAX_SIDE, MAX_SIDE))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    data = buffer.getvalue()
    if len(data) > max_bytes:
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=85)
        data = buffer.getvalue()
    if not data or len(data) > max_bytes:
        return None
    name = "capture.png" if data[:4] == b"\x89PNG" else "capture.jpg"
    return CapturedImage(data, name)
