import fs from 'node:fs';
import path from 'node:path';

function normalize(value) {
  return String(value ?? '').replace(/\s+/g, ' ').trim();
}

function terms(value) {
  const text = normalize(value).toLowerCase();
  const out = new Set(text.split(/[^\p{L}\p{N}\u4e00-\u9fff]+/u).filter((item) => item.length > 1));
  for (let i = 0; i + 1 < text.length; i += 1) {
    const pair = text.slice(i, i + 2);
    if (/^[\p{L}\p{N}\u4e00-\u9fff]{2}$/u.test(pair)) out.add(pair);
  }
  return out;
}

/** Small local, per-group memory store. It keeps raw events on disk and
 * retrieves only messages related to the current @ prompt. */
export class GroupMemory {
  constructor({ stateDir = '', maxEntries = 1000, maxResults = 6, maxChars = 1800 } = {}) {
    this.file = stateDir ? path.join(stateDir, 'memory.jsonl') : '';
    this.maxEntries = Math.max(100, Math.floor(Number(maxEntries) || 1000));
    this.maxResults = Math.max(1, Math.floor(Number(maxResults) || 6));
    this.maxChars = Math.max(300, Math.floor(Number(maxChars) || 1800));
    this.byGroup = new Map();
    if (this.file) this.#load();
  }

  #load() {
    try {
      for (const line of fs.readFileSync(this.file, 'utf8').split(/\r?\n/)) {
        if (!line.trim()) continue;
        const item = JSON.parse(line);
        if (item?.group && item?.text) this.#push(item.group, item);
      }
    } catch (error) {
      if (error?.code !== 'ENOENT') throw new Error(`读取长期记忆失败：${this.file}（${error?.message ?? error}）`);
    }
  }

  #push(group, item) {
    const list = this.byGroup.get(group) ?? [];
    if (item.id && list.some((entry) => entry.id === item.id)) return false;
    list.push(item);
    if (list.length > this.maxEntries) list.splice(0, list.length - this.maxEntries);
    this.byGroup.set(group, list);
    return true;
  }

  add({ group, id, sender, text, atMe = false, timestamp = Date.now() } = {}) {
    group = normalize(group);
    text = normalize(text);
    if (!group || !text) return;
    const item = { group, id: String(id ?? ''), sender: normalize(sender) || '未知', text, atMe: Boolean(atMe), timestamp };
    if (!this.#push(group, item) || !this.file) return;
    fs.mkdirSync(path.dirname(this.file), { recursive: true, mode: 0o700 });
    fs.appendFileSync(this.file, `${JSON.stringify(item)}\n`, { encoding: 'utf8', mode: 0o600 });
  }

  search(group, query, sender = '') {
    const list = this.byGroup.get(group) ?? [];
    if (!list.length) return '（无）';
    const queryTerms = terms(query);
    const ranked = list.map((item, index) => {
      const haystack = item.text.toLowerCase();
      let score = 0;
      for (const term of queryTerms) if (haystack.includes(term)) score += term.length > 2 ? 3 : 1;
      return { item, index, score };
    }).filter(({ score }) => score > 0)
      .sort((a, b) => b.score - a.score || b.index - a.index);

    const ownSender = normalize(sender);
    const withUser = ownSender && ownSender !== '未知';
    const ownRecent = withUser ? list.filter((item) => item.sender === ownSender).slice(-3) : [];
    const ownRelevant = withUser
      ? ranked.filter(({ item }) => item.sender === ownSender).slice(0, 2).map(({ item }) => item)
      : [];
    const own = [...new Set([...ownRecent, ...ownRelevant])];
    const ownSet = new Set(own);
    const groupRelevant = ranked
      .filter(({ item }) => !ownSet.has(item))
      .slice(0, this.maxResults)
      .sort((a, b) => a.index - b.index);

    let used = 0;
    const lines = [];
    const addLine = (line) => {
      if (used + line.length + 1 > this.maxChars) return false;
      lines.push(line);
      used += line.length + 1;
      return true;
    };
    if (own.length) {
      addLine('【当前用户最近说过】');
      for (const item of own) {
        addLine(`${item.sender}：${item.text}${item.atMe ? '（@了你）' : ''}`);
      }
    }
    if (groupRelevant.length) {
      addLine('【群里相关】');
      for (const { item } of groupRelevant) {
        addLine(`${item.sender}：${item.text}${item.atMe ? '（@了你）' : ''}`);
      }
    }
    return lines.join('\n') || '（无相关长期记忆）';
  }
}
