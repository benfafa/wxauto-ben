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
