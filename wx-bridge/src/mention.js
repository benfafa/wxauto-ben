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
export function findMention(text, aliases) {
  const value = String(text ?? '');
  for (const alias of normalizeAliases(aliases)) {
    if (value.includes(alias)) return { mentioned: true, alias };
  }
  return { mentioned: false, alias: null };
}

/** 去掉 @ 别名本身，保留其余文字。 */
export function stripMention(text, aliases) {
  let value = String(text ?? '');
  for (const alias of normalizeAliases(aliases)) value = value.split(alias).join(' ');
  return value
    .replace(/[\u2005\u00a0\u3000]/g, ' ')
    .replace(/[ \t]+/g, ' ')
    .replace(/^[\s,，。.!！?？:：;；、]+/, '')
    .replace(/[\s,，。.!！?？:：;；、]+$/, '')
    .trim();
}
