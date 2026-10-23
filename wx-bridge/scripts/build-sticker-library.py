#!/usr/bin/env python3
"""把打好标的表情库整理成可直接部署的一份：只留有标签、适合群聊、且没被拉黑的图。

用法：
    python scripts/build-sticker-library.py \\
        --index C:\\path\\sticker-index.json --src C:\\path\\stickers \\
        --out C:\\path\\release --drop 红包 --drop 涉政

输出 out/stickers/ 与 out/sticker-index.json；索引只保留留下来的条目，
Node 与采集端读的是同一份，所以 ID 一定对得上。
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

IMAGE_RE = re.compile(r"\.(jpe?g|png|gif|webp)$", re.I)
MAX_BYTES = 5 * 1024 * 1024


def load_index(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise SystemExit("索引顶层必须是对象")
    return data


def wanted(name, entry, drops):
    if not isinstance(entry, dict):
        return False, "不是对象"
    if entry.get("file") != name:
        return False, "键名与 file 不一致"
    if not IMAGE_RE.search(name):
        return False, "不是支持的图片扩展名"
    if entry.get("blocked") is True:
        return False, "服务商审核拒绝"
    if entry.get("safe") is not True:
        return False, "标记为不适合群聊"
    caption = entry.get("caption")
    if not isinstance(caption, str) or not caption.strip():
        return False, "没有描述"
    haystack = f"{caption} {' '.join(entry.get('keywords') or [])} {entry.get('mood') or ''}"
    for word in drops:
        if word and word.lower() in haystack.lower():
            return False, f"命中剔除词「{word}」"
    return True, ""


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path, text):
    directory = os.path.dirname(path) or "."
    handle, tmp = tempfile.mkstemp(prefix=".sticker-index.", dir=directory)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="整理可部署的表情包库")
    parser.add_argument("--index", required=True)
    parser.add_argument("--src", required=True, help="打标时的图片目录")
    parser.add_argument("--out", required=True, help="输出目录，会创建 stickers/ 和 sticker-index.json")
    parser.add_argument("--drop", action="append", default=[], help="按关键词剔除，可重复")
    parser.add_argument("--max-total-mb", type=int, default=0, help="限制总大小，超出按标签顺序截断")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    index_file = Path(args.index).absolute()
    src = Path(args.src).absolute()
    out = Path(args.out).absolute()
    target_dir = out / "stickers"
    index = load_index(index_file)

    kept, skipped = {}, []
    total = 0
    for name in sorted(index):
        entry = index[name]
        ok, reason = wanted(name, entry, args.drop)
        if not ok:
            skipped.append((name, reason))
            continue
        source = src / name
        if not source.is_file() or source.is_symlink():
            skipped.append((name, "源文件缺失或不是普通文件"))
            continue
        size = source.stat().st_size
        if size <= 0 or size > MAX_BYTES:
            skipped.append((name, f"大小不合适（{size}）"))
            continue
        if args.max_total_mb and total + size > args.max_total_mb * 1024 * 1024:
            skipped.append((name, "超出总大小上限"))
            continue
        kept[name] = {k: entry[k] for k in ("file", "bytes", "sha256", "animated", "caption",
                                            "keywords", "mood", "safe", "updatedAt")
                      if k in entry}
        total += size

    drops = [name for name, reason in skipped if reason.startswith("命中剔除词")]
    print(f"[build] 索引 {len(index)} 条，保留 {len(kept)} 张（约 {total / 1024 / 1024:.1f} MB），"
          f"剔除词命中 {len(drops)} 张，其他跳过 {len(skipped) - len(drops)} 张")
    for name, reason in skipped[:15]:
        print(f"[build]   跳过 {name}: {reason}")
    if args.dry_run:
        return 0
    if not kept:
        raise SystemExit("没有可保留的表情包，先确认 --index / --src 是否正确")

    target_dir.mkdir(parents=True, exist_ok=True)
    payload = {"generatedAt": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
               "source": str(index_file), "count": len(kept)}
    for name, entry in kept.items():
        source = src / name
        if sha256_of(source) != entry.get("sha256"):
            raise SystemExit(f"源文件与索引记录的哈希不一致：{name}（索引可能过期，重跑打标脚本）")
        shutil.copyfile(source, target_dir / name)
    atomic_write(out / "sticker-index.json", json.dumps(kept, ensure_ascii=False, indent=1))
    atomic_write(out / "sticker-index-meta.json", json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"[build] 输出：{target_dir} 与 {out / 'sticker-index.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
