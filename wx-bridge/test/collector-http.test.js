import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { CollectorClient } from '../src/wechat-client.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const TOKEN = '0123456789abcdef0123456789abcdef';
const PYTHON = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3');

function waitForUrl(child, timeoutMs = 10000) {
  return new Promise((resolve, reject) => {
    let stdout = '';
    let stderr = '';
    const timer = setTimeout(() => {
      reject(new Error(`collector did not start in ${timeoutMs}ms\nstdout:\n${stdout}\nstderr:\n${stderr}`));
    }, timeoutMs);
    const onData = (chunk) => {
      stdout += chunk.toString();
      const match = stdout.match(/\[collector\] url=(http:\/\/\S+)/);
      if (match) {
        clearTimeout(timer);
        child.stdout.off('data', onData);
        resolve(match[1]);
      }
    };
    child.stdout.on('data', onData);
    child.stderr.on('data', (chunk) => { stderr += chunk.toString(); });
    child.once('exit', (code) => {
      clearTimeout(timer);
      reject(new Error(`collector exited early with ${code}\nstdout:\n${stdout}\nstderr:\n${stderr}`));
    });
  });
}

test('Node CollectorClient talks to the real Python fake collector', { timeout: 20000 }, async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wx-bridge-collector-'));
  const sendLog = path.join(dir, 'sent.jsonl');
  const child = spawn(PYTHON, [
    '-m', 'collector.wx_collector',
    '--backend', 'fake',
    '--groups', '测试群',
    '--token', TOKEN,
    '--port', '0',
    '--allow-inject',
    '--send-log', sendLog
  ], {
    cwd: ROOT,
    env: { ...process.env, PYTHONUNBUFFERED: '1' },
    stdio: ['ignore', 'pipe', 'pipe']
  });

  let url;
  try {
    url = await waitForUrl(child);
    const client = new CollectorClient({ baseUrl: url, token: TOKEN, timeoutMs: 5000 });
    const health = await client.health();
    assert.equal(health.backend, 'fake');
    assert.equal(health.selfNickname, 'FakeBot');
    assert.deepEqual(health.groups, ['测试群']);

    const before = await client.cursor();
    const inject = await fetch(`${url}/inject`, {
      method: 'POST',
      headers: { authorization: `Bearer ${TOKEN}`, 'content-type': 'application/json' },
      body: JSON.stringify({ group: '测试群', sender: '张三', text: '@小助手 在吗', id: 'm1' })
    });
    assert.equal(inject.status, 200);

    const polled = await client.poll({ since: before.cursor, waitMs: 3000 });
    assert.equal(polled.events.length, 1);
    assert.equal(polled.events[0].sender, '张三');
    assert.equal(polled.events[0].text, '@小助手 在吗');

    await client.send({ group: '测试群', text: '你好', at: '张三' });
    const lines = fs.readFileSync(sendLog, 'utf8').trim().split('\n');
    assert.equal(lines.length, 1);
    const sent = JSON.parse(lines[0]);
    assert.equal(sent.group, '测试群');
    assert.equal(sent.text, '你好');
    assert.equal(sent.at, '张三');
  } finally {
    if (child.exitCode === null && child.signalCode === null) {
      child.kill('SIGTERM');
      await new Promise((resolve) => {
        const timer = setTimeout(resolve, 3000);
        child.once('exit', () => {
          clearTimeout(timer);
          resolve();
        });
      });
    }
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
