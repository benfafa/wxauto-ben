/**
 * 连续对话：同一个人在窗口内连续 @ 若干次后，接下来他不 @ 也当成对话继续。
 *
 * 设计取向：
 * - 按「群 + 人」绑定。别人照旧要 @，不会被顺带接管。
 * - 只在内存里，进程重启就忘掉——重启后先 @ 一次再进会话，比误接管安全。
 * - 空闲超时和回合数上限都会退出，避免一次热闹之后无限接管。
 */
const DEFAULTS = {
  enabled: false,
  triggerMentions: 3,
  windowMs: 60000,
  idleTimeoutMs: 300000,
  maxTurns: 40,
  groups: []
};

function requireInt(value, label, min, max) {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < min || value > max) {
    throw new Error(`${label} 必须是 ${min}~${max} 的整数`);
  }
  return value;
}

export function normalizeConversation(input) {
  if (input === undefined) return { ...DEFAULTS, groups: [] };
  if (input === null || typeof input !== 'object' || Array.isArray(input)) {
    throw new Error('conversation 必须是对象');
  }
  const unknown = Object.keys(input).filter((key) => !Object.hasOwn(DEFAULTS, key));
  if (unknown.length) throw new Error(`conversation 含未知字段：${unknown.join(', ')}`);
  const cfg = { ...DEFAULTS, ...input };
  if (typeof cfg.enabled !== 'boolean') throw new Error('conversation.enabled 必须是布尔值');
  requireInt(cfg.triggerMentions, 'conversation.triggerMentions', 2, 10);
  requireInt(cfg.windowMs, 'conversation.windowMs', 5000, 600000);
  requireInt(cfg.idleTimeoutMs, 'conversation.idleTimeoutMs', 10000, 3600000);
  requireInt(cfg.maxTurns, 'conversation.maxTurns', 1, 500);
  if (!Array.isArray(cfg.groups) || cfg.groups.some((name) => typeof name !== 'string' || !name.trim())) {
    throw new Error('conversation.groups 必须是群名字符串数组');
  }
  return { ...cfg, groups: [...new Set(cfg.groups.map((name) => name.trim()))] };
}

export class ConversationTracker {
  constructor(cfg) {
    this.cfg = normalizeConversation(cfg);
    this.recent = new Map();   // "群\0人" -> [时间戳]
    this.sessions = new Map(); // 群 -> { user, expiresAt, turns }
  }

  #eligible(group) {
    return this.cfg.enabled && (!this.cfg.groups.length || this.cfg.groups.includes(group));
  }

  #live(group, at) {
    const session = this.sessions.get(group);
    if (!session) return null;
    if (at >= session.expiresAt) {
      this.sessions.delete(group);
      return null;
    }
    return session;
  }

  /** 收到一条 @。返回这条是否算在连续对话里、以及是否刚刚开启。 */
  mention(group, user, at) {
    if (!this.#eligible(group)) return { active: false, opened: false };
    const current = this.#live(group, at);
    if (current && current.user === user) {
      current.expiresAt = at + this.cfg.idleTimeoutMs;
      return { active: true, opened: false };
    }
    const key = `${group}\u0000${user}`;
    const times = (this.recent.get(key) ?? []).filter((time) => at - time <= this.cfg.windowMs);
    times.push(at);
    this.recent.set(key, times);
    if (times.length < this.cfg.triggerMentions) return { active: false, opened: false };
    this.recent.delete(key);
    this.sessions.set(group, { user, expiresAt: at + this.cfg.idleTimeoutMs, turns: 0 });
    return { active: true, opened: true };
  }

  /** 不带 @ 的消息：只有会话主角才续上。 */
  follow(group, user, at) {
    const current = this.#live(group, at);
    if (!current || current.user !== user) return false;
    current.expiresAt = at + this.cfg.idleTimeoutMs;
    return true;
  }

  /** 回复成功后记一轮；超过上限就结束会话。 */
  countTurn(group, at) {
    const current = this.#live(group, at);
    if (!current) return { ok: true };
    current.turns += 1;
    if (current.turns >= this.cfg.maxTurns) {
      this.sessions.delete(group);
      return { ok: false, reason: 'max-turns', turns: current.turns };
    }
    return { ok: true, turns: current.turns };
  }

  status(group, at) {
    const current = this.#live(group, at);
    return current ? { user: current.user, turns: current.turns } : null;
  }
}
