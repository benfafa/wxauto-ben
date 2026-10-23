// 一次性探针：确认当前 DSH 配置的模型能否接收图片。
// 用法：node scripts/check-vision.mjs <图片路径> [config.json]
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadConfig } from '../src/config.js';
import { DshAgent } from '../src/dsh-agent.js';
import { SessionStore } from '../src/state.js';
import { Logger } from '../src/logger.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const imageFile = process.argv[2];
const configFile = process.argv[3] || path.resolve(__dirname, '..', 'config.json');
if (!imageFile) {
  console.error('用法：node scripts/check-vision.mjs <图片路径> [config.json]');
  process.exit(2);
}

const cfg = loadConfig(configFile);
const probeCfg = { ...cfg, vision: { ...cfg.vision, enabled: true } };
const log = new Logger({ stateDir: cfg.stateDir, name: 'check-vision' });
const store = new SessionStore(path.join(cfg.stateDir, 'check-vision-sessions.json'));
const agent = new DshAgent(probeCfg, { store, logger: log });

const ext = path.extname(imageFile).toLowerCase();
const mediaType = { '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp' }[ext];
if (!mediaType) {
  console.error(`PROBE_FAIL: 不支持的图片扩展名 ${ext}`);
  process.exit(2);
}
const data = fs.readFileSync(imageFile).toString('base64');

await agent.start();
try {
  const result = await agent.ask('vision-probe', '这张图片里有什么？只回答你实际看到的文字或物体，看不到就说明看不到。', {
    images: [{ mediaType, data, name: `probe${ext}` }]
  });
  console.log(`PROBE_OK model=${cfg.dsh.provider}/${cfg.dsh.model}: ${String(result.text).slice(0, 300)}`);
} catch (error) {
  console.log(`PROBE_FAIL model=${cfg.dsh.provider}/${cfg.dsh.model}: ${String(error?.message ?? error).slice(0, 500)}`);
} finally {
  await agent.stop().catch(() => {});
}
