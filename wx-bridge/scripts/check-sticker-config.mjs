// One-shot check of the sticker library as the bridge will see it:
//   node scripts/check-sticker-config.mjs [config.json]
// Prints the library size, a sample shortlist and the prompt cost. It reads the
// real config, so run it on the server right after deploying the index.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadConfig } from '../src/config.js';
import { shortlistStickers, stickerProtocol } from '../src/stickers.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const configFile = process.argv[2] || path.resolve(here, '..', 'config.json');
const cfg = loadConfig(configFile);
const stickers = cfg.stickers;

if (!stickers.enabled) {
  console.log('STICKERS_OFF: stickers.enabled=false');
  process.exit(0);
}
console.log(`directory=${stickers.directory}`);
console.log(`indexFile=${stickers.indexFile ?? '（未配置，走 autoLoad）'}`);
console.log(`items=${stickers.items.length} shortlistLimit=${stickers.shortlistLimit}`);
if (!stickers.items.length) {
  console.log('EMPTY_LIBRARY: 索引里没有可用条目（检查 safe/blocked 标记与图片是否真的存在）');
  process.exit(1);
}
const sample = shortlistStickers(stickers, { text: '今天上班好累，想摸鱼', context: '' });
console.log(`shortlist=${sample.length} ids=${sample.map((item) => item.id).join(',')}`);
console.log(`protocolChars=${stickerProtocol(stickers, sample).length}`);
const missing = stickers.items.filter((item) => !fs.existsSync(path.join(stickers.directory, item.file)));
console.log(`missingFiles=${missing.length}`);
const duplicateIds = stickers.items.length - new Set(stickers.items.map((item) => item.id)).size;
console.log(`duplicateIds=${duplicateIds}`);
if (missing.length || duplicateIds) {
  console.log('STICKERS_SUSPECT: 有缺失文件或重复 ID，先发文字也不发图');
  process.exit(1);
}
console.log('STICKERS_OK');
