/**
 * 按 UTF-16 码元截断时避免切开代理对（emoji / 生僻字）。
 * 上限按码点意义的字符数近似，纯 BMP 文本与 slice 一致。
 */
export function truncateText(value, max) {
  const text = String(value ?? '');
  const limit = Number.isFinite(Number(max)) ? Math.max(0, Math.floor(Number(max))) : 0;
  if (limit === 0) return '';
  if (text.length <= limit) return text;
  let cut = limit;
  const code = text.charCodeAt(cut - 1);
  if (code >= 0xd800 && code <= 0xdbff) cut -= 1;
  if (cut <= 0) {
    const first = text.charCodeAt(0);
    const second = text.charCodeAt(1);
    cut = (first >= 0xd800 && first <= 0xdbff && second >= 0xdc00 && second <= 0xdfff) ? 2 : 1;
  }
  return text.slice(0, cut);
}

export function truncateTextTail(value, max) {
  const text = String(value ?? '');
  const limit = Number.isFinite(Number(max)) ? Math.max(0, Math.floor(Number(max))) : 0;
  if (limit === 0) return '';
  if (text.length <= limit) return text;
  let start = text.length - limit;
  const code = text.charCodeAt(start);
  if (code >= 0xdc00 && code <= 0xdfff) {
    const before = text.charCodeAt(start - 1);
    start = (start > 0 && before >= 0xd800 && before <= 0xdbff) ? start - 1 : start + 1;
  }
  return text.slice(start);
}

export function avoidSplitSurrogate(text, index) {
  if (index <= 0) return 0;
  const code = text.charCodeAt(index - 1);
  if (code >= 0xd800 && code <= 0xdbff) return index - 1;
  return index;
}
