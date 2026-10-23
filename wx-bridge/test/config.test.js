import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { loadConfig } from '../src/config.js';

const TOKEN = '0123456789abcdef0123456789abcdef';

function writeConfig(overrides = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wx-bridge-config-'));
  const data = {
    wechat: {
      token: TOKEN,
      groups: ['群A'],
      mentionAliases: ['小助手'],
      ...(overrides.wechat ?? {})
    },
    dsh: { ...(overrides.dsh ?? {}) },
    behavior: { ...(overrides.behavior ?? {}) },
    ...(Object.hasOwn(overrides, 'stickers') ? { stickers: overrides.stickers } : {}),
    ...(Object.hasOwn(overrides, 'vision') ? { vision: overrides.vision } : {}),
    stateDir: overrides.stateDir ?? 'state'
  };
  const file = path.join(dir, 'config.json');
  fs.writeFileSync(file, JSON.stringify(data));
  return { dir, file };
}

test('loads defaults, normalizes aliases and resolves paths', () => {
  const { dir, file } = writeConfig();
  const cfg = loadConfig(file);
  assert.deepEqual(cfg.wechat.mentionAliases, ['@小助手']);
  assert.equal(cfg.stateDir, path.join(dir, 'state'));
  assert.equal(cfg.dsh.sessionCwd, path.join(dir, 'state', 'agents'));
  assert.equal(cfg.dsh.agentPreset, 'wx-chat');
  assert.equal(cfg.wechat.pollWaitMs, 15000);
  assert.equal(cfg.behavior.requireMention, true);
  fs.rmSync(dir, { recursive: true, force: true });
});

test('rejects placeholder or short tokens', () => {
  for (const token of ['REPLACE_WITH_AT_LEAST_16_RANDOM_CHARS', 'change-me', 'short']) {
    const { dir, file } = writeConfig({ wechat: { token } });
    assert.throws(() => loadConfig(file), /wechat\.token/);
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('rejects empty groups and aliases (fail closed)', () => {
  const a = writeConfig({ wechat: { groups: [] } });
  assert.throws(() => loadConfig(a.file), /wechat\.groups/);
  fs.rmSync(a.dir, { recursive: true, force: true });

  const b = writeConfig({ wechat: { mentionAliases: [] } });
  assert.throws(() => loadConfig(b.file), /mentionAliases/);
  fs.rmSync(b.dir, { recursive: true, force: true });
});

test('rejects broadcast aliases', () => {
  const { dir, file } = writeConfig({ wechat: { mentionAliases: ['@所有人'] } });
  assert.throws(() => loadConfig(file), /所有人/);
  fs.rmSync(dir, { recursive: true, force: true });
});

test('requires a preset unless allowDefaultPreset is explicit', () => {
  const a = writeConfig({ dsh: { agentPreset: '' } });
  assert.throws(() => loadConfig(a.file), /agentPreset/);
  fs.rmSync(a.dir, { recursive: true, force: true });

  const b = writeConfig({ dsh: { agentPreset: '', allowDefaultPreset: true } });
  assert.equal(loadConfig(b.file).dsh.agentPreset, '');
  fs.rmSync(b.dir, { recursive: true, force: true });
});

test('rejects a request timeout shorter than the long poll', () => {
  const { dir, file } = writeConfig({ wechat: { pollWaitMs: 15000, requestTimeoutMs: 5000 } });
  assert.throws(() => loadConfig(file), /requestTimeoutMs/);
  fs.rmSync(dir, { recursive: true, force: true });
});

test('rejects non-http DSH URLs', () => {
  const { dir, file } = writeConfig({ dsh: { baseUrl: 'ws://127.0.0.1:3080' } });
  assert.throws(() => loadConfig(file), /http\/https/);
  fs.rmSync(dir, { recursive: true, force: true });
});

test('stickers are independently disabled by default and paths use the config directory', () => {
  for (const input of [{}, { stickers: { enabled: true, items: [{ id: 'happy', file: 'mood/happy.PNG', keywords: [' 开心 '] }] } }]) {
    const { dir, file } = writeConfig(input);
    try {
      const cfg = loadConfig(file);
      assert.equal(cfg.stickers.directory, path.join(dir, 'stickers'));
      assert.equal(cfg.stickers.cooldownMs, 0);
      assert.equal(cfg.stickers.autoLoad, true);
      assert.equal(cfg.stickers.enabled, Boolean(input.stickers));
      assert.equal(cfg.dsh.agentPreset, 'wx-chat');
      assert.equal(cfg.dsh.allowDefaultPreset, false);
      assert.deepEqual(cfg.stickers.items, input.stickers
        ? [{ id: 'happy', file: 'mood/happy.PNG', keywords: ['开心'] }] : []);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  }
});

test('autoLoad scans the config-relative sticker directory when items are empty', () => {
  const { dir, file } = writeConfig({ stickers: { enabled: true } });
  try {
    fs.mkdirSync(path.join(dir, 'stickers'));
    fs.writeFileSync(path.join(dir, 'stickers', '开心_庆祝.png'), 'fixture');
    const cfg = loadConfig(file);
    assert.deepEqual(cfg.stickers.items, [{ id: 'auto-08b754933842', file: '开心_庆祝.png',
      keywords: ['开心', '庆祝'], description: '开心_庆祝' }]);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('loads explicit vision and sticker description without changing safety defaults', () => {
  const { dir, file } = writeConfig({ vision: { enabled: true }, stickers: { cooldownMs: 60000,
    items: [{ id: 'happy', file: 'happy.png', keywords: ['开心'], description: '庆祝' }] } });
  try {
    const cfg = loadConfig(file);
    assert.deepEqual(cfg.vision, { enabled: true, contextWindowMs: 120000, maxImages: 2, maxBytes: 5242880 });
    assert.equal(cfg.stickers.items[0].description, '庆祝');
    assert.equal(cfg.stickers.cooldownMs, 60000);
    assert.equal(cfg.behavior.requireMention, true);
    assert.equal(cfg.dsh.agentPreset, 'wx-chat');
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
  for (const vision of [null, { enabled: 'true' }, { maxImages: '2' }, { unknown: 1 }]) {
    const item = writeConfig({ vision });
    try { assert.throws(() => loadConfig(item.file), /vision/); }
    finally { fs.rmSync(item.dir, { recursive: true, force: true }); }
  }
});

test('rejects malformed sticker configuration even when disabled', () => {
  const item = { id: 'happy', file: 'happy.png', keywords: ['开心'] };
  const invalid = [null, [], true, { enabled: 'false' }, { autoLoad: 'true' }, { autoLoad: 1 },
    { cooldownMs: -1 }, { cooldownMs: 0.5 },
    { cooldownMs: '1000' }, { directory: '' }, { directory: '../stickers' },
    { directory: 'C:\\stickers' }, { directory: '/stickers' }, { unexpected: true }, { items: {} },
    { items: [item, item] }];
  for (const patch of [{ id: '../happy' }, { id: '' }, { file: '../happy.png' },
    { file: 'sub/../../happy.png' }, { file: 'C:/happy.png' }, { file: 'C:happy.png' },
    { file: '\\\\host\\share\\happy.png' }, { file: '/happy.png' }, { file: 'happy.png:stream' },
    { file: 'x/./happy.png' }, { file: 'happy.webp' }, { file: 'happy.png ' },
    { keywords: [] }, { keywords: [' '] }, { keywords: [1] }, { path: 'evil.png' },
    { description: false }, { description: 'x'.repeat(501) }]) {
    invalid.push({ items: [{ ...item, ...patch }] });
  }
  for (const stickers of invalid) {
    const { dir, file } = writeConfig({ stickers });
    try {
      assert.throws(() => loadConfig(file), /stickers/, JSON.stringify(stickers));
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  }
});
