#!/usr/bin/env node
// 把 wx-chat preset 作为一条 @deepseek-ai/dsh-agent-preset 行插入 DSH profile 的
// cordis.patch.yml。只改动自己的 marker 区块，保留用户其它 patch 内容。
//
// 用法：
//   node scripts/install-preset.mjs                 # 默认 profile=web，DSH_HOME=~/.dsh
//   node scripts/install-preset.mjs --profile web
//   node scripts/install-preset.mjs --dry-run
//   node scripts/install-preset.mjs --patch-file /tmp/test/cordis.patch.yml
//
// 装完必须重启 dsh web；DSH 只在启动时加载 preset 与 patch。

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import yaml from 'js-yaml';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const PRESET_SOURCE = path.join(ROOT, 'preset', 'wx-chat.preset.yml');
const BEGIN = '# === wx-bridge preset BEGIN ===';
const END = '# === wx-bridge preset END ===';
const PROFILE_NAME_RE = /^[A-Za-z0-9._-]+$/;

function fail(message) {
  throw new Error(message);
}

export function loadPresetRow(file = PRESET_SOURCE) {
  const row = yaml.load(fs.readFileSync(file, 'utf8'), { schema: yaml.JSON_SCHEMA });
  if (!row || typeof row !== 'object' || Array.isArray(row)) fail(`${file} 顶层必须是对象`);
  if (row.id !== 'preset-wx-chat') fail(`${file}: id 必须是 preset-wx-chat`);
  if (row.name !== '@deepseek-ai/dsh-agent-preset') fail(`${file}: name 必须是 @deepseek-ai/dsh-agent-preset`);
  if (row.config?.id !== 'wx-chat') fail(`${file}: config.id 必须是 wx-chat`);
  if (!Array.isArray(row.config?.plugins) || row.config.plugins.length === 0) {
    fail(`${file}: config.plugins 必须是非空数组`);
  }
  const persona = row.config.plugins.find((plugin) => plugin?.id === 'persona');
  if (!persona) fail(`${file}: 缺少 persona 行`);
  return row;
}

export function buildManagedBlock(row) {
  const body = yaml.dump([{ insert: [row] }], { lineWidth: -1, noRefs: true }).trimEnd();
  return [
    BEGIN,
    '# 由 wx-bridge/scripts/install-preset.mjs 维护；此区块会被整体替换，请不要手工编辑区块内部。',
    `# 源：${path.relative(ROOT, PRESET_SOURCE)}`,
    body,
    END,
    ''
  ].join('\n');
}

function stripManagedBlock(text) {
  const start = text.indexOf(BEGIN);
  if (start === -1) return text;
  const end = text.indexOf(END, start);
  if (end === -1) fail(`发现 ${BEGIN} 但没有 ${END}；请手工修复该文件后再运行`);
  return text.slice(0, start) + text.slice(end + END.length);
}

function repairRootEmptyArray(text) {
  const lines = text.replace(/^\uFEFF/, '').split(/\r?\n/);
  const first = lines.findIndex((line) => line.trim() && !line.trimStart().startsWith('#'));
  if (first >= 0 && lines[first].trim() === '[]') {
    const rest = lines.slice(first + 1).filter((line) => line.trim() && !line.trimStart().startsWith('#'));
    // 只处理「根数组为空」的两种常见形态：[] 后面是空/注释，或 [] 后面直接跟块操作。
    if (rest.length === 0 || /^-\s/.test(rest[0])) lines.splice(first, 1);
  }
  return lines.join('\n');
}

function assertNoExistingPreset(text, file) {
  let doc;
  try {
    doc = yaml.load(text, { schema: yaml.JSON_SCHEMA }) ?? [];
  } catch (error) {
    fail(`解析 ${file} 失败（未改动文件）：${error.message}`);
  }
  if (!Array.isArray(doc)) fail(`${file} 必须是 YAML 数组；未改动文件`);
  for (const operation of doc) {
    if (!operation || typeof operation !== 'object' || !Array.isArray(operation.insert)) continue;
    for (const entry of operation.insert) {
      if (entry?.id === 'preset-wx-chat' || entry?.config?.id === 'wx-chat') {
        fail(`${file} 里已经存在 wx-chat preset（id=${entry.id}），但不在 wx-bridge 管理区块内。请先手工移除它，避免重复注册`);
      }
    }
  }
}

function backup(file) {
  if (!fs.existsSync(file)) return null;
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const dest = `${file}.bak-${stamp}`;
  fs.copyFileSync(file, dest);
  // Preserve existing backups; installing a preset must not delete user history.
  return dest;
}

export function installPreset({ dshHome, profile = 'web', patchFile, dryRun = false } = {}) {
  if (!PROFILE_NAME_RE.test(profile)) fail(`非法 profile 名：${profile}`);
  const home = dshHome || process.env.DSH_HOME || path.join(os.homedir(), '.dsh');
  const target = patchFile || path.join(home, 'profiles', profile, 'cordis.patch.yml');
  const row = loadPresetRow();
  const block = buildManagedBlock(row);

  const exists = fs.existsSync(target);
  if (!exists && !patchFile) {
    fail(`找不到 ${target}。请先启动一次 dsh web 生成 profile，再运行本脚本`);
  }
  const original = exists ? fs.readFileSync(target, 'utf8') : '';
  let base = stripManagedBlock(original);
  base = repairRootEmptyArray(base).trimEnd();
  // `[]` / `---\n[]` 这类空文档直接当成空文件处理，避免把块操作追加到 `[]` 后面生成非法 YAML。
  if (base) {
    try {
      const parsedBase = yaml.load(base, { schema: yaml.JSON_SCHEMA });
      if (Array.isArray(parsedBase) && parsedBase.length === 0) base = '';
    } catch {
      // 留给下面的 assertNoExistingPreset 给出明确报错。
    }
  }
  if (base) {
    // 只对用户原有内容做结构校验；外层数组允许为空。
    assertNoExistingPreset(base.endsWith('\n') ? base : `${base}\n`, target);
  }
  const next = `${base ? `${base}\n\n` : ''}${block}`;

  let parsed;
  try {
    parsed = yaml.load(next, { schema: yaml.JSON_SCHEMA });
  } catch (error) {
    fail(`生成的 ${target} 不是合法 YAML（未写入）：${error.message}`);
  }
  if (!Array.isArray(parsed)) fail(`生成的 ${target} 顶层不是数组（未写入）`);
  const installed = parsed.some((operation) => Array.isArray(operation?.insert)
    && operation.insert.some((entry) => entry?.id === 'preset-wx-chat'));
  if (!installed) fail('内部错误：生成的 patch 中没有找到 preset-wx-chat');

  if (dryRun) return { target, changed: next !== original, next };

  if (exists && next !== original) backup(target);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  // 仅更新 preset；旧版 guard 文件保留原样，不再安装或挂载。
  const tmp = `${target}.${process.pid}.tmp`;
  try {
    fs.writeFileSync(tmp, next, 'utf8');
    fs.renameSync(tmp, target);
  } finally {
    fs.rmSync(tmp, { force: true });
  }
  return { target, changed: next !== original, next };
}

function parseArgs(argv) {
  const args = { profile: 'web', dryRun: false, print: false, dshHome: '', patchFile: '' };
  for (let i = 0; i < argv.length; i += 1) {
    const item = argv[i];
    if (item === '--profile') args.profile = argv[++i];
    else if (item === '--dry-run') args.dryRun = true;
    else if (item === '--print') args.print = true;
    else if (item === '--dsh-home') args.dshHome = argv[++i];
    else if (item === '--patch-file') args.patchFile = argv[++i];
    else if (item === '--help' || item === '-h') {
      console.log('node scripts/install-preset.mjs [--profile web] [--dsh-home DIR] [--patch-file FILE] [--dry-run] [--print]');
      process.exit(0);
    } else {
      fail(`未知参数：${item}`);
    }
  }
  return args;
}

if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url))) {
  try {
    const args = parseArgs(process.argv.slice(2));
    const result = installPreset(args);
    if (args.print || args.dryRun) {
      console.log(result.next);
      if (args.dryRun) console.error(`[install-preset] dry-run：未写入 ${result.target}`);
    }
    if (!args.dryRun) {
      console.log(`[install-preset] ${result.changed ? '已更新' : '已是最新'}：${result.target}`);
      console.log('[install-preset] wx-chat 不再挂载工具守卫，已启用 web search/fetch；旧 guard 文件保持不变，其他工具仍以实际注册为准。');
      console.log('[install-preset] 现在重启 dsh web，新的 wx-chat preset 才会生效。');
    }
  } catch (error) {
    console.error(`[install-preset] 失败：${error?.message ?? error}`);
    process.exitCode = 1;
  }
}
