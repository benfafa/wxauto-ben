import test from 'node:test';
import assert from 'node:assert/strict';
import { GroupContext } from '../src/context.js';

test('keeps a rolling window and excludes the current message', () => {
  const ctx = new GroupContext({ maxMessages: 3, maxChars: 1000 });
  ctx.add({ id: '1', group: '群A', sender: '甲', text: '一' });
  ctx.add({ id: '2', group: '群A', sender: '乙', text: '二' });
  ctx.add({ id: '3', group: '群A', sender: '丙', text: '三' });
  ctx.add({ id: '4', group: '群A', sender: '丁', text: '四' });

  const text = ctx.formatPrevious('群A', '4');
  assert.equal(text, '乙：二\n丙：三');
});

test('marks messages that mentioned the bot and caps total chars', () => {
  const ctx = new GroupContext({ maxMessages: 10, maxChars: 20 });
  ctx.add({ id: '1', group: '群A', sender: '甲', text: '很早以前的消息' });
  ctx.add({ id: '2', group: '群A', sender: '乙', text: '稍近一点的消息', atMe: true });
  ctx.add({ id: '3', group: '群A', sender: '丙', text: '最新一条要回的消息' });

  const text = ctx.formatPrevious('群A', '3');
  assert.match(text, /乙：稍近一点的消息（@了你）/);
  assert.doesNotMatch(text, /很早以前的消息/);
  assert.ok(text.length <= 40, `context should stay near the cap, got ${text.length}`);
  assert.match(text, /更早的消息已省略/);
});

test('returns a placeholder when there is no context', () => {
  const ctx = new GroupContext();
  assert.equal(ctx.formatPrevious('群A', 'x'), '（无）');
});
