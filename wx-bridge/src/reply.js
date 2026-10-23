import { avoidSplitSurrogate } from './text.js';

/** 把 agent 的 Markdown 回复转成微信可读的纯文本。 */
export function mdToPlain(markdown) {
  let text = String(markdown ?? '');
  text = text.replace(/```[a-zA-Z0-9_+-]*\n?([\s\S]*?)```/g, (_, body) => body.replace(/\n+$/, ''));
  text = text.replace(/`([^`\n]+)`/g, '$1');
  text = text.replace(/!\[([^\]\n]{0,500})\]\(([^)\s]{1,2000})\)/g, (_, alt, url) => alt || url);
  text = text.replace(/\[([^\]\n]{1,500})\]\(([^)\s]{1,2000})\)/g, '$1 ($2)');
  text = text.replace(/\*\*\*([^*]+)\*\*\*/g, '$1');
  text = text.replace(/\*\*([^*]+)\*\*/g, '$1');
  text = text.replace(/\*(?!\s)([^*\n]*?[^\s*])\*/g, '$1');
  text = text.replace(/~~([^~]+)~~/g, '$1');
  text = text.replace(/__([^_]+)__/g, '$1');
  text = text.replace(/^#{1,6}\s+/gm, '');
  text = text.replace(/^>\s?/gm, '');
  text = text.replace(/^\s*[-*+]\s+/gm, '• ');
  text = text.replace(/^\s*\d+\.\s+/gm, (match) => match.trimStart());
  text = text.split('\n')
    .filter((line) => !/^\s*\|?[\s:|-]+\|?\s*$/.test(line) || /\S/.test(line.replace(/[\s:|-]/g, '')))
    .join('\n');
  text = text.replace(/^\s*\|/gm, '').replace(/\|\s*$/gm, '');
  text = text.replace(/\n{3,}/g, '\n\n');
  return text.trim();
}

/**
 * 微信侧出站前的最后一道文本处理。
 *
 * 不追求“理解 Markdown”，只保证不会因为模型输出把整段消息发成乱码，
 * 也不会用纯文本伪造 @所有人（微信真正会通知全体的是 UI 层 @，这里只是消除歧义）。
 */
export function sanitizeOutgoing(text) {
  return mdToPlain(text)
    .replace(/\r\n?/g, '\n')
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, '')
    .replace(/@(所有人|all|everyone)/gi, (match) => `＠${match.slice(1)}`)
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

function findCut(text, max) {
  const windowStart = Math.floor(max * 0.4);
  for (const mark of ['\n', '。', '！', '？', '!', '?', '；', ';', '，', ',', ' ']) {
    const index = text.lastIndexOf(mark, max - 1);
    if (index >= windowStart) return index + (mark === '\n' ? 1 : 1);
  }
  return max;
}

/** 按长度切分，优先段落/句子边界，并且不切开代理对。 */
export function splitMessage(text, max = 1200) {
  const safeMax = Number.isFinite(Number(max)) ? Math.max(1, Math.floor(Number(max))) : 1200;
  const value = String(text ?? '').trim();
  if (!value) return [];
  if (value.length <= safeMax) return [value];

  const parts = [];
  let rest = value;
  while (rest.length > safeMax) {
    let cut = findCut(rest, safeMax);
    cut = avoidSplitSurrogate(rest, cut);
    if (cut <= 0) {
      const first = rest.charCodeAt(0);
      const second = rest.charCodeAt(1);
      cut = (first >= 0xd800 && first <= 0xdbff && second >= 0xdc00 && second <= 0xdfff) ? 2 : 1;
    }
    const part = rest.slice(0, cut).trim();
    if (part) parts.push(part);
    rest = rest.slice(cut).trimStart();
  }
  if (rest) parts.push(rest);
  return parts;
}
