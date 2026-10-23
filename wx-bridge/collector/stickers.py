"""Local, opt-in sticker allowlist. HTTP callers supply IDs, never paths."""

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PureWindowsPath

MAX_STICKER_BYTES = 5 * 1024 * 1024
EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif"}
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
IMAGE_RE = re.compile(r"\.(jpe?g|png|gif)\Z", re.IGNORECASE)
AUTO_LIMIT = 200
MAX_INDEX_ITEMS = 4000
MAX_KEYWORDS = 10
MAX_KEYWORD_CHARS = 50
MAX_DESCRIPTION_CHARS = 500


class StickerError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


def _object(value, keys, label):
    if not isinstance(value, dict) or set(value) - set(keys):
        raise ValueError(f"{label}: expected an object with only {', '.join(keys)}")


def _relative_path(value, label):
    if (not isinstance(value, str) or not value
            or re.search(r"[\x00-\x1f:]", value)
            or value.startswith(("/", "\\")) or PureWindowsPath(value).drive):
        raise ValueError(f"{label}: expected a safe relative path")
    parts = value.replace("\\", "/").split("/")
    if any(not part or part in (".", "..") or part.endswith((".", " ")) for part in parts):
        raise ValueError(f"{label}: empty, . and .. path segments are forbidden")
    return Path(*parts)


def _auto_keywords(stem):
    """Split a stem exactly like src/stickers.js (dedupe, 10 x 50 chars)."""
    parts = [part[:MAX_KEYWORD_CHARS] for part in re.split(r"[_\-\s.]+", stem) if part]
    unique = list(dict.fromkeys(parts))[:MAX_KEYWORDS]
    return unique or [stem[:MAX_KEYWORD_CHARS]]


def auto_sticker_items(directory):
    """Scan one directory level and build entries identical to Node's autoLoad."""
    directory = Path(directory)
    try:
        entries = list(os.scandir(directory))
    except (FileNotFoundError, NotADirectoryError):
        # The user may enable stickers before creating the folder / adding images.
        return []
    names = []
    for entry in entries:
        if not IMAGE_RE.search(entry.name):
            continue
        try:
            # follow_symlinks=False keeps links (including broken ones) out.
            if not entry.is_file(follow_symlinks=False):
                continue
        except OSError:
            continue
        names.append(entry.name)
    names.sort()
    items = []
    for name in names[:AUTO_LIMIT]:
        relative = Path(name)
        stem = relative.stem
        # Node hashes the forward-slash relative name; one level means basename.
        digest = hashlib.sha1(relative.as_posix().encode("utf-8")).hexdigest()
        items.append({
            "id": f"auto-{digest[:12]}",
            "file": relative,
            "keywords": _auto_keywords(stem),
            "description": stem[:MAX_DESCRIPTION_CHARS],
        })
    return items


def _stem(name):
    """Strip only the last extension, matching Node's path.basename(name, ext)."""
    return os.path.splitext(name)[0]


def _base_id(name):
    stem = _stem(name)
    if ID_RE.fullmatch(stem):
        return stem
    return f"img-{hashlib.sha1(name.encode('utf-8')).hexdigest()[:12]}"


def index_sticker_items(index_file):
    """Read the captioner's sticker-index.json exactly like src/stickers.js does.

    IDs depend only on the index contents, never on which files exist, so the two
    processes always agree on what an ID means.
    """
    try:
        raw = Path(index_file).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("stickers.indexFile: top level must be an object")
    accepted = []
    for key in sorted(value):
        entry = value[key]
        if not isinstance(entry, dict) or entry.get("file") != key:
            continue
        if not IMAGE_RE.search(key) or entry.get("safe") is not True or entry.get("blocked") is True:
            continue
        caption = entry.get("caption")
        if not isinstance(caption, str) or not caption.strip():
            continue
        try:
            relative = _relative_path(entry.get("file"), f"sticker index {key}")
        except ValueError:
            continue
        accepted.append((key, relative))
        if len(accepted) >= MAX_INDEX_ITEMS:
            break
    counts = {}
    for key, _relative in accepted:
        base = _base_id(key)
        counts[base] = counts.get(base, 0) + 1
    items = []
    used = set()
    for key, relative in accepted:
        base = _base_id(key)
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        sticker_id = f"{base}-{digest[:6]}" if counts[base] > 1 else base
        if sticker_id in used:
            continue
        used.add(sticker_id)
        items.append({"id": sticker_id, "file": relative})
    return items


class StickerCatalog:
    def __init__(self, config=None, *, base_dir=None):
        if config is None:
            config = {}
        _object(config, ("enabled", "directory", "cooldownMs", "autoLoad", "items",
                         "indexFile", "shortlistLimit"), "stickers")
        self.enabled = config.get("enabled", False)
        if not isinstance(self.enabled, bool):
            raise ValueError("stickers.enabled must be boolean")
        # Only the prompt side picks shortlists; validate anyway so both sides
        # reject the same malformed config file.
        shortlist = config.get("shortlistLimit", 20)
        if (isinstance(shortlist, bool) or not isinstance(shortlist, (int, float))
                or int(shortlist) != shortlist or not 1 <= shortlist <= 80):
            raise ValueError("stickers.shortlistLimit must be an integer between 1 and 80")
        cooldown = config.get("cooldownMs", 0)
        if (isinstance(cooldown, bool) or not isinstance(cooldown, (int, float))
                or not 0 <= cooldown <= 9007199254740991 or int(cooldown) != cooldown):
            raise ValueError("stickers.cooldownMs must be a nonnegative safe integer")
        self.auto_load = config.get("autoLoad", True)
        if not isinstance(self.auto_load, bool):
            raise ValueError("stickers.autoLoad must be boolean")
        directory = _relative_path(config.get("directory", "stickers"), "stickers.directory")
        self.directory = Path(base_dir or Path.cwd()).absolute() / directory
        index_file = config.get("indexFile")
        self.index_file = None
        if index_file is not None and index_file != "":
            self.index_file = (Path(base_dir or Path.cwd()).absolute()
                               / _relative_path(index_file, "stickers.indexFile"))
        self._root = None
        self._files = {}
        items = config.get("items", [])
        if not isinstance(items, list):
            raise ValueError("stickers.items must be an array")
        for item in items:
            _object(item, ("id", "file", "keywords", "description"), "stickers.items[]")
            if "description" in item and (not isinstance(item["description"], str)
                    or len(item["description"].encode("utf-16-le")) // 2 > 500):
                raise ValueError("stickers.items[].description must be a string of at most 500 UTF-16 units")
            sticker_id = item.get("id")
            if not isinstance(sticker_id, str) or not ID_RE.fullmatch(sticker_id) or sticker_id in self._files:
                raise ValueError("stickers.items[].id must be unique and match [A-Za-z0-9_-]{1,64}")
            file = _relative_path(item.get("file"), "stickers.items[].file")
            if file.suffix.lower() not in EXTENSIONS:
                raise ValueError("stickers.items[].file only supports jpg/jpeg/png/gif")
            keywords = item.get("keywords")
            if (not isinstance(keywords, list) or not keywords
                    or any(not isinstance(word, str) or not word.strip() for word in keywords)):
                raise ValueError("stickers.items[].keywords must be nonempty strings")
            self._files[sticker_id] = file
        # Explicit items always win; a label index replaces the auto-scan.
        explicit_ids = set(self._files)
        if self.enabled and self.index_file is not None:
            explicit_files = {relative.as_posix() for relative in self._files.values()}
            for item in index_sticker_items(self.index_file):
                relative = item["file"]
                if item["id"] in explicit_ids or relative.as_posix() in explicit_files:
                    continue
                # Same rule as Node: an entry whose image is gone is never offered.
                if not (self.directory / relative).is_file():
                    continue
                self._files[item["id"]] = relative
            if not self._files and self.auto_load:
                for item in auto_sticker_items(self.directory):
                    self._files[item["id"]] = item["file"]
        elif self.enabled and not self._files and self.auto_load:
            for item in auto_sticker_items(self.directory):
                self._files[item["id"]] = item["file"]
        if self.enabled and self._files:
            self._root = self.directory.resolve(strict=True)
            if not self._root.is_dir():
                raise ValueError("stickers.directory must be a directory")
            # Hand-picked items are checked at startup; a large indexed library is
            # validated per ID when a sticker is actually sent.
            for sticker_id in explicit_ids:
                self.validate(sticker_id)

    @classmethod
    def from_config_file(cls, file):
        file = Path(file).absolute()
        try:
            data = json.loads(file.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return cls(base_dir=file.parent)
        if not isinstance(data, dict):
            raise ValueError("config must be an object")
        config = data.get("stickers", {})
        if config is None:
            raise ValueError("stickers must be an object")
        return cls(config, base_dir=file.parent)

    def validate(self, sticker_id):
        if not self.enabled:
            raise StickerError("stickers-disabled", "本地表情包未启用", 403)
        if not isinstance(sticker_id, str) or not ID_RE.fullmatch(sticker_id):
            raise StickerError("invalid-sticker-id", "stickerId 格式非法")
        if sticker_id not in self._files:
            raise StickerError("unknown-sticker", "stickerId 不在配置白名单中", 404)
        try:
            # Pin the root at startup; detect later directory/link replacement.
            if self.directory.resolve(strict=True) != self._root:
                raise ValueError("sticker directory changed")
            relative = self._files[sticker_id]
            file = (self.directory / relative).resolve(strict=True)
            if not file.is_relative_to(self._root):
                raise ValueError("sticker resolves outside its directory")
            if file.suffix.lower() not in EXTENSIONS:
                raise ValueError("unsupported sticker extension")
            if not stat.S_ISREG(file.stat().st_mode):
                raise ValueError("sticker is not a regular file")
            with file.open("rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_STICKER_BYTES:
                    raise ValueError("sticker must be a regular file of at most 5MB")
                magic = stream.read(8)
            suffixes = {relative.suffix.lower(), file.suffix.lower()}
            valid = ((suffixes <= {".jpg", ".jpeg"} and magic.startswith(b"\xff\xd8\xff"))
                     or (suffixes == {".png"} and magic == b"\x89PNG\r\n\x1a\n")
                     or (suffixes == {".gif"} and magic[:6] in (b"GIF87a", b"GIF89a")))
            if not valid:
                raise ValueError("sticker magic does not match its extension")
            return file
        except (OSError, RuntimeError, ValueError) as error:
            raise StickerError("invalid-sticker-file", f"表情包文件校验失败：{sticker_id}: {error}") from error
