/** 把配置里的别名规范成以 @ 开头、按长度倒序（避免短别名先吃掉长别名）。 */
export function normalizeAliases(aliases) {
  const out = [];
  for (const raw of aliases ?? []) {
    if (typeof raw !== 'string') continue;
    let alias = raw.trim();
    if (!alias) continue;
    if (!alias.startsWith('@')) alias = `@${alias}`;
    if (alias === '@') continue;
    if (!out.includes(alias)) out.push(alias);
  }
  return out.sort((a, b) => b.length - a.length);
}

/**
 * 是否 @ 了机器人。
 *
 * wxauto 3.9 不提供结构化的 at 列表，只能匹配可见文本。因此别名宁可选得具体一些
 * （例如 @小助手），不要用 @助手 这种容易出现在普通句子里的短词。
 */
// A one-letter alias must not swallow a longer name: WeChat ends a real mention
// with a separator, never with another letter or digit.
const WORD_AFTER = /[0-9A-Za-z_\uff21-\uff3a\uff41-\uff5a\uff10-\uff19]/u;

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function mentionPattern(alias, flags = 'u') {
  return new RegExp(`${escapeRegExp(alias)}(?!${WORD_AFTER.source})`, flags);
}

export function findMention(text, aliases) {
  const value = String(text ?? '');
  for (const alias of normalizeAliases(aliases)) {
    if (mentionPattern(alias).test(value)) return { mentioned: true, alias };
  }
  return { mentioned: false, alias: null };
}

// wxautox4-open 读不到群昵称时 sender 是「未知」；拿它去 @ 会让整条回复失败。
const UNKNOWN_SENDERS = new Set(['未知', 'unknown']);

/** 可安全 @ 的发送者昵称；未知/空名返回 null，让调用方改发纯文字。 */
export function mentionable(sender) {
  const name = String(sender ?? '').trim();
  return name && !UNKNOWN_SENDERS.has(name.toLowerCase()) ? name : null;
}

/** 去掉 @ 别名本身，保留其余文字。 */
export function stripMention(text, aliases) {
  let value = String(text ?? '');
  for (const alias of normalizeAliases(aliases)) value = value.replace(mentionPattern(alias, 'gu'), ' ');
  return value
    .replace(/[\u2005\u00a0\u3000]/g, ' ')
    .replace(/[ \t]+/g, ' ')
    .replace(/^[\s,，。.!！?？:：;；、]+/, '')
    .replace(/[\s,，。.!！?？:：;；、]+$/, '')
    .trim();
}
