"""Opt-in image references and on-demand, bounded image decoding (UI worker only)."""
import base64
import io
import json
import os
import re
import stat
import tempfile
import time
import uuid
import warnings
from collections import OrderedDict
from pathlib import Path

DEFAULTS = {"enabled": False, "contextWindowMs": 120000, "maxImages": 2, "maxBytes": 5242880}
MAX_REFERENCES = 256
MAX_PIXELS = 16_000_000
MAX_SIDE = 8192


def normalize_vision(config=None):
    if config is None:
        config = {}
    if not isinstance(config, dict) or set(config) - set(DEFAULTS):
        raise ValueError("vision: invalid configuration fields")
    result = {**DEFAULTS, **config}
    if not isinstance(result["enabled"], bool):
        raise ValueError("vision.enabled must be boolean")
    for key, minimum, maximum in (("contextWindowMs", 1000, 600000), ("maxImages", 1, 4), ("maxBytes", 1024, 5242880)):
        value = result[key]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not minimum <= value <= maximum or int(value) != value):
            raise ValueError(f"vision.{key}: invalid integer")
    return result


def vision_from_file(file):
    try:
        data = json.loads(Path(file).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return normalize_vision()
    if not isinstance(data, dict) or ("vision" in data and data["vision"] is None):
        raise ValueError("vision must be an object")
    return normalize_vision(data.get("vision", {}))


def check_path(path, *, directory=False):
    """Reject symlinks/junctions in every component, including the temp root."""
    path = Path(path).absolute()
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("image path contains a link/reparse point")
    info = path.lstat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError("image path has the wrong file type")
    return info


class ImageCache:
    def __init__(self, config=None):
        self.config = normalize_vision(config)
        self.references = OrderedDict()
        self.root = None
        self._root_identity = None
        self._owned_requests = {}

    def _check_root(self):
        info = check_path(self.root, directory=True)
        if (info.st_dev, info.st_ino) != self._root_identity:
            raise ValueError("image cache root changed")

    def prune(self):
        self._cleanup_owned()
        cutoff = time.monotonic() - self.config["contextWindowMs"] / 1000
        for image_id, (_, _, created) in list(self.references.items()):
            if created < cutoff:
                del self.references[image_id]

    def register(self, group, message):
        if not self.config["enabled"]:
            return None
        self.prune()
        image_id = uuid.uuid4().hex
        self.references[image_id] = (group, message, time.monotonic())
        while len(self.references) > MAX_REFERENCES:
            self.references.popitem(last=False)
        return image_id

    def get(self, group, image_id):
        self.prune()
        if not self.config["enabled"]:
            raise ValueError("vision-disabled")
        if not isinstance(image_id, str) or not re.fullmatch(r"[a-f0-9]{32}", image_id):
            raise ValueError("invalid-image-id")
        reference = self.references.get(image_id)
        if not reference or reference[0] != group:
            raise ValueError("image-unavailable")
        message = reference[1]
        if not callable(getattr(message, "download", None)):
            raise ValueError("image-download-unsupported")
        if self.root is None:
            temp = Path(tempfile.gettempdir()).absolute()
            check_path(temp, directory=True)
            self.root = Path(tempfile.mkdtemp(prefix="wx-bridge-images-", dir=temp))
            info = check_path(self.root, directory=True)
            self._root_identity = (info.st_dev, info.st_ino)
        self._check_root()
        if len(self._owned_requests) >= MAX_REFERENCES:
            raise ValueError("image cache cleanup pending; refusing more downloads")
        folder = Path(tempfile.mkdtemp(prefix="request-", dir=self.root))
        identity = check_path(folder, directory=True)
        self._owned_requests[folder] = (identity.st_dev, identity.st_ino)
        try:
            # Only the verified wxauto4 signature and str/Path return contract.
            result = message.download(dir_path=str(folder))
            self._check_root()
            after = check_path(folder, directory=True)
            if (identity.st_dev, identity.st_ino) != (after.st_dev, after.st_ino):
                raise ValueError("image directory changed")
            if not isinstance(result, (str, Path)):
                raise ValueError("image download did not return str/Path")
            file = Path(result)
            if not file.is_absolute():
                raise ValueError("image download returned a relative path")
            # wxauto saves directly in dir_path. No traversal or nested directories.
            if file.parent != folder or file.name in (".", "..") or ":" in file.name:
                raise ValueError("image download escaped cache")
            info = check_path(file)
            if info.st_nlink != 1:
                raise ValueError("image hard links are forbidden")
            if not 0 < info.st_size <= self.config["maxBytes"]:
                raise ValueError("image file too large or empty")
            fd = os.open(file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
            with os.fdopen(fd, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                        or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                        or not 0 < opened.st_size <= self.config["maxBytes"]):
                    raise ValueError("image file changed")
                data = stream.read(self.config["maxBytes"] + 1)
            check_path(file)
            if len(data) > self.config["maxBytes"]:
                raise ValueError("image file grew")
            return self._decode(data)
        finally:
            # Raw downloads never persist until TTL: clean this newly created request
            # directory immediately, never follow links or delete external files.
            self._cleanup_owned()

    def _cleanup_owned(self):
        for folder, identity in list(self._owned_requests.items()):
            try:
                self._check_root()
                after = check_path(folder, directory=True)
                if identity != (after.st_dev, after.st_ino):
                    continue  # Never remove a replacement directory.
                self._clean_request(folder)
                del self._owned_requests[folder]
            except FileNotFoundError:
                self._owned_requests.pop(folder, None)
            except (OSError, ValueError):
                pass  # Retry on subsequent UI polls and shutdown, without logging paths.

    def _clean_request(self, folder):
        # The tree was empty before this download. Delete only entries created
        # inside it, not a returned external path and never a link's target.
        check_path(folder, directory=True)
        for file in folder.iterdir():
            info = file.lstat()
            if stat.S_ISLNK(info.st_mode):
                file.unlink()
            elif getattr(info, "st_file_attributes", 0) & 0x400:
                # A Windows junction is a directory entry, not its target tree.
                file.rmdir() if stat.S_ISDIR(info.st_mode) else file.unlink()
            elif stat.S_ISREG(info.st_mode):
                file.unlink()
            elif stat.S_ISDIR(info.st_mode):
                self._clean_request(file)
        folder.rmdir()

    def _decode(self, data):
        from PIL import Image
        valid = (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")
                 or data[:6] in (b"GIF87a", b"GIF89a")
                 or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))
        if not valid:
            raise ValueError("image magic invalid")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as original:
                if original.format not in ("PNG", "JPEG", "WEBP", "GIF"):
                    raise ValueError("image format invalid")
                width, height = original.size
                if max(width, height) > MAX_SIDE or width * height > MAX_PIXELS:
                    raise ValueError("image pixel limit exceeded")
                original.load()
                alpha = "A" in original.getbands() or "transparency" in original.info
                image = original.convert("RGBA" if alpha else "RGB")
                image.thumbnail((1600, 1600))
                # New image strips metadata, profiles and EXIF; only first frame.
                clean = Image.new(image.mode, image.size)
                clean.paste(image)
                fmt, mime, name = ("PNG", "image/png", "wx.png") if alpha else ("JPEG", "image/jpeg", "wx.jpg")
                while True:
                    output = io.BytesIO()
                    clean.save(output, format=fmt, quality=85)
                    encoded = output.getvalue()
                    if len(encoded) <= self.config["maxBytes"]:
                        return {"mediaType": mime, "data": base64.b64encode(encoded).decode("ascii"), "name": name}
                    if min(clean.size) <= 32:
                        raise ValueError("encoded image too large")
                    clean = clean.resize((max(1, clean.width // 2), max(1, clean.height // 2)))

    def close(self):
        self.references.clear()
        self._cleanup_owned()
        if self.root:
            try:
                self._check_root()
                self.root.rmdir()
            except (OSError, ValueError):
                pass
