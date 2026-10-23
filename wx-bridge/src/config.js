import fs from 'node:fs';
import path from 'node:path';
import { normalizeStickers } from './stickers.js';
import { normalizeVision } from './vision.js';
import { normalizeProactive } from './proactive.js';
import { normalizeConversation } from './conversation.js';
import { normalizeTts } from './tts.js';

const DEFAULTS = Object.freeze({
  wechat: {
    collectorUrl: 'http://127.0.0.1:8765',
    token: '',
    groups: [],
    mentionAliases: [],
    pollMs: 800,
    pollWaitMs: 15000,
    requestTimeoutMs: 30000,
    contextMessages: 20,
    contextMaxChars: 4000
  },
  dsh: {
    baseUrl: 'http://127.0.0.1:3080',
    authToken: '',
    allowRemote: false,
    provider: 'deepseek-official',
    model: 'deepseek-flash',
    reasoningEffort: 'max',
    agentPreset: 'wx-chat',
    verifyPreset: true,
    allowDefaultPreset: false,
    workspaceTitle: '微信群聊',
    sessionCwd: '',
    turnTimeoutMs: 120000,
    streamReconnectMs: 3000
  },
  behavior: {
    requireMention: true,
    replyWithMention: true,
    minReplyIntervalMs: 1500,
    maxRepliesPerMinute: 12,
    maxTriggerAgeMs: 300000,
    maxReplyChars: 1200,
    sendErrorNotice: false,
    logAllMessages: true,
    globalConcurrency: 2,
    maxQueuePerGroup: 10,
    partDelayMs: 500,
    memoryMaxEntries: 1000,
    memoryResults: 6,
    memoryMaxChars: 1800
  },
  stateDir: 'state'
});

const PLACEHOLDER_TOKENS = new Set([
  'change-me',
  'changeme',
  'replace-me',
  'replace_me',
  'your-token',
  'token'
]);

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function mergeDefaults(base, input) {
  if (!isPlainObject(input)) return structuredClone(base);
  const out = structuredClone(base);
  for (const [key, value] of Object.entries(input)) {
    if (isPlainObject(value) && isPlainObject(out[key])) out[key] = mergeDefaults(out[key], value);
    else out[key] = value;
  }
  return out;
}

function requireString(value, label, { allowEmpty = false } = {}) {
  if (typeof value !== 'string') throw new Error(`${label} 必须是字符串`);
  const text = value.trim();
  if (!allowEmpty && text === '') throw new Error(`${label} 不能为空`);
  return text;
}

function requireUrl(value, label) {
  const text = requireString(value, label);
  let url;
  try {
    url = new URL(text);
  } catch {
    throw new Error(`${label} 不是合法 URL：${text}`);
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new Error(`${label} 只支持 http/https：${text}`);
  }
  return url.toString().replace(/\/+$/, '');
}

function requireInt(value, label, min, max, fallback) {
  const n = value === undefined ? fallback : Number(value);
  if (!Number.isInteger(n) || n < min || n > max) {
    throw new Error(`${label} 必须是 ${min}~${max} 的整数（当前 ${JSON.stringify(value)}）`);
  }
  return n;
}

function requireBool(value, label, fallback) {
  if (value === undefined) return fallback;
  if (typeof value !== 'boolean') throw new Error(`${label} 必须是布尔值`);
  return value;
}

function normalizeAliases(value) {
  if (!Array.isArray(value) || value.length === 0) {
    throw new Error('wechat.mentionAliases 至少配置一个 @ 别名，例如 ["@机器人"]');
  }
  const out = [];
  for (const item of value) {
    if (typeof item !== 'string') throw new Error('wechat.mentionAliases 里的每一项都必须是字符串');
    let alias = item.trim();
    if (!alias) continue;
    if (!alias.startsWith('@')) alias = `@${alias}`;
    if (alias === '@') throw new Error('wechat.mentionAliases 不能只配置一个 "@"');
    if (/^@(所有人|all|everyone)$/i.test(alias)) {
      throw new Error(`wechat.mentionAliases 不能使用 ${alias}（会误触发全体成员消息）`);
    }
    if (!out.includes(alias)) out.push(alias);
  }
  if (out.length === 0) throw new Error('wechat.mentionAliases 至少配置一个非空别名');
  return out.sort((a, b) => b.length - a.length);
}

function normalizeGroups(value) {
  if (!Array.isArray(value) || value.length === 0) {
    throw new Error('wechat.groups 至少配置一个群名（fail-closed：空白名单不监听任何群）');
  }
  if (value.length > 50) throw new Error('wechat.groups 最多 50 个群；wxauto 需要为每个群保持一个独立聊天窗口');
  const out = [];
  for (const item of value) {
    const group = requireString(item, 'wechat.groups 里的群名');
    if (!out.includes(group)) out.push(group);
  }
  return out;
}

function readJson(file) {
  let text;
  try {
    text = fs.readFileSync(file, 'utf8');
  } catch (error) {
    throw new Error(`读取配置失败：${file}（${error?.message ?? error}）`);
  }
  if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    throw new Error(`配置文件不是合法 JSON：${file}（${error?.message ?? error}）`);
  }
  if (!isPlainObject(parsed)) throw new Error(`配置文件顶层必须是对象：${file}`);
  return parsed;
}

/**
 * 读取并校验 wx-bridge 配置。
 *
 * 所有安全相关项都 fail-closed：空群白名单、占位符 token、空 @ 别名、空 preset
 * 都会直接拒绝启动，而不是在运行期静默不工作。
 */
export function loadConfig(file) {
  const configFile = path.resolve(file);
  const raw = readJson(configFile);
  const merged = mergeDefaults(DEFAULTS, raw);
  const baseDir = path.dirname(configFile);

  const token = requireString(merged.wechat.token, 'wechat.token');
  if (token.length < 16) {
    throw new Error('wechat.token 至少 16 个字符；请用随机串，不要用可猜的口令');
  }
  if (PLACEHOLDER_TOKENS.has(token.toLowerCase()) || /^replace/i.test(token)) {
    throw new Error('wechat.token 仍是示例占位符；请改成随机串（例如 node -e "console.log(require(\'crypto\').randomBytes(24).toString(\'hex\'))"）');
  }

  const groups = normalizeGroups(merged.wechat.groups);
  const mentionAliases = normalizeAliases(merged.wechat.mentionAliases);

  const stateDirRaw = requireString(merged.stateDir, 'stateDir');
  const stateDir = path.isAbsolute(stateDirRaw) ? stateDirRaw : path.resolve(baseDir, stateDirRaw);

  const sessionCwdRaw = requireString(merged.dsh.sessionCwd, 'dsh.sessionCwd', { allowEmpty: true });
  const sessionCwd = sessionCwdRaw === ''
    ? path.join(stateDir, 'agents')
    : (path.isAbsolute(sessionCwdRaw) ? sessionCwdRaw : path.resolve(baseDir, sessionCwdRaw));

  const agentPreset = requireString(merged.dsh.agentPreset, 'dsh.agentPreset', { allowEmpty: true });
  const allowDefaultPreset = requireBool(merged.dsh.allowDefaultPreset, 'dsh.allowDefaultPreset', false);
  if (!agentPreset && !allowDefaultPreset) {
    throw new Error('dsh.agentPreset 不能为空：微信群里必须使用无本地工具的 preset；确实要用 DSH 默认 preset 请显式设置 dsh.allowDefaultPreset=true');
  }

  const pollWaitMs = requireInt(merged.wechat.pollWaitMs, 'wechat.pollWaitMs', 0, 60000, DEFAULTS.wechat.pollWaitMs);
  const requestTimeoutMs = requireInt(
    merged.wechat.requestTimeoutMs,
    'wechat.requestTimeoutMs',
    1000,
    120000,
    Math.max(DEFAULTS.wechat.requestTimeoutMs, pollWaitMs + 5000)
  );
  if (requestTimeoutMs < pollWaitMs + 1000) {
    throw new Error(`wechat.requestTimeoutMs（${requestTimeoutMs}）必须至少比 pollWaitMs（${pollWaitMs}）大 1000ms`);
  }

  const cfg = {
    configFile,
    stateDir,
    stickers: normalizeStickers(raw.stickers, baseDir),
    vision: normalizeVision(raw.vision),
    tts: normalizeTts(raw.tts),
    conversation: (() => {
      const conversation = normalizeConversation(raw.conversation);
      const unknown = conversation.groups.filter((name) => !groups.includes(name));
      if (unknown.length) {
        throw new Error(`conversation.groups 里有没被监听的群：${unknown.join('、')}`);
      }
      return conversation;
    })(),
    proactive: (() => {
      const proactive = normalizeProactive(raw.proactive);
      const unknown = proactive.groups.filter((name) => !groups.includes(name));
      if (unknown.length) {
        throw new Error(`proactive.groups 里有没被监听的群：${unknown.join('、')}（wechat.groups 才有 ${groups.join('、')}）`);
      }
      return proactive;
    })(),
    wechat: {
      collectorUrl: requireUrl(merged.wechat.collectorUrl, 'wechat.collectorUrl'),
      token,
      groups,
      mentionAliases,
      pollMs: requireInt(merged.wechat.pollMs, 'wechat.pollMs', 100, 10000, DEFAULTS.wechat.pollMs),
      pollWaitMs,
      requestTimeoutMs,
      contextMessages: requireInt(merged.wechat.contextMessages, 'wechat.contextMessages', 0, 100, DEFAULTS.wechat.contextMessages),
      contextMaxChars: requireInt(merged.wechat.contextMaxChars, 'wechat.contextMaxChars', 500, 20000, DEFAULTS.wechat.contextMaxChars)
    },
    dsh: {
      baseUrl: requireUrl(merged.dsh.baseUrl, 'dsh.baseUrl'),
      authToken: requireString(merged.dsh.authToken, 'dsh.authToken', { allowEmpty: true }),
      allowRemote: requireBool(merged.dsh.allowRemote, 'dsh.allowRemote', false),
      provider: requireString(merged.dsh.provider, 'dsh.provider'),
      model: requireString(merged.dsh.model, 'dsh.model'),
      reasoningEffort: requireString(merged.dsh.reasoningEffort, 'dsh.reasoningEffort'),
      agentPreset,
      verifyPreset: requireBool(merged.dsh.verifyPreset, 'dsh.verifyPreset', true),
      allowDefaultPreset,
      workspaceTitle: requireString(merged.dsh.workspaceTitle, 'dsh.workspaceTitle'),
      sessionCwd,
      turnTimeoutMs: requireInt(merged.dsh.turnTimeoutMs, 'dsh.turnTimeoutMs', 10000, 600000, DEFAULTS.dsh.turnTimeoutMs),
      streamReconnectMs: requireInt(merged.dsh.streamReconnectMs, 'dsh.streamReconnectMs', 500, 60000, DEFAULTS.dsh.streamReconnectMs)
    },
    behavior: {
      requireMention: requireBool(merged.behavior.requireMention, 'behavior.requireMention', true),
      replyWithMention: requireBool(merged.behavior.replyWithMention, 'behavior.replyWithMention', true),
      minReplyIntervalMs: requireInt(merged.behavior.minReplyIntervalMs, 'behavior.minReplyIntervalMs', 0, 60000, DEFAULTS.behavior.minReplyIntervalMs),
      maxRepliesPerMinute: requireInt(merged.behavior.maxRepliesPerMinute, 'behavior.maxRepliesPerMinute', 1, 120, DEFAULTS.behavior.maxRepliesPerMinute),
      maxTriggerAgeMs: requireInt(merged.behavior.maxTriggerAgeMs, 'behavior.maxTriggerAgeMs', 0, 3600000, DEFAULTS.behavior.maxTriggerAgeMs),
      maxReplyChars: requireInt(merged.behavior.maxReplyChars, 'behavior.maxReplyChars', 100, 4000, DEFAULTS.behavior.maxReplyChars),
      sendErrorNotice: requireBool(merged.behavior.sendErrorNotice, 'behavior.sendErrorNotice', false),
      logAllMessages: requireBool(merged.behavior.logAllMessages, 'behavior.logAllMessages', true),
      globalConcurrency: requireInt(merged.behavior.globalConcurrency, 'behavior.globalConcurrency', 1, 4, DEFAULTS.behavior.globalConcurrency),
      maxQueuePerGroup: requireInt(merged.behavior.maxQueuePerGroup, 'behavior.maxQueuePerGroup', 1, 100, DEFAULTS.behavior.maxQueuePerGroup),
      partDelayMs: requireInt(merged.behavior.partDelayMs, 'behavior.partDelayMs', 0, 5000, DEFAULTS.behavior.partDelayMs),
      memoryMaxEntries: requireInt(merged.behavior.memoryMaxEntries, 'behavior.memoryMaxEntries', 100, 10000, DEFAULTS.behavior.memoryMaxEntries),
      memoryResults: requireInt(merged.behavior.memoryResults, 'behavior.memoryResults', 1, 20, DEFAULTS.behavior.memoryResults),
      memoryMaxChars: requireInt(merged.behavior.memoryMaxChars, 'behavior.memoryMaxChars', 300, 10000, DEFAULTS.behavior.memoryMaxChars)
    }
  };

  return cfg;
}
