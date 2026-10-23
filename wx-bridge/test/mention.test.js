import test from 'node:test';
import assert from 'node:assert/strict';
import { findMention, normalizeAliases, stripMention } from '../src/mention.js';

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
