import test from 'node:test';
import assert from 'node:assert/strict';
import { Bridge } from '../src/bridge.js';

const quietLogger = {
  info() {},
  warn() {},
  error() {},
  message() {},
  activity() {}
};

function makeCollector() {
  const sent = [];
  return {
    sent,
    health: async () => ({ selfNickname: 'Bot', groups: ['群A'], backend: 'fake' }),
    cursor: async () => ({ collectorId: 'c1', cursor: 0 }),
    poll: async () => ({ collectorId: 'c1', cursor: 0, events: [] }),
    send: async (payload) => {
      sent.push(payload);
      return { ok: true };
    }
  };
}

function makeAgent(reply = '你好，我在。') {
  const calls = [];
  return {
    calls,
    async ask(group, prompt) {
      calls.push({ group, prompt });
      return { text: reply };
    }
  };
}

function makeBridge({ agent = makeAgent(), collector = makeCollector(), behavior = {} } = {}) {
  const cfg = {
    wechat: {
      groups: ['群A'],
      mentionAliases: ['@小助手'],
      contextMessages: 10,
      contextMaxChars: 1000,
      pollWaitMs: 0,
      pollMs: 100
    },
    behavior: {
      requireMention: true,
      replyWithMention: true,
      minReplyIntervalMs: 0,
      maxRepliesPerMinute: 10,
      maxTriggerAgeMs: 300000,
      maxReplyChars: 1200,
      logAllMessages: false,
      globalConcurrency: 1,
      maxQueuePerGroup: 10,
      partDelayMs: 0,
      sendErrorNotice: false,
      ...behavior
    }
  };
  return { bridge: new Bridge({ cfg, collector, agent, logger: quietLogger }), agent, collector, cfg };
}

test('a mentioned message reaches the agent and is replied with @sender', async () => {
  const { bridge, agent, collector } = makeBridge();
  await bridge.handleEvent({ group: '群A', sender: '李四', text: '今天天气不错', id: 'm0', seq: 1 });
  await bridge.handleEvent({ group: '群A', sender: '张三', text: '@小助手\u2005今天几号？', id: 'm1', seq: 2 });
  await bridge.flush();

  assert.equal(agent.calls.length, 1);
  assert.equal(agent.calls[0].group, '群A');
  assert.match(agent.calls[0].prompt, /【群名】群A/);
  assert.match(agent.calls[0].prompt, /【发送者】张三/);
  assert.match(agent.calls[0].prompt, /今天几号/);
  assert.match(agent.calls[0].prompt, /李四：今天天气不错/);

  assert.deepEqual(collector.sent, [{ group: '群A', text: '你好，我在。', at: '张三' }]);
});

test('messages without a mention are context only', async () => {
  const { bridge, agent, collector } = makeBridge();
  await bridge.handleEvent({ group: '群A', sender: '张三', text: '只是聊天', id: 'm1' });
  await bridge.flush();
  assert.equal(agent.calls.length, 0);
  assert.equal(collector.sent.length, 0);
});

test('groups outside the whitelist are ignored', async () => {
  const { bridge, agent } = makeBridge();
  await bridge.handleEvent({ group: '群B', sender: '张三', text: '@小助手 在吗', id: 'm1' });
  await bridge.flush();
  assert.equal(agent.calls.length, 0);
});

test('the same collector message id is only processed once', async () => {
  const { bridge, agent } = makeBridge();
  const event = { group: '群A', sender: '张三', text: '@小助手 在吗', id: 'same-id' };
  await bridge.handleEvent(event);
  await bridge.handleEvent(event);
  await bridge.flush();
  assert.equal(agent.calls.length, 1);
});

test('long replies are split; only the first part mentions the sender', async () => {
  const reply = '一'.repeat(25);
  const { bridge, collector } = makeBridge({
    agent: makeAgent(reply),
    behavior: { maxReplyChars: 10 }
  });
  await bridge.handleEvent({ group: '群A', sender: '张三', text: '@小助手 说点长的', id: 'm1' });
  await bridge.flush();

  assert.equal(collector.sent.length, 3);
  assert.equal(collector.sent[0].at, '张三');
  assert.equal(collector.sent[1].at, null);
  assert.equal(collector.sent[2].at, null);
  assert.equal(collector.sent.map((item) => item.text).join(''), reply);
});

test('replyWithMention=false sends plain text', async () => {
  const { bridge, collector } = makeBridge({ behavior: { replyWithMention: false } });
  await bridge.handleEvent({ group: '群A', sender: '张三', text: '@小助手 在吗', id: 'm1' });
  await bridge.flush();
  assert.equal(collector.sent[0].at, null);
});

test('per-group minimum interval drops rapid second triggers', async () => {
  const { bridge, agent } = makeBridge({ behavior: { minReplyIntervalMs: 60000 } });
  await bridge.handleEvent({ group: '群A', sender: '张三', text: '@小助手 第一条', id: 'm1' });
  await bridge.handleEvent({ group: '群A', sender: '李四', text: '@小助手 第二条', id: 'm2' });
  await bridge.flush();
  assert.equal(agent.calls.length, 1);
});

test('agent errors do not crash or send by default', async () => {
  const agent = {
    calls: [],
    async ask() {
      this.calls.push(1);
      throw new Error('dsh down');
    }
  };
  const { bridge, collector } = makeBridge({ agent });
  await bridge.handleEvent({ group: '群A', sender: '张三', text: '@小助手 在吗', id: 'm1' });
  await bridge.flush();
  assert.equal(agent.calls.length, 1);
  assert.equal(collector.sent.length, 0);
});

test('stale mentions from a backlog are ignored', async () => {
  const { bridge, agent } = makeBridge({ behavior: { maxTriggerAgeMs: 1000 } });
  await bridge.handleEvent({
    group: '群A',
    sender: '张三',
    text: '@小助手 很旧的消息',
    id: 'old',
    timestamp: Date.now() / 1000 - 60
  });
  await bridge.flush();
  assert.equal(agent.calls.length, 0);
});

test('run() polls the collector, replies, and exits on abort', async () => {
  const events = [];
  const sent = [];
  const collector = {
    health: async () => ({ selfNickname: 'Bot', groups: ['群A'], backend: 'fake' }),
    cursor: async () => ({ collectorId: 'c1', cursor: 0 }),
    poll: async () => {
      if (events.length > 0) {
        const batch = events.splice(0);
        return { collectorId: 'c1', cursor: batch[batch.length - 1].seq, events: batch };
      }
      await new Promise((resolve) => setTimeout(resolve, 10));
      return { collectorId: 'c1', cursor: 0, events: [] };
    },
    send: async (payload) => {
      sent.push(payload);
      return { ok: true };
    }
  };
  const agent = makeAgent('收到');
  const { bridge } = makeBridge({ agent, collector });
  const abort = new AbortController();
  const running = bridge.run(abort.signal);
  await new Promise((resolve) => setTimeout(resolve, 30));
  events.push({
    group: '群A',
    sender: '张三',
    text: '@小助手 在吗',
    id: 'm1',
    seq: 1,
    timestamp: Date.now() / 1000
  });
  for (let i = 0; i < 100 && sent.length === 0; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  abort.abort();
  await running;
  assert.equal(agent.calls.length, 1);
  assert.deepEqual(sent, [{ group: '群A', text: '收到', at: '张三' }]);
});
