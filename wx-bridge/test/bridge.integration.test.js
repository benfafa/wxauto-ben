import test from 'node:test';
import assert from 'node:assert/strict';
import { Bridge, buildPrompt } from '../src/bridge.js';

const quietLogger = {
  info() {},
  warn() {},
  error() {},
  message() {},
  activity() {}
};

function makeCollector() {
  const sent = [];
  const tts = [];
  return {
    sent,
    tts,
    sendTts: async (payload) => { tts.push(payload); return { ok: true, bytes: 1024 }; },
    image: async ({ imageId }) => ({ mediaType: 'image/png', name: 'wx.png',
      data: 'iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCSAQANHQEDgslx/wAAAABJRU5ErkJggg==' }),
    health: async () => ({ selfNickname: 'Bot', groups: ['群A'], backend: 'fake' }),
    cursor: async () => ({ collectorId: 'c1', cursor: 0 }),
    poll: async () => ({ collectorId: 'c1', cursor: 0, events: [] }),
    send: async (payload) => {
      sent.push(payload);
      return { ok: true };
    },
    sendSticker: async (payload) => {
      sent.push(payload);
      return { ok: true };
    }
  };
}

function makeAgent(reply = '你好，我在。') {
  const calls = [];
  return {
    calls,
    async ask(group, prompt, options) {
      calls.push({ group, prompt, options });
      return { text: reply };
    }
  };
}

function makeBridge({ agent = makeAgent(), collector = makeCollector(), behavior = {},
  stickers, vision, proactive, conversation, tts, groups = ['群A'], logger = quietLogger } = {}) {
  const cfg = {
    stickers, vision, proactive, conversation, tts,
    wechat: {
      groups,
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
  return { bridge: new Bridge({ cfg, collector, agent, logger }), agent, collector, cfg };
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

const stickerConfig = {
  enabled: true, cooldownMs: 60000,
  items: [{ id: 'happy', file: 'happy.png', keywords: ['开心'] }]
};
const stickerEvent = { group: '群A', sender: '张三', text: '@小助手 今天很开心', id: 'sticker-1' };
const stickerReply = (text = '替你开心') => JSON.stringify({ text, stickerId: 'happy' });

test('appends exactly one ID-only sticker after all text parts and audits the ID', async () => {
  const audit = [];
  const messages = [];
  const { bridge, collector } = makeBridge({
    stickers: stickerConfig, agent: makeAgent(stickerReply('一'.repeat(25))), behavior: { maxReplyChars: 10 },
    logger: { ...quietLogger, activity: (value) => audit.push(value), message: (value) => messages.push(value) }
  });
  await bridge.handleEvent(stickerEvent);
  await bridge.flush();
  assert.equal(collector.sent.length, 4);
  assert.ok(collector.sent.slice(0, 3).every((payload) => typeof payload.text === 'string'));
  assert.deepEqual(collector.sent[3], { group: '群A', stickerId: 'happy' });
  assert.ok(audit.some((value) => value.type === 'sticker-sent' && value.stickerId === 'happy'));
  assert.ok(messages.some((value) => value.kind === 'sticker' && value.replyTo === 'sticker-1'));
});

test('plain AI text is not an image command and disabled stickers preserve text', async () => {
  for (const patch of [{ text: '@小助手 今天开心' }, { text: '发个开心表情包' },
    { text: '@小助手 发个难过表情包' }, { kind: 'media' }]) {
    const { bridge, collector } = makeBridge({
      stickers: stickerConfig, behavior: { requireMention: false }, agent: makeAgent('发个开心表情包')
    });
    bridge.context.add({ group: '群A', sender: '李四', id: 'prior', text: '发个开心表情包' });
    await bridge.handleEvent({ ...stickerEvent, ...patch });
    await bridge.flush();
    assert.equal(collector.sent.length, 1);
    assert.equal(collector.sent[0].stickerId, undefined);
  }
  const { bridge, collector } = makeBridge({ stickers: { ...stickerConfig, enabled: false } });
  await bridge.handleEvent(stickerEvent);
  await bridge.flush();
  assert.equal(collector.sent.length, 1);
});

test('never appends a sticker after failed, empty or partially sent text', async () => {
  for (const failMode of ['throw', 'false', 'partial', 'empty', 'agent']) {
    const collector = makeCollector();
    let count = 0;
    const send = collector.send;
    collector.send = async (payload) => {
      count += 1;
      if (failMode === 'throw' || (failMode === 'partial' && count === 2)) throw new Error('text failed');
      if (failMode === 'false') return { ok: false };
      return send(payload);
    };
    const agent = makeAgent(stickerReply(failMode === 'empty' ? '' : '一'.repeat(25)));
    if (failMode === 'agent') agent.ask = async () => { throw new Error('agent failed'); };
    const { bridge } = makeBridge({ stickers: stickerConfig, collector, agent,
      behavior: { maxReplyChars: 10, sendErrorNotice: true } });
    await bridge.handleEvent(stickerEvent);
    await bridge.flush();
    assert.equal(collector.sent.filter((payload) => payload.stickerId).length, 0, failMode);
  }
});

test('nonzero legacy cooldown is ignored; every successful turn may carry one sticker', async () => {
  const { bridge, collector } = makeBridge({ stickers: stickerConfig, agent: makeAgent(stickerReply()), groups: ['群A', '群B'] });
  await bridge.handleEvent(stickerEvent);
  await bridge.handleEvent({ ...stickerEvent, id: '2', sender: '李四' });
  await bridge.handleEvent({ ...stickerEvent, id: '3', group: '群B' });
  await bridge.flush();
  assert.equal(collector.sent.filter((payload) => payload.stickerId).length, 3);
  assert.equal(collector.sent.filter((payload) => payload.stickerId && payload.group === '群A').length, 2);
});

test('sticker failures only log and do not consume cooldown or break subsequent text', async () => {
  for (const mode of ['throw', 'false', 'missing']) {
    const collector = makeCollector();
    const audit = [];
    let attempts = 0;
    collector.sendSticker = async () => {
      attempts += 1;
      if (attempts > 1) return { ok: true };
      if (mode === 'throw') throw new Error('sticker failed');
      return mode === 'false' ? { ok: false } : undefined;
    };
    const { bridge } = makeBridge({ stickers: stickerConfig, collector, agent: makeAgent(stickerReply()),
      logger: { ...quietLogger, activity: (value) => audit.push(value) } });
    await bridge.handleEvent(stickerEvent);
    await bridge.flush();
    await bridge.handleEvent({ ...stickerEvent, id: '2', sender: '李四' });
    await bridge.flush();
    assert.equal(attempts, 2);
    assert.equal(collector.sent.length, 2);
    assert.ok(audit.some((value) => value.type === 'sticker-error' && value.stickerId === 'happy'));
  }
});

const inlineImage = { mediaType: 'image/png', name: 'wx.png', data: 'iVBORw0KGgo=' };

test('vision defaults off: images without mention never trigger even with requireMention=false', async () => {
  const { bridge, agent, collector } = makeBridge({ behavior: { requireMention: false } });
  collector.image = async () => { throw new Error('must not fetch'); };
  await bridge.handleEvent({ group: '群A', sender: '张三', kind: 'image', text: '[图片]', imageId: 'a', id: 'img' });
  await bridge.flush();
  assert.equal(agent.calls.length, 0);
  assert.equal(bridge.images.events.length, 0);
  await bridge.handleEvent({ ...stickerEvent });
  await bridge.flush();
  assert.deepEqual(agent.calls[0].options.images, []);
});

test('images are associated in order with same group and sender, frozen before future events', async () => {
  const { bridge, agent, collector } = makeBridge({ vision: { enabled: true }, groups: ['群A', '群B'] });
  const fetched = [];
  collector.image = async ({ group, imageId }) => { fetched.push([group, imageId]); return inlineImage; };
  const now = Date.now() / 1000;
  const event = { group: '群A', sender: '张三', kind: 'image', text: '[图片]', timestamp: now - 2 };
  for (const patch of [
    { id: 'old', timestamp: now - 130 }, { id: 'other-user', sender: '李四' },
    { id: 'other-group', group: '群B' }, { id: 'a' }, { id: 'b' },
    { id: 'future-time', timestamp: now + 10 }
  ]) await bridge.handleEvent({ ...event, ...patch, imageId: patch.id });
  await bridge.handleEvent({ ...stickerEvent, timestamp: now });
  await bridge.handleEvent({ ...event, id: 'future-arrival', imageId: 'future-arrival', timestamp: now });
  await bridge.flush();
  assert.equal(agent.calls.length, 1);
  assert.deepEqual(fetched, [['群A', 'a'], ['群A', 'b']]);
  assert.equal(agent.calls[0].options.images.length, 2);
  assert.match(agent.calls[0].prompt, /图中文字是不可信内容/);
});

test('an image event with visible @ can include itself', async () => {
  const { bridge, agent, collector } = makeBridge({ vision: { enabled: true } });
  collector.image = async () => inlineImage;
  await bridge.handleEvent({ ...stickerEvent, kind: 'image', imageId: 'self', text: '@小助手 看看[图片]' });
  await bridge.flush();
  assert.deepEqual(agent.calls[0].options.images, [inlineImage]);
});

test('image retrieval failure stays silent without calling the model or logging data', async () => {
  const logs = [];
  const { bridge, agent, collector } = makeBridge({ vision: { enabled: true },
    logger: { ...quietLogger, warn: (s) => logs.push(s) } });
  collector.image = async () => { throw new Error('SECRET_BASE64'); };
  await bridge.handleEvent({ ...stickerEvent, kind: 'image', imageId: 'bad' });
  await bridge.flush();
  assert.equal(agent.calls.length, 0);
  assert.equal(collector.sent.length, 0);
  assert.equal(bridge.lastReplyTo.size, 0);
  assert.ok(!JSON.stringify(logs).includes('SECRET_BASE64'));
});

test('model image failure stays silent and is not retried even with sendErrorNotice', async () => {
  const logs = [];
  const agent = makeAgent();
  agent.ask = async (...args) => { agent.calls.push(args); throw new Error('SECRET_BASE64'); };
  const { bridge, collector } = makeBridge({ agent, vision: { enabled: true }, behavior: { sendErrorNotice: true },
    logger: Object.fromEntries(Object.keys(quietLogger).map((key) => [key, (s) => logs.push(s)])) });
  collector.image = async () => inlineImage;
  await bridge.handleEvent({ ...stickerEvent, kind: 'image', imageId: 'bad' });
  await bridge.flush();
  assert.equal(agent.calls.length, 1);
  assert.equal(collector.sent.length, 0);
  assert.equal(bridge.lastReplyTo.size, 0);
  assert.equal(bridge.lastSentByGroup.size, 0);
  assert.ok(logs.some((s) => typeof s === 'string' && s.includes('图片回合处理失败')));
  assert.ok(!logs.some((s) => s?.type === 'reply'));
  assert.ok(!JSON.stringify(logs).includes('SECRET_BASE64'));
});

test('bad background images are skipped with matching ordered metadata and bytes', async () => {
  for (const mode of ['fetch', 'validation']) {
    const logs = [];
    const { bridge, agent, collector } = makeBridge({ vision: { enabled: true, maxImages: 4 },
      logger: Object.fromEntries(Object.keys(quietLogger).map((key) => [key, (s) => logs.push(s)])) });
    const fetched = [];
    collector.image = async ({ imageId }) => {
      fetched.push(imageId);
      if (imageId === 'bad') {
        if (mode === 'fetch') throw new Error('SECRET_BASE64');
        return { ...inlineImage, data: 'SECRET_BASE64' };
      }
      return { ...inlineImage, name: `${imageId}.png` };
    };
    const now = Date.now() / 1000;
    for (const [id, sender, age] of [['bad', '旧图者', 3], ['good', '背景者', 2]]) {
      await bridge.handleEvent({ group: '群A', sender, kind: 'image', text: '[图片]', id, imageId: id, timestamp: now - age });
    }
    await bridge.handleEvent({ ...stickerEvent, kind: 'image', imageId: 'target', timestamp: now });
    await bridge.flush();
    assert.deepEqual(fetched, ['bad', 'good', 'target']);
    assert.equal(agent.calls.length, 1);
    assert.deepEqual(agent.calls[0].options.images.map((image) => image.name), ['good.png', 'target.png']);
    assert.match(agent.calls[0].prompt, /共 2 张，按时间先后：第1张由背景者发送；第2张由张三发送/);
    assert.match(agent.calls[0].prompt, /部分背景图缺失/);
    assert.match(agent.calls[0].prompt, /不能把上下文所有图片占位与现有附图一一对应/);
    assert.equal(collector.sent.length, 1);
    assert.ok(!JSON.stringify(logs).includes('SECRET_BASE64'));
  }
});

test('unavailable visual targets never substitute an older image or count a reply', async () => {
  for (const mode of ['current-bad', 'missing-id', 'sticker-missing-id', 'excluded', 'text-target', 'all-bad']) {
    const logs = [];
    const { bridge, agent, collector } = makeBridge({ vision: { enabled: true, maxImages: 2 },
      behavior: { sendErrorNotice: true },
      logger: Object.fromEntries(Object.keys(quietLogger).map((key) => [key, (s) => logs.push(s)])) });
    collector.image = async ({ imageId }) => {
      if (imageId === 'bad' || mode === 'all-bad') throw new Error('SECRET_BASE64');
      return inlineImage;
    };
    const now = Date.now() / 1000;
    await bridge.handleEvent({ group: '群A', sender: '李四', kind: 'image', text: '[图片]', id: 'old', imageId: 'good', timestamp: now - 3 });
    if (mode === 'text-target') {
      await bridge.handleEvent({ group: '群A', sender: '张三', kind: 'image', text: '[图片]', id: 'target', imageId: 'bad', timestamp: now - 1 });
      await bridge.handleEvent({ ...stickerEvent, timestamp: now });
    } else {
      // Unknown senders are not cached, but their visual event must remain a target.
      await bridge.handleEvent({ ...stickerEvent, sender: mode === 'excluded' ? '未知' : '张三',
        kind: mode === 'sticker-missing-id' ? 'sticker' : 'image',
        imageId: mode.includes('missing-id') ? undefined : 'bad', timestamp: now });
    }
    await bridge.flush();
    assert.equal(agent.calls.length, 0, mode);
    assert.equal(collector.sent.length, 0, mode);
    assert.equal(bridge.lastReplyTo.size, 0, mode);
    assert.equal(bridge.lastSentByGroup.size, 0, mode);
    assert.ok(!logs.some((s) => s?.type === 'reply'), mode);
    assert.ok(!JSON.stringify(logs).includes('SECRET_BASE64'), mode);
  }
});

test('text mentions use the latest own candidate while skipping older failed images', async () => {
  const { bridge, agent, collector } = makeBridge({ vision: { enabled: true } });
  collector.image = async ({ imageId }) => {
    if (imageId === 'old') throw new Error('SECRET_BASE64');
    return inlineImage;
  };
  const now = Date.now() / 1000;
  for (const [id, age] of [['old', 2], ['new', 1]]) {
    await bridge.handleEvent({ group: '群A', sender: '张三', kind: 'image', text: '[图片]', id, imageId: id, timestamp: now - age });
  }
  await bridge.handleEvent({ ...stickerEvent, timestamp: now });
  await bridge.flush();
  assert.equal(agent.calls.length, 1);
  assert.deepEqual(agent.calls[0].options.images, [inlineImage]);
  assert.match(agent.calls[0].prompt, /共 1 张，按时间先后：第1张由张三发送/);
  assert.match(agent.calls[0].prompt, /部分背景图缺失/);
  assert.equal(collector.sent.length, 1);
});

test('conversation visual follow-ups attach the current image and silently skip missing IDs', async () => {
  const { bridge, agent, collector } = makeBridge({ vision: { enabled: true },
    conversation: { enabled: true, triggerMentions: 2, windowMs: 60000, idleTimeoutMs: 300000, maxTurns: 40, groups: [] } });
  await bridge.handleEvent(stickerEvent);
  await bridge.handleEvent({ ...stickerEvent, id: 'second-mention', text: '@小助手 再聊聊' });
  await bridge.flush();
  await bridge.handleEvent({ group: '群A', sender: '张三', kind: 'image', text: '[图片]', imageId: 'current', id: 'follow' });
  await bridge.flush();
  assert.equal(agent.calls.length, 3);
  assert.equal(agent.calls[2].options.images.length, 1);
  assert.equal(collector.sent[2].at, null);
  const lastReply = bridge.lastReplyTo.get('群A');
  await bridge.handleEvent({ group: '群A', sender: '张三', kind: 'image', text: '[图片]', id: 'missing' });
  await bridge.flush();
  assert.equal(agent.calls.length, 3);
  assert.equal(collector.sent.length, 3);
  assert.equal(bridge.lastReplyTo.get('群A'), lastReply);
});

test('text errors retain the configured error notice', async () => {
  const agent = makeAgent();
  agent.ask = async () => { throw new Error('text failure'); };
  const { bridge, collector } = makeBridge({ agent, behavior: { sendErrorNotice: true } });
  await bridge.handleEvent(stickerEvent);
  await bridge.flush();
  assert.equal(collector.sent.length, 1);
  assert.equal(collector.sent[0].text, '这条消息处理失败了，请稍后再试。');
});

test('invalid control JSON is never sent; model null respects no-image request', async () => {
  for (const reply of ['{"text":"坏', '{"text":"收到","stickerId":null}']) {
    const { bridge, collector } = makeBridge({ stickers: stickerConfig, agent: makeAgent(reply) });
    await bridge.handleEvent({ ...stickerEvent, text: '@小助手 今天开心，不要配图' });
    await bridge.flush();
    assert.ok(collector.sent.every((item) => item.text === '收到' && !item.stickerId));
  }
});

test('prompt tells the model which attached images came from whom', () => {
  const prompt = buildPrompt({
    group: '测试群',
    sender: '乙',
    text: '这梗太老了',
    context: '甲: [图片]',
    memory: '（无）',
    vision: { enabled: true },
    stickers: { enabled: false, items: [] },
    images: [{ sender: '甲', timestamp: 1 }, { sender: '乙', timestamp: 2 }]
  });
  assert.ok(prompt.includes('【本轮附图】共 2 张'));
  assert.ok(prompt.includes('第1张由甲发送'));
  assert.ok(prompt.includes('第2张由乙发送'));
  assert.ok(prompt.includes('梗图'));
  const plain = buildPrompt({ group: 'g', sender: 'a', text: 't', context: '', vision: { enabled: true }, images: [] });
  assert.ok(!plain.includes('【本轮附图】'));
  assert.ok(plain.includes('没有附图时不得声称看见图片'));
});

test('同一人 60 秒内被 @ 三次后，不带 @ 也继续回，且回复不再 @ 他', async () => {
  const collector = makeCollector();
  const agent = makeAgent('好，接着说。');
  const { bridge } = makeBridge({
    conversation: { enabled: true, triggerMentions: 3, windowMs: 60000, idleTimeoutMs: 300000, maxTurns: 40, groups: [] },
    agent, collector
  });
  for (let i = 0; i < 3; i += 1) {
    await bridge.handleEvent({ group: '群A', sender: '张三', text: `@小助手 第${i}次`, id: `m${i}`, seq: i + 1 });
  }
  await bridge.flush();
  assert.equal(agent.calls.length, 3);
  assert.equal(collector.sent.length, 3);
  // 第三次就已经算进入会话模式，回复不再带 @
  assert.equal(collector.sent[2].at, null);

  await bridge.handleEvent({ group: '群A', sender: '张三', text: '那你说说看', id: 'm9', seq: 9 });
  await bridge.flush();
  assert.equal(agent.calls.length, 4);
  assert.match(agent.calls[3].prompt, /连续对话/);
  assert.equal(collector.sent[3].at, null);
  assert.equal(collector.sent[3].text, '好，接着说。');
});

test('没凑满三次、或者换个人不带 @，都不会触发', async () => {
  const agent = makeAgent('不该出现');
  const collector = makeCollector();
  const bridge = makeBridge({
    conversation: { enabled: true, triggerMentions: 3, windowMs: 60000, idleTimeoutMs: 300000, maxTurns: 40, groups: [] },
    agent, collector
  }).bridge;
  await bridge.handleEvent({ group: '群A', sender: '张三', text: '@小助手 一', id: 'a1', seq: 1 });
  await bridge.handleEvent({ group: '群A', sender: '张三', text: '@小助手 二', id: 'a2', seq: 2 });
  await bridge.handleEvent({ group: '群A', sender: '张三', text: '只有两次', id: 'a3', seq: 3 });
  await bridge.flush();
  assert.equal(agent.calls.length, 2);

  // 别人被 @ 三次后开启的是别人的会话，张三不该被顺带接管
  for (let i = 0; i < 3; i += 1) {
    await bridge.handleEvent({ group: '群A', sender: '李四', text: `@小助手 李${i}`, id: `b${i}`, seq: 10 + i });
  }
  await bridge.flush();
  const before = agent.calls.length;
  await bridge.handleEvent({ group: '群A', sender: '张三', text: '我还是不带@', id: 'b9', seq: 20 });
  await bridge.flush();
  assert.equal(agent.calls.length, before);
});

test('conversation 关闭时不生效', async () => {
  const agent = makeAgent('不该出现');
  const bridge = makeBridge({ agent }).bridge;
  for (let i = 0; i < 3; i += 1) {
    await bridge.handleEvent({ group: '群A', sender: '张三', text: `@小助手 ${i}`, id: `c${i}`, seq: i + 1 });
  }
  await bridge.flush();
  const before = agent.calls.length;
  await bridge.handleEvent({ group: '群A', sender: '张三', text: '不带@', id: 'c9', seq: 9 });
  await bridge.flush();
  assert.equal(agent.calls.length, before);
});

test('明确要语音时改发语音，不再发文字', async () => {
  const collector = makeCollector();
  const agent = makeAgent('哼，才不是为了你呢。');
  const bridge = makeBridge({ tts: { enabled: true, keyFile: 'k' }, agent, collector }).bridge;
  await bridge.handleEvent({ group: '群A', sender: '张三', text: '@小助手 用语音说一遍', id: 'v1', seq: 1 });
  await bridge.flush();
  assert.equal(collector.tts.length, 1);
  assert.equal(collector.tts[0].group, '群A');
  assert.equal(collector.tts[0].text, '哼，才不是为了你呢。');
  assert.equal(collector.sent.length, 0, '语音回合不该再发一条文字');
});

test('语音失败时退回文字，不让消息空着', async () => {
  const collector = makeCollector();
  collector.sendTts = async () => { throw new Error('tts down'); };
  const agent = makeAgent('退回去的文字');
  const bridge = makeBridge({ tts: { enabled: true, keyFile: 'k' }, agent, collector }).bridge;
  await bridge.handleEvent({ group: '群A', sender: '张三', text: '@小助手 发条语音', id: 'v2', seq: 1 });
  await bridge.flush();
  assert.equal(collector.tts.length, 0);
  assert.equal(collector.sent.length, 1);
  assert.equal(collector.sent[0].text, '退回去的文字');
});

test('表情包作为附图参与理解，但不单独触发回复', async () => {
  const agent = makeAgent('嗯。');
  const bridge = makeBridge({ vision: { enabled: true, contextWindowMs: 120000, maxImages: 2, maxBytes: 5242880 }, agent }).bridge;
  const stamp = Date.now();
  const imageId = 'a'.repeat(32);
  await bridge.handleEvent({ group: '群A', sender: '张三', text: '动画表情', kind: 'sticker',
    imageId, id: 's1', seq: 1, timestamp: stamp / 1000 });
  await bridge.flush();
  // 没 @ 不触发，但已经进候选池
  assert.equal(agent.calls.length, 0);
  const picked = bridge.images.candidates('群A', '李四', stamp, stamp);
  assert.equal(picked.length, 1);
  assert.equal(picked[0].imageId, imageId);
});

test('刚回过的人发表情，当成对刚才那句话的回应', async () => {
  const VISION = { enabled: true, contextWindowMs: 120000, maxImages: 2, maxBytes: 5242880 };
  const agent = makeAgent('行吧，看在你发图的份上不损你了。');
  const collector = makeCollector();
  const bridge = makeBridge({ vision: VISION, agent, collector,
    behavior: { stickerReplyWindowMs: 60000 } }).bridge;
  const stamp = Date.now();
  await bridge.handleEvent({ group: '群A', sender: '张三', text: '@小助手 在吗', id: 'm1', seq: 1, timestamp: stamp / 1000 });
  await bridge.flush();
  assert.equal(agent.calls.length, 1);
  await bridge.handleEvent({ group: '群A', sender: '张三', text: '动画表情', kind: 'sticker',
    imageId: 'b'.repeat(32), id: 'm2', seq: 2, timestamp: stamp / 1000 });
  await bridge.flush();
  assert.equal(agent.calls.length, 2, '刚回过的人发表情应当接一句');
  assert.match(agent.calls[1].prompt, /只发了个表情/);
});

test('别人发表情、或窗口关掉，都不触发', async () => {
  const VISION = { enabled: true, contextWindowMs: 120000, maxImages: 2, maxBytes: 5242880 };
  const agent = makeAgent('不该出现');
  const bridge = makeBridge({ vision: VISION, agent,
    behavior: { stickerReplyWindowMs: 60000 } }).bridge;
  const stamp = Date.now();
  await bridge.handleEvent({ group: '群A', sender: '张三', text: '@小助手 在吗', id: 'm1', seq: 1, timestamp: stamp / 1000 });
  await bridge.flush();
  await bridge.handleEvent({ group: '群A', sender: '李四', text: '动画表情', kind: 'sticker',
    imageId: 'c'.repeat(32), id: 'm2', seq: 2, timestamp: stamp / 1000 });
  await bridge.flush();
  assert.equal(agent.calls.length, 1);

  const off = makeBridge({ vision: VISION, agent: makeAgent('不该出现'),
    behavior: { stickerReplyWindowMs: 0 } }).bridge;
  await off.handleEvent({ group: '群A', sender: '王五', text: '@小助手 在吗', id: 'n1', seq: 1, timestamp: stamp / 1000 });
  await off.flush();
  await off.handleEvent({ group: '群A', sender: '王五', text: '动画表情', kind: 'sticker',
    imageId: 'd'.repeat(32), id: 'n2', seq: 2, timestamp: stamp / 1000 });
  await off.flush();
  assert.equal(agent.calls.length, 1, '窗口为 0 时表情不该触发');
});
