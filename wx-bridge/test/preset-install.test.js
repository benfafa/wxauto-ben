import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import yaml from 'js-yaml';
import { buildManagedBlock, installPreset, loadPresetRow } from '../scripts/install-preset.mjs';
import { apply as applyToolGuard } from '../preset/wx-bridge-tool-guard.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const USER_PATCH = [
  '- insert:',
  '    - id: user-plugin',
  "      name: '@example/user-plugin'",
  ''
].join('\n');

function tempFile(content) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wx-bridge-preset-'));
  const file = path.join(dir, 'cordis.patch.yml');
  fs.writeFileSync(file, content);
  return { dir, file };
}

test('the preset source has the expected shape and no relative plugin paths', () => {
  const row = loadPresetRow();
  assert.equal(row.config.id, 'wx-chat');
  const ids = row.config.plugins.map((plugin) => plugin.id);
  assert.ok(ids.includes('persona'));
  assert.ok(ids.includes('tool-web'));
  const relativeGuard = './wx-bridge-tool-guard.mjs';
  for (const plugin of row.config.plugins) {
    if (plugin.name === relativeGuard) continue;
    assert.ok(!String(plugin.name).startsWith('.') && !path.isAbsolute(String(plugin.name)), `relative plugin: ${plugin.name}`);
  }
  const guard = row.config.plugins.find((plugin) => plugin.id === 'wx-tool-guard');
  assert.equal(guard.name, relativeGuard);
  assert.ok(fs.existsSync(path.join(ROOT, 'preset', 'wx-bridge-tool-guard.mjs')));
});

test('managed block is valid YAML and contains the preset row', () => {
  const block = buildManagedBlock(loadPresetRow());
  assert.match(block, /wx-bridge preset BEGIN/);
  assert.match(block, /wx-bridge preset END/);
  const body = block.split('\n').filter((line) => !line.startsWith('#')).join('\n');
  const parsed = yaml.load(body, { schema: yaml.JSON_SCHEMA });
  assert.ok(Array.isArray(parsed));
  assert.equal(parsed[0].insert[0].id, 'preset-wx-chat');
});

test('installs alongside user patch content and is idempotent', () => {
  const { dir, file } = tempFile(USER_PATCH);
  try {
    const first = installPreset({ patchFile: file });
    assert.equal(first.changed, true);
    assert.equal(first.guardChanged, true);
    assert.ok(fs.existsSync(first.guardTarget));
    assert.match(fs.readFileSync(first.guardTarget, 'utf8'), /tools\.guard/);
    const once = fs.readFileSync(file, 'utf8');
    assert.match(once, /user-plugin/);
    assert.match(once, /preset-wx-chat/);
    const parsed = yaml.load(once, { schema: yaml.JSON_SCHEMA });
    assert.equal(parsed.length, 2);

    const second = installPreset({ patchFile: file });
    assert.equal(second.changed, false);
    assert.equal(second.guardChanged, false);
    assert.equal(fs.readFileSync(file, 'utf8'), once);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('repairs a root [] before appending block operations', () => {
  const { dir, file } = tempFile(`[]\n${USER_PATCH}`);
  try {
    installPreset({ patchFile: file });
    const text = fs.readFileSync(file, 'utf8');
    const parsed = yaml.load(text, { schema: yaml.JSON_SCHEMA });
    assert.ok(Array.isArray(parsed));
    assert.ok(parsed.some((operation) => operation?.insert?.some((entry) => entry?.id === 'preset-wx-chat')));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('refuses to duplicate an unmanaged wx-chat preset', () => {
  const existing = [
    '- insert:',
    '    - id: preset-wx-chat',
    "      name: '@deepseek-ai/dsh-agent-preset'",
    '      config:',
    '        id: wx-chat',
    '        plugins:',
    '          - id: persona',
    "            name: '@deepseek-ai/dsh-persona'",
    ''
  ].join('\n');
  const { dir, file } = tempFile(existing);
  try {
    assert.throws(() => installPreset({ patchFile: file }), /已经存在 wx-chat preset/);
    assert.equal(fs.readFileSync(file, 'utf8'), existing);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('dry-run does not write the file', () => {
  const { dir, file } = tempFile(USER_PATCH);
  try {
    const result = installPreset({ patchFile: file, dryRun: true });
    assert.equal(result.changed, true);
    assert.match(result.next, /preset-wx-chat/);
    assert.equal(fs.readFileSync(file, 'utf8'), USER_PATCH);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('fails when the DSH profile patch file does not exist', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wx-bridge-preset-'));
  try {
    assert.throws(
      () => installPreset({ dshHome: dir, profile: 'web' }),
      /请先启动一次 dsh web/
    );
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('the execution-time guard denies every tool, including globally inherited ones', () => {
  const restricted = [];
  let guard;
  applyToolGuard({
    tools: {
      restrict({ deny }) { restricted.push(deny[0]); },
      guard(fn) { guard = fn; }
    }
  });
  assert.equal(typeof guard, 'function');
  assert.match(guard({ name: 'bash' }), /禁止执行工具/);
  assert.match(guard({ name: 'read_mcp_resource' }), /禁止执行工具/);
  assert.match(guard({ name: 'ask_user_question' }), /禁止执行工具/);
  assert.match(guard({}), /禁止执行工具/);
  assert.ok(restricted.includes('bash'));
  assert.ok(restricted.includes('read_mcp_resource'));
  assert.ok(restricted.includes('working_directory'));
});

test('handles a bare empty patch file and a document marker + []', () => {
  for (const content of ['[]\n', '---\n[]\n']) {
    const { dir, file } = tempFile(content);
    try {
      installPreset({ patchFile: file });
      const parsed = yaml.load(fs.readFileSync(file, 'utf8'), { schema: yaml.JSON_SCHEMA });
      assert.ok(Array.isArray(parsed), `not an array for ${JSON.stringify(content)}`);
      assert.ok(parsed.some((operation) => operation?.insert?.some((entry) => entry?.id === 'preset-wx-chat')));
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  }
});
