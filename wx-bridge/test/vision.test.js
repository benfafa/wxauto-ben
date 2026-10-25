import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeVision, validateImages, RecentImages } from '../src/vision.js';

const image = { mediaType: 'image/png', data: 'iVBORw0KGgo=', name: 'wx.png' };

test('vision strict configuration defaults and boundaries', () => {
  assert.deepEqual(normalizeVision(), { enabled: false, contextWindowMs: 120000, maxImages: 2, maxBytes: 5242880 });
  for (const value of [null, [], false, { enabled: 1 }, { maxImages: '2' }, { maxImages: true },
    { maxImages: 0 }, { maxImages: 5 }, { maxImages: 1.5 }, { maxBytes: 1023 }, { maxBytes: 5242881 },
    { contextWindowMs: 999 }, { contextWindowMs: 600001 }, { url: 'http://example.com' }]) {
    assert.throws(() => normalizeVision(value), /vision/);
  }
  assert.equal(normalizeVision({ enabled: true, contextWindowMs: 1000, maxBytes: 1024, maxImages: 4 }).maxImages, 4);
});

test('inline images require opt-in, a bounded canonical base64 schema and matching MIME magic', () => {
  assert.deepEqual(validateImages([image], { enabled: true }), [{ type: 'image', ...image }]);
  assert.deepEqual(validateImages(), []);
  assert.throws(() => validateImages([image]), /vision/);
  for (const value of [null, [image, image, image], [{ ...image, url: 'http://example.com' }],
    [{ ...image, data: 'data:image/png;base64,iVBORw0KGgo=' }], [{ ...image, data: 'a===' }],
    [{ ...image, data: '' }], [{ ...image, data: 'AB==' }], [{ ...image, name: '../wx.png' }],
    [{ ...image, mediaType: 'image/jpeg' }], [{ ...image, data: Buffer.alloc(1025).toString('base64') }]]) {
    assert.throws(() => validateImages(value, { enabled: true, maxBytes: 1024 }), /vision/);
  }
});

test('maximum-size canonical image data is accepted without regex stack overflow', () => {
  const data = Buffer.alloc(5242880);
  Buffer.from('89504e470d0a1a0a', 'hex').copy(data);
  assert.equal(validateImages([{ ...image, data: data.toString('base64') }], { enabled: true }).length, 1);
});

test('metadata cache bounds, expires, excludes future/cross-group and applies maxImages', () => {
  const cache = new RecentImages({ enabled: true, contextWindowMs: 1000, maxImages: 2 });
  for (let i = 0; i < 510; i++) cache.add({ kind: 'image', group: 'A', sender: 'a', imageId: `${i}`, timestamp: 10 }, 10000);
  assert.equal(cache.events.length, 500);
  assert.deepEqual(cache.candidates('A', 'a', 10000, 10000).map((e) => e.imageId), ['508', '509']);
  assert.deepEqual(cache.candidates('B', 'a', 10000, 10000), []);
  assert.deepEqual(cache.candidates('A', 'a', 9000, 10000), []);
  assert.deepEqual(cache.candidates('A', 'a', 11001, 11001), []);
  cache.add({ kind: 'text' }, 11001);
  assert.equal(cache.events.length, 0);
  cache.clear();
});

test('event identities survive missing image IDs and candidate snapshots stay isolated', () => {
  const cache = new RecentImages({ enabled: true });
  const event = { kind: 'image', group: 'A', sender: 'a', timestamp: 10 };
  const first = cache.add(event, 10000);
  const second = cache.add(event, 10000);
  assert.notEqual(first, second);
  const snapshot = cache.candidates('A', 'a', 10000, 10000);
  assert.deepEqual(snapshot.map((item) => item.seq), [first, second]);
  assert.deepEqual(snapshot.map((item) => item.imageId), [null, null]);
  cache.add({ ...event, group: 'B', imageId: 'other' }, 10000);
  cache.add({ ...event, imageId: 'new' }, 10000);
  assert.deepEqual(snapshot.map((item) => item.seq), [first, second]);
  assert.deepEqual(cache.candidates('A', 'a', 10000, 10000).map((item) => item.imageId), [null, 'new']);
  cache.clear();
  assert.notEqual(cache.add(event, 10000), first);
});

// 梗图常见形态是「甲发图、乙 @ 机器人接梗」，所以别人的图也要能带上，但自己的优先。
test('carries other members images and prefers the trigger sender, oldest first', () => {
  const cache = new RecentImages({ enabled: true, contextWindowMs: 10000, maxImages: 2 });
  cache.add({ kind: 'image', group: 'A', sender: '甲', imageId: 'a1', timestamp: 1 }, 1000);
  cache.add({ kind: 'image', group: 'A', sender: '甲', imageId: 'a2', timestamp: 2 }, 2000);
  cache.add({ kind: 'image', group: 'A', sender: '乙', imageId: 'b1', timestamp: 3 }, 3000);
  assert.deepEqual(cache.candidates('A', '乙', 3000, 3000).map((e) => e.imageId), ['a2', 'b1']);
  assert.deepEqual(cache.candidates('A', '甲', 3000, 3000).map((e) => e.imageId), ['a1', 'a2']);
  assert.deepEqual(cache.candidates('A', '丙', 3000, 3000).map((e) => e.imageId), ['a2', 'b1']);
});
