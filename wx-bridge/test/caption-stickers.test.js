import test from 'node:test';
import assert from 'node:assert/strict';
import { parseLabel, sanitizeError } from '../scripts/caption-stickers.mjs';

test('accepts a well-formed label object', () => {
  const label = parseLabel('{"caption":"柴犬歪头笑，配文就这？","keywords":["开心","嘲讽","柴犬"],"mood":"得意","safe":true}');
  assert.deepEqual(label, { caption: '柴犬歪头笑，配文就这？', keywords: ['开心', '嘲讽', '柴犬'], mood: '得意', safe: true });
});

test('tolerates code fences, whitespace and duplicate keywords', () => {
  const label = parseLabel('```json\n {"caption":"猫摊在桌上","keywords":["累"," 累 ","摸鱼"],"mood":"疲惫","safe":true}\n```');
  assert.deepEqual(label.keywords, ['累', '摸鱼']);
});

test('rejects anything that is not a complete label object', () => {
  assert.equal(parseLabel('这张图很好笑'), null);
  assert.equal(parseLabel('{"caption":"缺字段"}'), null);
  assert.equal(parseLabel('{"caption":"c","keywords":[],"mood":"m","safe":true}'), null);
  assert.equal(parseLabel('{"caption":"c","keywords":["k"],"mood":"m","safe":"yes"}'), null);
  assert.deepEqual(parseLabel('{"caption":"c","keywords":["k"],"mood":"m","safe":true,"path":"C:\\\\windows\\\\system32"}'),
    { caption: 'c', keywords: ['k'], mood: 'm', safe: true });
  assert.equal(parseLabel('{"caption":"' + '长'.repeat(121) + '","keywords":["k"],"mood":"m","safe":true}'), null);
});

test('error summaries never carry inline image data', () => {
  const long = 'A'.repeat(500);
  const text = sanitizeError(new Error(`prompt ${long} data:image/png;base64,${long} tail`));
  assert.ok(text.length <= 160);
  assert.ok(!text.includes(long));
  assert.ok(text.includes('[内容已省略]'));
  assert.equal(sanitizeError(null), '未知错误');
});
