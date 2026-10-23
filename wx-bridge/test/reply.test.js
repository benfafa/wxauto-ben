import test from 'node:test';
import assert from 'node:assert/strict';
import { mdToPlain, sanitizeOutgoing, splitMessage } from '../src/reply.js';

test('mdToPlain strips common Markdown noise', () => {
  assert.equal(mdToPlain('**你好**，`世界`'), '你好，世界');
  assert.equal(mdToPlain('# 标题\n- 一\n- 二'), '标题\n• 一\n• 二');
  assert.equal(mdToPlain('[链接](https://example.com)'), '链接 (https://example.com)');
  assert.equal(mdToPlain('```js\nconsole.log(1)\n```'), 'console.log(1)');
});

test('sanitizeOutgoing neutralizes broadcast mentions and control chars', () => {
  assert.equal(sanitizeOutgoing('**注意** @所有人\u0000'), '注意 ＠所有人');
  assert.equal(sanitizeOutgoing('@all 开会'), '＠all 开会');
  assert.equal(sanitizeOutgoing('   '), '');
});

test('splitMessage prefers newlines and respects the max length', () => {
  const text = `${'a'.repeat(8)}\n${'b'.repeat(8)}\n${'c'.repeat(8)}`;
  const parts = splitMessage(text, 10);
  assert.equal(parts.join(''), text.replace(/\n/g, ''));
  assert.ok(parts.every((part) => part.length <= 10), JSON.stringify(parts));
  assert.deepEqual(parts, ['aaaaaaaa', 'bbbbbbbb', 'cccccccc']);
});

test('splitMessage never cuts a surrogate pair', () => {
  const text = '😀😀😀😀😀';
  for (const max of [1, 2, 3]) {
    const parts = splitMessage(text, max);
    assert.equal(parts.join(''), text);
    for (const part of parts) {
      assert.ok(!/^[\uDC00-\uDFFF]/.test(part), `orphan low surrogate in ${JSON.stringify(part)}`);
      assert.ok(!/[\uD800-\uDBFF]$/.test(part), `orphan high surrogate in ${JSON.stringify(part)}`);
    }
  }
});

test('splitMessage hard-cuts when there is no natural boundary', () => {
  const parts = splitMessage('a'.repeat(10), 3);
  assert.deepEqual(parts, ['aaa', 'aaa', 'aaa', 'a']);
});
