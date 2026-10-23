import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeTts, wantsVoice } from '../src/tts.js';

test('tts 配置默认值与边界', () => {
  const cfg = normalizeTts(undefined);
  assert.equal(cfg.enabled, false);
  assert.equal(cfg.provider, 'minimax');
  assert.equal(cfg.maxChars, 300);
  for (const bad of [{ provider: 'azure' }, { maxChars: 0 }, { maxBytes: 10 }, { timeoutMs: 100 },
    { speed: 3 }, { pitch: 13 }, { enabled: 'yes' }, { typo: 1 }, null, []]) {
    assert.throws(() => normalizeTts(bad), /tts/, JSON.stringify(bad));
  }
  assert.throws(() => normalizeTts({ enabled: true, provider: 'minimax' }), /keyFile/);
  assert.equal(normalizeTts({ enabled: true, keyFile: 'k', provider: 'edge' }).provider, 'edge');
});

test('只有明确要语音才触发，否定说法除外', () => {
  for (const text of ['用语音说一遍', '发条语音', '来一条语音', '发个语音', '念给我听', '说给我听', '语音回我']) {
    assert.equal(wantsVoice(text), true, text);
  }
  for (const text of ['别发语音', '不用语音了', '不要用声音', '语音识别怎么开', '', '   ', null]) {
    assert.equal(wantsVoice(text), false, String(text));
  }
});
