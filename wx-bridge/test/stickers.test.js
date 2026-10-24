import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { normalizeStickers, parseStickerReply, refreshStickerIndex, shortlistStickers, stickerProtocol } from '../src/stickers.js';

const stickers = { enabled: true, cooldownMs: 60000, items: [
  { id: 'happy', keywords: ['开心'], description: '庆祝、祝贺' }, { id: 'sad', keywords: ['难过'] }
] };

// These IDs are also asserted by collector/tests/test_stickers.py, which proves
// both implementations hash the same relative filename the same way.
const AUTO_FILES = ['a.b.PNG', 'happy day.jpg', 'sad-life.GIF', '开心_庆祝.png'];
const AUTO_IDS = {
  'a.b.PNG': 'auto-8c0d8630c877',
  'happy day.jpg': 'auto-f4f24ff4a3a1',
  'sad-life.GIF': 'auto-8697d6432e49',
  '开心_庆祝.png': 'auto-08b754933842'
};
const AUTO_KEYWORDS = {
  'a.b.PNG': ['a', 'b'],
  'happy day.jpg': ['happy', 'day'],
  'sad-life.GIF': ['sad', 'life'],
  '开心_庆祝.png': ['开心', '庆祝']
};

function makeStickerDir(files = AUTO_FILES) {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'wx-bridge-stickers-'));
  const directory = path.join(base, 'stickers');
  fs.mkdirSync(directory);
  for (const name of files) fs.writeFileSync(path.join(directory, name), 'fixture');
  return { base, directory };
}

test('model selects one allowlisted sticker in ordinary conversation without request regex', () => {
  assert.deepEqual(parseStickerReply('{"text":"太棒了","stickerId":"happy"}', stickers), { text: '太棒了', stickerId: 'happy' });
  assert.match(stickerProtocol(stickers), /普通聊天/);
  assert.match(stickerProtocol(stickers), /庆祝、祝贺/);
  assert.match(stickerProtocol(stickers), /不配图.*null/);
  assert.deepEqual(parseStickerReply('{"text":"收到，不配图","stickerId":null}', stickers), { text: '收到，不配图', stickerId: null });
});

test('disabled and empty catalogs omit protocol and cannot select images', () => {
  for (const cfg of [undefined, { ...stickers, enabled: false }, { ...stickers, items: [] }]) {
    assert.equal(stickerProtocol(cfg), '');
    assert.equal(parseStickerReply('{"text":"收到","stickerId":"happy"}', cfg).stickerId, null);
    assert.equal(parseStickerReply('文字回归', cfg).text, '文字回归');
  }
});

test('unknown or multiple IDs never send; malformed and malicious JSON never leaks', () => {
  for (const stickerId of ['unknown', '../happy.png', 'https://example.com/a.png', ['happy', 'sad'], {}, 1]) {
    const result = parseStickerReply(JSON.stringify({ text: '安全文字', stickerId }), stickers);
    assert.deepEqual(result, { text: '安全文字', stickerId: null });
  }
  for (const raw of ['{"text":"坏', '{"text":"好","stickerId":"happy", "file":"evil"}',
    '```json\n{"text":"好","stickerId":"happy"}\n```', '前缀 {"stickerId":"happy"}',
    '[{"text":"好","stickerId":"happy"}]', '{"text":{"stickerId":"happy"},"stickerId":"happy"}',
    '{"text":"{\\"stickerId\\":\\"happy\\"}","stickerId":"happy"}',
    '{"text":"好","stickerId":"happy","__proto__":{}}']) {
    assert.deepEqual(parseStickerReply(raw, stickers), { text: '', stickerId: null }, raw);
  }
  assert.deepEqual(parseStickerReply('普通安全文字', stickers), { text: '普通安全文字', stickerId: null });
});

test('autoLoad scans one level, sorts by filename and derives stable IDs/keywords', () => {
  const { base, directory } = makeStickerDir();
  try {
    fs.mkdirSync(path.join(directory, 'nested.png'));
    fs.writeFileSync(path.join(directory, 'notes.txt'), 'not an image');
    try {
      fs.symlinkSync(path.join(directory, 'a.b.PNG'), path.join(directory, 'link.png'));
    } catch (error) {
      if (error.code !== 'EPERM') throw error;
    }
    const cfg = normalizeStickers({ enabled: true, directory: 'stickers' }, base);
    assert.equal(cfg.autoLoad, true);
    assert.deepEqual(cfg.items.map((item) => item.file), AUTO_FILES);
    assert.deepEqual(cfg.items.map((item) => item.id), AUTO_FILES.map((name) => AUTO_IDS[name]));
    for (const item of cfg.items) {
      assert.deepEqual(item.keywords, AUTO_KEYWORDS[item.file]);
      assert.equal(item.description, path.basename(item.file, path.extname(item.file)));
    }
    assert.deepEqual(cfg.items[0], { id: AUTO_IDS['a.b.PNG'], file: 'a.b.PNG',
      keywords: ['a', 'b'], description: 'a.b' });
    const again = normalizeStickers({ enabled: true, directory: 'stickers' }, base);
    assert.deepEqual(again.items, cfg.items);
  } finally {
    fs.rmSync(base, { recursive: true, force: true });
  }
});

test('autoLoad keyword splitting dedupes, truncates and caps at ten parts', () => {
  const long = 'x'.repeat(60);
  const name = `one_one_${long}_three_four_five_six_seven_eight_nine_ten_eleven.png`;
  const { base } = makeStickerDir([name]);
  try {
    const [item] = normalizeStickers({ enabled: true, directory: 'stickers' }, base).items;
    assert.deepEqual(item.keywords, ['one', 'x'.repeat(50), 'three', 'four', 'five',
      'six', 'seven', 'eight', 'nine', 'ten']);
    assert.equal(item.description, name.slice(0, -4));
  } finally {
    fs.rmSync(base, { recursive: true, force: true });
  }
});

test('explicit items win over autoLoad; autoLoad=false or a missing directory stays empty', () => {
  const { base } = makeStickerDir();
  try {
    const item = { id: 'custom', file: 'a.b.PNG', keywords: ['自定义'] };
    for (const autoLoad of [true, false]) {
      const cfg = normalizeStickers({ enabled: true, directory: 'stickers', autoLoad, items: [item] }, base);
      assert.deepEqual(cfg.items.map(({ id }) => id), ['custom']);
    }
    assert.deepEqual(normalizeStickers(
      { enabled: true, directory: 'stickers', autoLoad: false }, base).items, []);
    assert.deepEqual(normalizeStickers(
      { enabled: true, directory: 'missing' }, base).items, []);
    assert.deepEqual(normalizeStickers({ enabled: false, directory: 'stickers' }, base).items, []);
    assert.throws(() => normalizeStickers({ autoLoad: 'true' }), /autoLoad/);
  } finally {
    fs.rmSync(base, { recursive: true, force: true });
  }
});

test('autoLoad keeps at most 200 images after sorting', () => {
  const names = Array.from({ length: 205 }, (_, index) => `img-${String(index).padStart(3, '0')}.png`);
  const { base } = makeStickerDir(names);
  try {
    const { items } = normalizeStickers({ enabled: true, directory: 'stickers' }, base);
    assert.equal(items.length, 200);
    assert.equal(items[0].file, 'img-000.png');
    assert.equal(items[199].file, 'img-199.png');
  } finally {
    fs.rmSync(base, { recursive: true, force: true });
  }
});

// The IDs below are asserted again in collector/tests/test_stickers.py, which is
// what keeps the prompt side and the sending side on the same library.
const INDEX_FILES = ['001.gif', '002.png', '003.png', '004.gif', '005.png', '006.png'];
const sha = (value, length) => createHash('sha1').update(value, 'utf8').digest('hex').slice(0, length);
const INDEX_BODY = {
  '001.gif': { file: '001.gif', safe: true, caption: '熊猫头端碗坏笑，边吃边看热闹', keywords: ['熊猫头', '看热闹'], mood: '狡黠' },
  '002.png': { file: '002.png', safe: true, caption: '流泪猫猫头，委屈到不行', keywords: ['委屈', '哭'], mood: '难过' },
  '003.png': { file: '003.png', safe: false, caption: '涉政梗图', keywords: ['敏感'], mood: '无语' },
  '004.gif': { file: '004.gif', safe: true, blocked: true, caption: '服务商审核拒绝', keywords: ['待人工确认'], mood: '不可用' },
  '005.png': { file: '005.png', safe: true, caption: '   ', keywords: ['空描述'], mood: '中性' },
  '006.png': { file: '006.png', caption: '没有 safe 字段', keywords: ['缺字段'] },
  '007.png': { file: 'evil/../007.png', safe: true, caption: '越界路径', keywords: ['越界'] },
  'ghost.png': { file: 'ghost.png', safe: true, caption: '索引里有但磁盘上没有', keywords: ['缺失'] },
  '002.gif': { file: '002.gif', safe: true, caption: '与 002.png 撞名的动图', keywords: ['撞名'] }
};

function makeIndexedStickers(files = INDEX_FILES, body = INDEX_BODY) {
  const { base, directory } = makeStickerDir(files);
  fs.writeFileSync(path.join(base, 'sticker-index.json'), JSON.stringify(body), 'utf8');
  return { base, directory, indexFile: path.join(base, 'sticker-index.json') };
}

test('indexFile ids come from stems, collide safely, and skip unsafe or missing entries', () => {
  const { base, indexFile } = makeIndexedStickers();
  try {
    const cfg = normalizeStickers({ enabled: true, directory: 'stickers', indexFile: 'sticker-index.json' }, base);
    // Same stem twice => both get a sha suffix; the one missing on disk never steals the bare id.
    assert.deepEqual(cfg.items.map(({ id, file }) => [id, file]),
      [['001', '001.gif'], [`002-${sha('002.png', 6)}`, '002.png']]);
    assert.equal(cfg.indexFile, indexFile);
    assert.deepEqual(cfg.items[0].keywords, ['熊猫头', '看热闹', '狡黠']);
    // Explicit items keep priority and never renumber the index.
    const withCustom = normalizeStickers({
      enabled: true, directory: 'stickers', indexFile: 'sticker-index.json',
      items: [{ id: '001', file: '001.gif', keywords: ['自定义'] }]
    }, base);
    assert.deepEqual(withCustom.items.map(({ id }) => id), ['001', `002-${sha('002.png', 6)}`]);
    assert.deepEqual(normalizeStickers(
      { enabled: false, directory: 'stickers', indexFile: 'sticker-index.json' }, base).items, []);
    // A missing index is not an error: autoLoad still covers the dropped folder.
    assert.equal(normalizeStickers({ enabled: true, directory: 'stickers', indexFile: 'nope.json', autoLoad: false }, base).items.length, 0);
    assert.equal(normalizeStickers({ enabled: true, directory: 'stickers', indexFile: 'nope.json' }, base).items.length, INDEX_FILES.length);
    assert.throws(() => normalizeStickers({ indexFile: 7 }, base), /indexFile/);
  } finally {
    fs.rmSync(base, { recursive: true, force: true });
  }
});

test('a large indexed library is shortlisted instead of pasted whole', () => {
  const files = Array.from({ length: 60 }, (_, i) => `${String(i).padStart(3, '0')}.png`);
  const body = Object.fromEntries(files.map((name, i) => [name, {
    file: name, safe: true,
    caption: i % 2 ? '夸人帅气，墨镜红字' : '疲惫打工人的早安',
    keywords: i % 2 ? ['帅', '自夸'] : ['打工', '早安'],
    mood: i % 2 ? '得意' : '疲惫'
  }]));
  const { base } = makeIndexedStickers(files, body);
  try {
    const cfg = normalizeStickers({ enabled: true, directory: 'stickers', indexFile: 'sticker-index.json' }, base);
    assert.equal(cfg.items.length, 60);
    const picked = shortlistStickers(cfg, { text: '你也太帅了吧', context: '', limit: 8 });
    assert.equal(picked.length, 8);
    assert.ok(picked.every((item) => item.keywords.some((word) => /帅|自夸/.test(word))));
    const prompt = stickerProtocol(cfg, picked);
    assert.match(prompt, /候选素材/);
    assert.doesNotMatch(prompt, /早安/);
    // Small libraries are still offered in full.
    const small = { enabled: true, items: cfg.items.slice(0, 4) };
    assert.equal(shortlistStickers(small, { text: '随便', limit: 24 }).length, 4);
    // No literal match still returns a stable, bounded rotation.
    const a = shortlistStickers(cfg, { text: '完全无关的火星话', limit: 6 }).map(({ id }) => id).join();
    const b = shortlistStickers(cfg, { text: '完全无关的火星话', limit: 6 }).map(({ id }) => id).join();
    assert.equal(a, b);
    assert.equal(a.split(',').length, 6);
  } finally {
    fs.rmSync(base, { recursive: true, force: true });
  }
});

test('index hot-reloads when the label file changes and rejects a broken rewrite', () => {
  const clean = {
    '001.gif': { file: '001.gif', safe: true, caption: '熊猫头端碗坏笑', keywords: ['看热闹'] },
    '002.png': { file: '002.png', safe: true, caption: '流泪猫猫头', keywords: ['委屈'] }
  };
  const { base } = makeIndexedStickers(['001.gif', '002.png'], clean);
  try {
    const cfg = normalizeStickers({ enabled: true, directory: 'stickers', indexFile: 'sticker-index.json' }, base);
    assert.deepEqual(cfg.items.map(({ id }) => id), ['001', '002']);
    assert.equal(refreshStickerIndex(cfg), false);
    const indexFile = path.join(base, 'sticker-index.json');
    fs.writeFileSync(path.join(base, 'stickers', '003.png'), 'fixture');
    fs.writeFileSync(indexFile, JSON.stringify({ ...clean,
      '003.png': { file: '003.png', safe: true, caption: '新打的标签', keywords: ['新增'] } }), 'utf8');
    const future = new Date(Date.now() + 5000);
    fs.utimesSync(indexFile, future, future);
    assert.equal(refreshStickerIndex(cfg), true);
    assert.deepEqual(cfg.items.map(({ id }) => id), ['001', '002', '003']);
    assert.equal(parseStickerReply('{"text":"嗨","stickerId":"003"}', cfg).stickerId, '003');
    fs.writeFileSync(indexFile, '{broken', 'utf8');
    const later = new Date(Date.now() + 9000);
    fs.utimesSync(indexFile, later, later);
    assert.throws(() => refreshStickerIndex(cfg), /合法 JSON/);
  } finally {
    fs.rmSync(base, { recursive: true, force: true });
  }
});

test('shortlistLimit bounds the prompt and long captions are trimmed for the model', () => {
  const files = Array.from({ length: 40 }, (_, i) => `${String(i).padStart(2, '0')}.png`);
  const long = '很'.repeat(300);
  const body = Object.fromEntries(files.map((name, i) => [name, {
    file: name, safe: true, caption: i === 0 ? long : '哈哈一笑', keywords: ['哈哈']
  }]));
  const { base } = makeIndexedStickers(files, body);
  try {
    const cfg = normalizeStickers({ enabled: true, directory: 'stickers', indexFile: 'sticker-index.json', shortlistLimit: 5 }, base);
    assert.equal(cfg.shortlistLimit, 5);
    assert.equal(shortlistStickers(cfg, { text: '哈哈' }).length, 5);
    const all = shortlistStickers({ ...cfg, items: cfg.items }, { text: '', context: '', limit: 40 });
    assert.ok(all.every((item) => item.description.length <= 90));
    assert.equal(cfg.items.find((item) => item.file === '00.png').description.length, 300);
    for (const bad of [0, 81, '20', 1.5]) {
      assert.throws(() => normalizeStickers({ enabled: true, shortlistLimit: bad }, base), /shortlistLimit/);
    }
  } finally {
    fs.rmSync(base, { recursive: true, force: true });
  }
});

test('主库与收件箱打标库合并，任一份变动都会热更新', () => {
  const files = ['001.gif', '002.png', '003.png'];
  const { base } = makeStickerDir(files);
  const main = { '001.gif': { file: '001.gif', safe: true, caption: '熊猫头端碗', keywords: ['熊猫头'] } };
  const extra = { '002.png': { file: '002.png', safe: true, caption: '猫猫坏笑', keywords: ['猫'] } };
  fs.writeFileSync(path.join(base, 'sticker-index.json'), JSON.stringify(main));
  fs.writeFileSync(path.join(base, 'sticker-index-extra.json'), JSON.stringify(extra));
  try {
    const cfg = normalizeStickers({
      enabled: true, directory: 'stickers',
      indexFile: 'sticker-index.json', extraIndexFile: 'sticker-index-extra.json'
    }, base);
    assert.deepEqual(cfg.items.map(({ id }) => id), ['001', '002']);
    assert.equal(refreshStickerIndex(cfg), false);
    const extraFile = path.join(base, 'sticker-index-extra.json');
    fs.writeFileSync(extraFile, JSON.stringify({ ...extra,
      '003.png': { file: '003.png', safe: true, caption: '新收的表情', keywords: ['新'] } }));
    const future = new Date(Date.now() + 5000);
    fs.utimesSync(extraFile, future, future);
    assert.equal(refreshStickerIndex(cfg), true);
    assert.deepEqual(cfg.items.map(({ id }) => id), ['001', '002', '003']);
  } finally {
    fs.rmSync(base, { recursive: true, force: true });
  }
});
