/**
 * 语音回复的开关与触发词。
 *
 * 只在用户明确要「语音」时合成：群里默认发文字，语音是额外动作，成本更高、
 * 也更吵。合成放在采集端做（见 collector/tts.py），这里只校验配置和判断意图。
 */
const DEFAULTS = {
  enabled: false,
  provider: 'minimax',
  keyFile: '',
  model: 'speech-02-hd',
  voice: 'qiaopi_mengmei',
  speed: 1.05,
  pitch: 2,
  maxChars: 300,
  maxBytes: 5 * 1024 * 1024,
  timeoutMs: 60000,
  // 发微信原生语音气泡（不是 mp3 文件）：合成在采集端，这里只校验开关。
  nativeVoice: false
};

function requireInt(value, label, min, max) {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < min || value > max) {
    throw new Error(`${label} 必须是 ${min}~${max} 的整数`);
  }
  return value;
}

export function normalizeTts(input) {
  if (input === undefined) return { ...DEFAULTS };
  if (input === null || typeof input !== 'object' || Array.isArray(input)) {
    throw new Error('tts 必须是对象');
  }
  const unknown = Object.keys(input).filter((key) => !Object.hasOwn(DEFAULTS, key));
  if (unknown.length) throw new Error(`tts 含未知字段：${unknown.join(', ')}`);
  const cfg = { ...DEFAULTS, ...input };
  if (typeof cfg.enabled !== 'boolean') throw new Error('tts.enabled 必须是布尔值');
  if (typeof cfg.nativeVoice !== 'boolean') throw new Error('tts.nativeVoice 必须是布尔值');
  if (!['minimax', 'edge'].includes(cfg.provider)) throw new Error('tts.provider 只能是 minimax 或 edge');
  requireInt(cfg.maxChars, 'tts.maxChars', 1, 2000);
  requireInt(cfg.maxBytes, 'tts.maxBytes', 1024, 20 * 1024 * 1024);
  requireInt(cfg.timeoutMs, 'tts.timeoutMs', 5000, 120000);
  requireInt(cfg.pitch, 'tts.pitch', -12, 12);
  if (typeof cfg.speed !== 'number' || !Number.isFinite(cfg.speed) || cfg.speed < 0.5 || cfg.speed > 2) {
    throw new Error('tts.speed 必须在 0.5~2 之间');
  }
  for (const key of ['keyFile', 'model', 'voice']) {
    if (typeof cfg[key] !== 'string') throw new Error(`tts.${key} 必须是字符串`);
  }
  if (cfg.enabled && cfg.provider === 'minimax' && !cfg.keyFile) {
    throw new Error('tts.provider=minimax 时必须配置 tts.keyFile');
  }
  return cfg;
}

// 明确要语音的说法才触发；否定说法直接排除（"别发语音"）。
const NEGATIVE = /(?:不用|不要|别|无需|甭)(?:发|用|说)?\s*(?:语音|声音)/u;
const REQUEST = /(?:发|来|用|转)(?:一?[条段个])?\s*语音|念(?:给|一|出)|说给我听|读给我听|用说的|语音(?:回复|回我|说)/u;

export function wantsVoice(text) {
  if (typeof text !== 'string' || !text.trim()) return false;
  if (NEGATIVE.test(text)) return false;
  return REQUEST.test(text);
}
