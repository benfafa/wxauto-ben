import test from 'node:test';
import assert from 'node:assert/strict';
import { ConversationTracker, normalizeConversation } from '../src/conversation.js';

const ON = { enabled: true, triggerMentions: 3, windowMs: 60000, idleTimeoutMs: 300000, maxTurns: 40, groups: [] };

test('默认关闭，字段写错就拒绝', () => {
  const cfg = normalizeConversation(undefined);
  assert.equal(cfg.enabled, false);
  assert.equal(cfg.triggerMentions, 3);
  assert.equal(cfg.windowMs, 60000);
  for (const bad of [{ triggerMentions: 1 }, { triggerMentions: 11 }, { windowMs: 1000 },
    { idleTimeoutMs: 1000 }, { maxTurns: 0 }, { enabled: 'true' }, { groups: [1] }, { groups: [''] },
    { typo: 1 }, null, []]) {
    assert.throws(() => normalizeConversation(bad), /conversation/, JSON.stringify(bad));
  }
  assert.deepEqual(normalizeConversation({ groups: ['群A', '群A', ' 群B '] }).groups, ['群A', '群B']);
});

test('60 秒内被同一个人 @ 三次才开启，并且按人绑定', () => {
  const tracker = new ConversationTracker(ON);
  assert.equal(tracker.mention('群A', '张三', 1000).active, false);
  assert.equal(tracker.mention('群A', '张三', 20000).active, false);
  const third = tracker.mention('群A', '张三', 59000);
  assert.deepEqual(third, { active: true, opened: true });
  // 换个人不算
  assert.equal(tracker.mention('群A', '李四', 60000).active, false);
  assert.deepEqual(tracker.status('群A', 60000), { user: '张三', turns: 0 });
});

test('@ 太分散就不算，窗口会滑动', () => {
  const tracker = new ConversationTracker(ON);
  tracker.mention('群A', '张三', 0);
  tracker.mention('群A', '张三', 40000);
  assert.equal(tracker.mention('群A', '张三', 110000).active, false);
});

test('进入会话后，只有主角不带 @ 的消息能续上', () => {
  const tracker = new ConversationTracker(ON);
  tracker.mention('群A', '张三', 0);
  tracker.mention('群A', '张三', 1000);
  tracker.mention('群A', '张三', 2000);
  assert.equal(tracker.follow('群A', '张三', 3000), true);
  assert.equal(tracker.follow('群A', '李四', 3000), false);
  assert.equal(tracker.follow('群B', '张三', 3000), false);
});

test('闲置超时和回合上限都会退出会话', () => {
  const idle = new ConversationTracker({ ...ON, triggerMentions: 2, idleTimeoutMs: 10000 });
  idle.mention('群A', '张三', 0);
  idle.mention('群A', '张三', 100);
  assert.equal(idle.follow('群A', '张三', 5000), true);   // 续命到 15000
  assert.equal(idle.follow('群A', '张三', 14999), true);  // 又续到 24999
  assert.equal(idle.follow('群A', '张三', 40000), false); // 早过期了

  const capped = new ConversationTracker({ ...ON, triggerMentions: 2, maxTurns: 2 });
  capped.mention('群A', '张三', 0);
  capped.mention('群A', '张三', 100);
  assert.equal(capped.countTurn('群A', 200).ok, true);
  const last = capped.countTurn('群A', 300);
  assert.equal(last.ok, false);
  assert.equal(last.reason, 'max-turns');
  assert.equal(capped.status('群A', 400), null);
});

test('groups 限定只在这些群里生效', () => {
  const tracker = new ConversationTracker({ ...ON, triggerMentions: 2, groups: ['群A'] });
  tracker.mention('群B', '张三', 0);
  assert.equal(tracker.mention('群B', '张三', 100).active, false);
  tracker.mention('群A', '张三', 0);
  assert.equal(tracker.mention('群A', '张三', 100).active, true);
});
