import { truncateText } from './text.js';

const PER_MESSAGE_CHARS = 300;

function oneLine(text) {
  return String(text ?? '').replace(/\s+/g, ' ').trim();
}

/**
 * 每个群一份滚动窗口。所有消息（包括没有 @ 的）都进入窗口，
 * 这样被 @ 时 agent 能看到最近的对话，而不是只看到孤零零一句话。
 */
export class GroupContext {
  constructor({ maxMessages = 20, maxChars = 4000 } = {}) {
    const messages = Number(maxMessages);
    const chars = Number(maxChars);
    this.maxMessages = Number.isFinite(messages) ? Math.max(0, Math.floor(messages)) : 20;
    this.maxChars = Number.isFinite(chars) ? Math.max(0, Math.floor(chars)) : 4000;
    this.byGroup = new Map();
  }

  add(message) {
    if (!message?.group) return;
    let list = this.byGroup.get(message.group);
    if (!list) {
      list = [];
      this.byGroup.set(message.group, list);
    }
    if (message.id && list.some((item) => item.id === message.id)) return;
    list.push({
      id: message.id,
      sender: message.sender,
      text: oneLine(message.text),
      atMe: Boolean(message.atMe)
    });
    if (list.length > this.maxMessages) list.splice(0, list.length - this.maxMessages);
    this.byGroup.set(message.group, list);
  }

  /** 当前消息之前的上下文；当前消息由 bridge 单独放在 prompt 里。 */
  formatPrevious(group, currentId) {
    const list = this.byGroup.get(group) ?? [];
    const previous = currentId ? list.filter((item) => item.id !== currentId) : list;
    if (previous.length === 0) return '（无）';

    const lines = [];
    let used = 0;
    let omitted = false;
    for (let i = previous.length - 1; i >= 0; i -= 1) {
      const item = previous[i];
      const text = truncateText(item.text, PER_MESSAGE_CHARS);
      const line = `${item.sender || '未知'}：${text}${item.atMe ? '（@了你）' : ''}`;
      if (used + line.length + 1 > this.maxChars) {
        omitted = true;
        break;
      }
      lines.push(line);
      used += line.length + 1;
    }
    lines.reverse();
    return `${omitted ? '（更早的消息已省略）\n' : ''}${lines.join('\n')}`;
  }
}
