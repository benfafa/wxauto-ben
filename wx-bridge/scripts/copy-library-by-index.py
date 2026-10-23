#!/usr/bin/env python3
"""按 sticker-index.json 把表情库落到目标目录，逐个校验 sha256。

用途：服务器上已经有一份原始图片（比如之前上传的 scratch 目录），
只需上传索引就能拼出要上线的曲库，不用重传 300MB。

    python scripts/copy-library-by-index.py --index sticker-index.json --src C:\\wxb\\sticker-lab --dst C:\\wxb\\stickers

多余文件只列出来，加了 --remove-extra 才真的删。
"""

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path, PureWindowsPath

BAD_CHARS = re.compile(r"[\x00-\x1f:]")


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_name(value):
    if (not isinstance(value, str) or not value or BAD_CHARS.search(value)
            or value.startswith(("/", "\\")) or PureWindowsPath(value).drive):
        return None
    parts = value.replace("\\", "/").split("/")
    if any(not part or part in (".", "..") or part.endswith((".", " ")) for part in parts):
        return None
    if len(parts) != 1:
        return None
    return parts[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description="按索引拼装可上线的表情库")
    parser.add_argument("--index", required=True)
    parser.add_argument("--src", required=True, help="原始图片所在目录（只扫一层）")
    parser.add_argument("--dst", required=True, help="上线目录，脚本只往这里写")
    parser.add_argument("--remove-extra", action="store_true", help="删除 dst 里索引没提到的文件")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    index = json.loads(Path(args.index).read_text(encoding="utf-8-sig"))
    if not isinstance(index, dict):
        raise SystemExit("索引顶层必须是对象")
    src, dst = Path(args.src).absolute(), Path(args.dst).absolute()
    copied = identical = missing = mismatched = 0
    wanted = set()
    for name in sorted(index):
        entry = index[name]
        if not isinstance(entry, dict) or entry.get("file") != name:
            missing += 1
            continue
        clean = safe_name(name)
        if clean is None:
            print(f"[copy] 拒绝非法文件名：{name!r}")
            missing += 1
            continue
        wanted.add(clean)
        source = src / clean
        if not source.is_file() or source.is_symlink():
            print(f"[copy] 源文件缺失：{clean}")
            missing += 1
            continue
        digest = entry.get("sha256")
        actual = sha256_of(source)
        if isinstance(digest, str) and digest and actual != digest:
            print(f"[copy] 哈希不一致（索引过期？）：{clean}")
            mismatched += 1
            continue
        target = dst / clean
        if target.is_file() and sha256_of(target) == actual:
            identical += 1
            continue
        copied += 1
        if not args.dry_run:
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

    extra = []
    if dst.is_dir():
        extra = [p.name for p in dst.iterdir() if p.is_file() and p.name not in wanted]
    print(f"[copy] 索引 {len(index)} 条：新拷 {copied}，已存在且一致 {identical}，"
          f"缺失 {missing}，哈希不符 {mismatched}，目标目录多余 {len(extra)}")
    if not args.dry_run and extra and args.remove_extra:
        for name in extra:
            (dst / name).unlink()
        print(f"[copy] 已按 --remove-extra 删除 {len(extra)} 个多余文件")
    elif extra:
        print("[copy] 多余文件（未删除）：" + ", ".join(sorted(extra)[:20])
              + ("…" if len(extra) > 20 else ""))
    if missing or mismatched:
        return 1
    print("COPY_OK" if not args.dry_run else "COPY_DRY_RUN_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
