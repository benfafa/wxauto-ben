#!/usr/bin/env python3
"""Print what the collector's sticker catalog actually resolves for a config file.

Run on the server right after deploying the index; it uses the same code path as
/send-sticker, so "IDs the model can pick" and "IDs the sender can resolve" are
proven equal here instead of assumed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector.stickers import StickerCatalog, StickerError  # noqa: E402


def main(argv):
    config = argv[1] if len(argv) > 1 else "config.json"
    catalog = StickerCatalog.from_config_file(config)
    if not catalog.enabled:
        print("COLLECTOR_STICKERS_OFF")
        return 0
    print(f"directory={catalog.directory}")
    print(f"indexFile={catalog.index_file}")
    print(f"ids={len(catalog._files)}")
    broken = []
    checked = 0
    for sticker_id in sorted(catalog._files):
        checked += 1
        try:
            catalog.validate(sticker_id)
        except StickerError as error:
            broken.append(f"{sticker_id}: {error.code}")
    print(f"spotChecked={checked} broken={len(broken)}")
    for line in broken:
        print(f"  {line}")
    print("COLLECTOR_STICKERS_OK" if not broken else "COLLECTOR_STICKERS_SUSPECT")
    return 0 if not broken else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
