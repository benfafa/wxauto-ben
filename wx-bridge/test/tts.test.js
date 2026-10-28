import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeTts, wantsVoice } from '../src/tts.js';

test('tts 配置默认值与边界', () => {
  const cfg = normalizeTts(undefined);
  assert.equal(cfg.enabled, false);
  assert.equal(cfg.provider, 'minimax');
  assert.equal(cfg.model, 'speech-2.8-hd');
  assert.equal(cfg.voice, 'Chinese (Mandarin)_BashfulGirl');
  assert.equal(cfg.emotion, 'happy');
  assert.equal(cfg.vol, 5.0);
  assert.equal(cfg.maxChars, 300);
  for (const bad of [{ provider: 'azure' }, { maxChars: 0 }, { maxBytes: 10 }, { timeoutMs: 100 },
    { speed: 3 }, { pitch: 13 }, { vol: 0 }, { emotion: 'bored' }, { edgeVoice: 1 },
    { enabled: 'yes' }, { typo: 1 }, null, []]) {
    assert.throws(() => normalizeTts(bad), /tts/, JSON.stringify(bad));
  }
  assert.throws(() => normalizeTts({ enabled: true, provider: 'minimax' }), /keyFile/);
  assert.equal(normalizeTts({ enabled: true, keyFile: 'k', provider: 'edge' }).provider, 'edge');
});

test('只有明确要语音才触发，否定说法除外', () => {
  for (const text of ['用语音说一遍', '发条语音', '来一条语音', '发个语音', '念给我听', '说给我听', '语音回我',
    '给俺唱首歌', '唱首歌', '唱一首歌', '唱给我听', '来首歌', '唱首庐州月吧', '来一首', '来两首',
    '唱两句', '唱一段', '唱一曲', '唱两首']) {
    assert.equal(wantsVoice(text), true, text);
  }
  for (const text of ['别发语音', '不用语音了', '不要用声音', '语音识别怎么开', '别唱歌', '不要唱了',
    '唱反调', '合唱团真吵', '', '   ', null]) {
    assert.equal(wantsVoice(text), false, String(text));
  }
});
