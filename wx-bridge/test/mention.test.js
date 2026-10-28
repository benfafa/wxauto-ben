import test from 'node:test';
import assert from 'node:assert/strict';
import { findMention, mentionable, normalizeAliases, stripMention } from '../src/mention.js';

test('未知发送者不参与 @', () => {
  assert.equal(mentionable('张三'), '张三');
  assert.equal(mentionable(' 未知 '), null);
  assert.equal(mentionable('Unknown'), null);
  assert.equal(mentionable(''), null);
  assert.equal(mentionable(null), null);
});

test('normalizeAliases adds @, dedupes and sorts longest first', () => {
  assert.deepEqual(normalizeAliases(['小助手', '@小助手', '@机器人小助手']), ['@机器人小助手', '@小助手']);
});

test('findMention matches a configured alias', () => {
  assert.deepEqual(findMention('@小助手 今天几号', ['小助手']), { mentioned: true, alias: '@小助手' });
  assert.deepEqual(findMention('今天几号', ['小助手']), { mentioned: false, alias: null });
});

test('stripMention removes aliases and WeChat at separators', () => {
  assert.equal(stripMention('@小助手\u2005今天几号', ['@小助手']), '今天几号');
  assert.equal(stripMention('@小助手，你好', ['@小助手']), '你好');
  assert.equal(stripMention('@小助手', ['@小助手']), '');
});

test('longest alias wins when aliases overlap', () => {
  const aliases = normalizeAliases(['@小助', '@小助手']);
  assert.equal(findMention('@小助手 在吗', aliases).alias, '@小助手');
  assert.equal(stripMention('@小助手 在吗', aliases), '在吗');
});

test('单字母别名不吃更长的群昵称', () => {
  assert.deepEqual(findMention('@F 在吗', ['@F']), { mentioned: true, alias: '@F' });
  assert.deepEqual(findMention('@F\u2005今晚吃啥', ['@F']), { mentioned: true, alias: '@F' });
  // 微信 @ 人后面接中文是手打的，不是 @ 成员，仍然算触发
  assert.equal(findMention('@F在吗', ['@F']).mentioned, true);
  assert.deepEqual(findMention('@Frank 看下', ['@F']), { mentioned: false, alias: null });
  assert.deepEqual(findMention('@F4 上线了', ['@F']), { mentioned: false, alias: null });
  assert.equal(stripMention('@Frank 看下', ['@F']), '@Frank 看下');
  assert.equal(stripMention('喊了 @F 和 @F 两次', ['@F']), '喊了 和 两次');
});
