import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { DshAgent } from '../src/dsh-agent.js';

const quietLogger = { info() {}, warn() {}, error() {} };

class MemoryStore {
  constructor() {
    this.map = new Map();
  }
  get(key) { return this.map.get(key) ?? null; }
  set(key, value) { this.map.set(key, value); }
  remove(key) { this.map.delete(key); }
  entries() { return [...this.map.entries()]; }
}

function ok(value) {
  return { result: { ok: true, value } };
}

function createFakeApi({ presets = [{ id: 'wx-chat' }], promptErrorOnce = null, reply = '最终回复' } = {}) {
  const inbox = [];
  let wake = null;
  let created = 0;
  let sessionId = 's1';
  let promptCount = 0;
  const prompts = [];
  const followed = [];
  const responses = [];
  let pendingError = promptErrorOnce;

  const push = (payload) => {
    inbox.push({ payload });
    wake?.();
    wake = null;
  };

  const api = {
    _desiredFollows: new Set(),
    agentPresets: {
      list: async () => ok({ presets })
    },
    workspace: {
      create: async () => ok({ created: true, workspace: { workspaceId: 'ws1' } }),
      rename: async () => ok({}),
      archiveSession: async () => ok({})
    },
    sessions: {
      create: async () => {
        created += 1;
        sessionId = `s${created}`;
        return ok({ sessionId });
      },
      selectModel: async () => ok({ selected: {} }),
      prompt: async (params) => {
        prompts.push(params);
        if (pendingError) {
          const error = pendingError;
          pendingError = null;
          return { result: { ok: false, error } };
        }
        promptCount += 1;
        const turn = `t${promptCount}`;
        setTimeout(() => {
          push({ type: 'session/event', sessionId: params.sessionId, event: { type: 'turn/start', data: { turn } } });
          push({
            type: 'session/event',
            sessionId: params.sessionId,
            event: {
              type: 'assistant/message',
              data: { turn, message: { content: [{ type: 'text', text: reply }] } }
            }
          });
          push({ type: 'session/event', sessionId: params.sessionId, event: { type: 'turn/end', data: { turn, reason: { kind: 'completed' } } } });
        }, 5);
        return ok({});
      }
    },
    events: {
      follow: (id) => { followed.push(id); api._desiredFollows.add(id); },
      forget: (id) => api._desiredFollows.delete(id),
      mux: (_payload, signal, onOpen) => {
        onOpen?.();
        return (async function* generate() {
          while (!signal?.aborted) {
            if (inbox.length > 0) {
              yield inbox.shift();
              continue;
            }
            await new Promise((resolve) => {
              wake = resolve;
              setTimeout(resolve, 10);
            });
          }
        })();
      }
    },
    respond: async (message) => { responses.push(message); return ok({}); }
  };

  return {
    api,
    push,
    responses,
    prompts,
    followed,
    get created() { return created; },
    get sessionId() { return sessionId; }
  };
}

function makeConfig(sessionCwd) {
  return {
    dsh: {
      baseUrl: 'http://127.0.0.1:3080',
      authToken: '',
      allowRemote: false,
      provider: 'deepseek-official',
      model: 'deepseek-flash',
      reasoningEffort: 'max',
      agentPreset: 'wx-chat',
      verifyPreset: true,
      allowDefaultPreset: false,
      workspaceTitle: '微信群聊',
      sessionCwd,
      turnTimeoutMs: 2000,
      streamReconnectMs: 500
    }
  };
}

test('creates a session with the preset and collects the turn reply', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wx-bridge-dsh-'));
  const fake = createFakeApi();
  const store = new MemoryStore();
  const agent = new DshAgent(makeConfig(dir), { store, logger: quietLogger, api: fake.api });
  await agent.start();
  try {
    const result = await agent.ask('群A', '提示词内容');
    assert.equal(result.text, '最终回复');
    assert.equal(store.get('群A'), 's1');
    assert.deepEqual(fake.followed, ['s1']);
    assert.equal(fake.prompts.length, 1);
    assert.match(fake.prompts[0].content[0].text, /提示词内容/);
  } finally {
    await agent.stop();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('rebuilds the session once when DSH says session/not-found', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wx-bridge-dsh-'));
  const fake = createFakeApi({
    promptErrorOnce: { code: 'session/not-found', message: 'gone' }
  });
  const store = new MemoryStore();
  const agent = new DshAgent(makeConfig(dir), { store, logger: quietLogger, api: fake.api });
  await agent.start();
  try {
    const result = await agent.ask('群A', '重试内容');
    assert.equal(result.text, '最终回复');
    assert.equal(fake.created, 2);
    assert.equal(store.get('群A'), 's2');
    assert.deepEqual(fake.followed, ['s1', 's2']);
  } finally {
    await agent.stop();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('reuses a stored session only after re-following it', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wx-bridge-dsh-'));
  const fake = createFakeApi();
  const store = new MemoryStore();
  store.set('群A', 's1');            // bridge 重启后从 sessions.json 读回的旧会话
  const agent = new DshAgent(makeConfig(dir), { store, logger: quietLogger, api: fake.api });
  await agent.start();
  try {
    const result = await agent.ask('群A', '重启后的第一条');
    assert.equal(result.text, '最终回复');
    assert.equal(fake.created, 0, '旧会话可用时不应重建');
    assert.deepEqual(fake.followed, ['s1'], '必须重新 follow，否则收不到 turn 事件');
  } finally {
    await agent.stop();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('verified inline content survives session rebuild with one accepted turn', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wx-bridge-dsh-'));
  const fake = createFakeApi({ promptErrorOnce: { code: 'session/not-found', message: 'gone' } });
  const cfg = { ...makeConfig(dir), vision: { enabled: true } };
  const agent = new DshAgent(cfg, { store: new MemoryStore(), logger: quietLogger, api: fake.api });
  const image = { mediaType: 'image/png', data: 'iVBORw0KGgo=', name: 'wx.png' };
  await agent.start();
  try {
    await agent.ask('群A', '看图', { images: [image] });
    assert.equal(fake.prompts.length, 2);
    for (const prompt of fake.prompts) assert.deepEqual(prompt.content, [{ type: 'text', text: '看图' }, { type: 'image', ...image }]);
    assert.equal(fake.created, 2);
    await assert.rejects(agent.ask('群A', 'bad', { images: [{ ...image, data: 'data:image/png;base64,bad' }] }), /vision/);
    assert.equal(fake.prompts.length, 2);
  } finally { await agent.stop(); fs.rmSync(dir, { recursive: true, force: true }); }
});

test('image/model refusal never resubmits an accepted or rejected non-session turn', async () => {
  for (const accepted of [false, true]) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wx-bridge-dsh-'));
    const fake = createFakeApi({ promptErrorOnce: accepted ? null : { code: 'image/not-found', message: 'image unsupported' } });
    if (accepted) {
      const original = fake.api.sessions.prompt;
      fake.api.sessions.prompt = async (params) => {
        await original(params);
        return ok({});
      };
    }
    const cfg = { ...makeConfig(dir), vision: { enabled: true } };
    if (accepted) cfg.dsh.turnTimeoutMs = 1;
    const agent = new DshAgent(cfg, { store: new MemoryStore(), logger: quietLogger, api: fake.api });
    await agent.start();
    try {
      await assert.rejects(agent.ask('群A', '看图', { images: [{ mediaType: 'image/png', data: 'iVBORw0KGgo=', name: 'wx.png' }] }));
      assert.equal(fake.prompts.length, 1);
      assert.equal(fake.created, 1);
    } finally { await agent.stop(); fs.rmSync(dir, { recursive: true, force: true }); }
  }
});

test('vision model selection failure cannot fall back to another model', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wx-bridge-dsh-'));
  const fake = createFakeApi();
  fake.api.sessions.selectModel = async () => { throw new Error('model unavailable'); };
  const agent = new DshAgent({ ...makeConfig(dir), vision: { enabled: true } }, { store: new MemoryStore(), logger: quietLogger, api: fake.api });
  await agent.start();
  try {
    await assert.rejects(agent.ask('群A', '看图'), /拒绝切换/);
    assert.equal(fake.prompts.length, 0);
  } finally { await agent.stop(); fs.rmSync(dir, { recursive: true, force: true }); }
});

test('approval grants allowed-once only to followed, stored or active project sessions', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wx-bridge-dsh-'));
  const fake = createFakeApi();
  const store = new MemoryStore();
  const logs = [];
  const logger = { ...quietLogger, info: (text) => logs.push(text), warn: (text) => logs.push(text) };
  const agent = new DshAgent(makeConfig(dir), { store, logger, api: fake.api });
  await agent.start();
  const request = async (sessionId, eventId) => {
    fake.push({ type: 'approval/requested', sessionId, clientId: 'client1', eventId, toolName: 'web_search' });
    await new Promise((resolve) => setImmediate(resolve));
  };
  try {
    await request('foreign-session', 'unknown');
    await request(undefined, 'missing');
    await request('', 'empty');
    assert.deepEqual(fake.responses, [], '未知或缺失会话不能发送审批回执');
    assert.ok(logs.some((line) => /未知会话.*未发送回执/.test(line)));

    store.set('群A', 'stored-session');
    await request('stored-session', 'stored');
    fake.api.events.follow('followed-session');
    await request('followed-session', 'followed');
    agent.waiters.set('active-session', { reject() {} });
    await request('active-session', 'active');
    agent.waiters.delete('active-session');
    assert.deepEqual(fake.responses, ['stored', 'followed', 'active'].map((eventId) => ({
      clientId: 'client1', eventId, outcome: { kind: 'result', value: 'allowed-once' }
    })));
    assert.equal(logs.filter((line) => /自动放行工具审批 allowed-once/.test(line)).length, 3);

    fake.api.events.forget('followed-session');
    store.remove('群A');
    await request('followed-session', 'forgotten');
    await request('stored-session', 'removed');
    await request('active-session', 'inactive');
    assert.equal(fake.responses.length, 3, '归属依据移除后不能凭历史记录放行');

    fake.push({ type: 'question/requested', sessionId: 'stored-session', clientId: 'client1', eventId: 'question' });
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(fake.responses[3], {
      clientId: 'client1', eventId: 'question', outcome: { kind: 'result', value: { answers: [] } }
    });

    store.set('群A', 'stored-session');
    fake.api.respond = async () => { throw new Error('approval transport failed'); };
    await request('stored-session', 'failed');
    assert.ok(logs.some((line) => /回执审批失败：approval transport failed/.test(line)));
  } finally {
    await agent.stop();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('fails loudly when the configured preset is missing', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wx-bridge-dsh-'));
  const fake = createFakeApi({ presets: [{ id: 'standard' }] });
  const agent = new DshAgent(makeConfig(dir), { store: new MemoryStore(), logger: quietLogger, api: fake.api });
  await assert.rejects(agent.start(), /wx-chat/);
  fs.rmSync(dir, { recursive: true, force: true });
});
