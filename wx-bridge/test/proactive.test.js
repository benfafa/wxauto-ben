import test from 'node:test';
import assert from 'node:assert/strict';
import { Bridge } from '../src/bridge.js';
import { buildProactivePrompt, normalizeProactive } from '../src/proactive.js';
import { parseProactiveReply } from '../src/stickers.js';

const quiet = { info() {}, warn() {}, error() {}, message() {}, activity() {} };

function spyLogger() {
  const events = [];
  return {
    events,
    info() {}, warn() {}, error() {}, message() {},
    activity(entry) { events.push(entry); }
  };
}

function makeCollector() {
  const sent = [];
  return {
    sent,
    health: async () => ({ selfNickname: 'Bot', groups: ['群A'], backend: 'fake' }),
    cursor: async () => ({ collectorId: 'c1', cursor: 0 }),
    poll: async () => ({ collectorId: 'c1', cursor: 0, events: [] }),
    send: async (payload) => { sent.push(payload); return { ok: true }; },
    sendSticker: async (payload) => { sent.push(payload); return { ok: true }; }
  };
}

function makeAgent(reply) {
  const calls = [];
  return {
    calls,
    async ask(group, prompt) { calls.push({ group, prompt }); return { text: reply }; }
  };
}

function bridge({ proactive, agent, collector, logger } = {}) {
  const cfg = {
    stickers: { enabled: false, items: [] },
    vision: { enabled: false },
    proactive,
    wechat: { groups: ['群A'], mentionAliases: ['@小助手'], contextMessages: 10,
      contextMaxChars: 1000, pollWaitMs: 0, pollMs: 100 },
    behavior: { requireMention: true, replyWithMention: true, minReplyIntervalMs: 0,
      maxRepliesPerMinute: 10, maxTriggerAgeMs: 300000, maxReplyChars: 1200, logAllMessages: false,
      globalConcurrency: 1, maxQueuePerGroup: 10, partDelayMs: 0, sendErrorNotice: false,
      memoryMaxEntries: 100, memoryResults: 3, memoryMaxChars: 500 }
  };
  return new Bridge({ cfg, collector: collector ?? makeCollector(), agent: agent ?? makeAgent('{"text":"这句我能接","stickerId":null}'),
    logger: logger ?? quiet });
}

const ACTIVE = { enabled: true, chance: 100, minIntervalMs: 0, maxPerHour: 10,
  activityWindowMs: 120000, minRecentMessages: 2, groups: [] };

async function chatter(instance, count = 2) {
  for (let i = 0; i < count; i += 1) {
    await instance.handleEvent({ group: '群A', sender: `群友${i}`, text: `闲聊第 ${i} 条`, id: `c${i}`, seq: i + 1 });
  }
  await instance.flush();
}

test('proactive 默认关闭：光聊天不 @ 也不会触发模型', async () => {
  const agent = makeAgent('{"text":"不该说话","stickerId":null}');
  const instance = bridge({ proactive: normalizeProactive(undefined), agent });
  await chatter(instance, 3);
  assert.equal(agent.calls.length, 0);
});

test('开启后按概率插话，并且不 @ 任何人', async () => {
  const collector = makeCollector();
  const agent = makeAgent('{"text":"这个我熟","stickerId":null}');
  const instance = bridge({ proactive: ACTIVE, agent, collector });
  await chatter(instance);
  assert.equal(agent.calls.length, 1);
  assert.match(agent.calls[0].prompt, /没有人 @ 你/);
  assert.doesNotMatch(agent.calls[0].prompt, /【被 @ 的消息】/);
  assert.equal(collector.sent.length, 1);
  assert.equal(collector.sent[0].at, null);
  assert.equal(collector.sent[0].text, '这个我熟');
});

test('模型返回空 text 就是沉默，不算失败也不发消息', async () => {
  const collector = makeCollector();
  const logger = spyLogger();
  const agent = makeAgent('{"text":"","stickerId":null}');
  const instance = bridge({ proactive: ACTIVE, agent, collector, logger });
  await chatter(instance);
  assert.equal(collector.sent.length, 0);
  assert.ok(logger.events.some((entry) => entry.type === 'proactive-declined'));
});

test('没按协议返回（散文/坏 JSON）一律当沉默，绝不把提示词漏进群', async () => {
  for (const reply of ['我觉得应该插一句', '{bad json', '{"text":"ok","extra":1}', '{"stickerId":"a"}']) {
    const collector = makeCollector();
    const instance = bridge({ proactive: ACTIVE, agent: makeAgent(reply), collector });
    await chatter(instance);
    assert.equal(collector.sent.length, 0, `不该发送：${reply}`);
  }
});

test('摇号没中就不说话，并把原因写进 activity 便于回看', async () => {
  const collector = makeCollector();
  const logger = spyLogger();
  const instance = bridge({ proactive: { ...ACTIVE, chance: 0 }, collector, logger });
  await chatter(instance);
  assert.equal(collector.sent.length, 0);
  const skip = logger.events.find((entry) => entry.type === 'proactive-skip');
  assert.equal(skip.reason, 'chance');
});

test('每小时上限管住连续热聊：第二条不会连着插话', async () => {
  const collector = makeCollector();
  const agent = makeAgent('{"text":"来了","stickerId":null}');
  const instance = bridge({ proactive: { ...ACTIVE, maxPerHour: 1 }, agent, collector });
  await chatter(instance, 2);
  await instance.handleEvent({ group: '群A', sender: '群友9', text: '接着聊', id: 'c9', seq: 9 });
  await instance.flush();
  assert.equal(agent.calls.length, 1);
  assert.equal(collector.sent.length, 1);
});

test('消息太少不算「有人在聊」，自己发的消息也不计入', async () => {
  const agent = makeAgent('{"text":"冒泡","stickerId":null}');
  const instance = bridge({ proactive: ACTIVE, agent });
  await instance.handleEvent({ group: '群A', sender: '张三', text: '只有一条', id: 's1', seq: 1 });
  await instance.handleEvent({ group: '群A', sender: '小助手', text: '我自己说的话', id: 's2', seq: 2 });
  await instance.handleEvent({ group: '群A', sender: '小助手', text: '再刷一条', id: 's3', seq: 3 });
  await instance.flush();
  assert.equal(agent.calls.length, 0);
});

test('群里刚被 @ 回过，就不立刻再主动插话', async () => {
  const agent = makeAgent('{"text":"先回答再插话","stickerId":null}');
  const collector = makeCollector();
  const instance = bridge({ proactive: ACTIVE, agent, collector });
  await instance.handleEvent({ group: '群A', sender: '张三', text: '随便聊聊', id: 'a1', seq: 1 });
  await instance.handleEvent({ group: '群A', sender: '李四', text: '@小助手 在吗', id: 'a2', seq: 2 });
  await instance.flush();
  // 第一条不该触发（消息数不够），第二条是 @ 回合：只应有一次调用。
  assert.equal(agent.calls.length, 1);
  assert.match(agent.calls[0].prompt, /【被 @ 的消息】/);
});

test('normalizeProactive 对每一项都 fail-closed', () => {
  assert.deepEqual(normalizeProactive(undefined), { enabled: false, chance: 12, minIntervalMs: 360000,
    maxPerHour: 3, activityWindowMs: 120000, minRecentMessages: 2, groups: [] });
  assert.deepEqual(normalizeProactive({ groups: ['群A', '群A', ' 群B '] }).groups, ['群A', '群B']);
  for (const bad of [{ chance: 101 }, { chance: -1 }, { minIntervalMs: 1000 }, { maxPerHour: 0 },
    { activityWindowMs: 100 }, { minRecentMessages: 0 }, { groups: '群A' }, { groups: [''] },
    { enabled: 'true' }, { typo: 1 }]) {
    assert.throws(() => normalizeProactive(bad), /proactive/, `应该拒绝：${JSON.stringify(bad)}`);
  }
  assert.throws(() => normalizeProactive([]), /对象/);
});

test('主动回合的提示词把「沉默」设成默认答案', () => {
  const prompt = buildProactivePrompt({ group: '群A', context: '张三：加班到十点', memory: '（无）',
    stickers: { enabled: false, items: [] }, vision: { enabled: false } });
  assert.match(prompt, /大多数时候不值得/);
  assert.match(prompt, /返回空字符串/);
  assert.match(prompt, /不要 @ 任何人/);
  assert.match(prompt, /不得自称 AI/);
  assert.match(prompt, /张三：加班到十点/);
  assert.match(prompt, /不会给你任何图片/);
});

test('parseProactiveReply 只认协议内的非空 text，表情 ID 仍要过白名单', () => {
  const stickers = { enabled: true, items: [{ id: '001', file: '001.gif', keywords: ['哈哈'] }] };
  assert.deepEqual(parseProactiveReply('{"text":"接一句","stickerId":"001"}', stickers),
    { text: '接一句', stickerId: '001', declined: false });
  assert.equal(parseProactiveReply('{"text":"接一句","stickerId":"../evil"}', stickers).stickerId, null);
  assert.equal(parseProactiveReply('{"text":"  ","stickerId":null}', stickers).declined, true);
  assert.equal(parseProactiveReply('接一句', stickers).declined, true);
  assert.equal(parseProactiveReply(undefined, stickers).declined, true);
});
