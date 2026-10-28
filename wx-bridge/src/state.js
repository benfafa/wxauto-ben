import fs from 'node:fs';
import path from 'node:path';
import { randomBytes } from 'node:crypto';

function atomicWrite(file, content) {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const tmp = `${file}.${process.pid}.${randomBytes(4).toString('hex')}.tmp`;
  try {
    fs.writeFileSync(tmp, content, { encoding: 'utf8', mode: 0o600 });
    fs.renameSync(tmp, file);
  } catch (error) {
    fs.rmSync(tmp, { force: true });
    throw error;
  }
}

/**
 * 群名 -> DSH sessionId 映射。
 *
 * 文件读坏时**大声失败**，不静默重置：静默重置会让每个群拿到全新会话（上下文全丢），
 * 随后又把空映射写回磁盘，一次截断就变成永久丢失。
 */
export class SessionStore {
  constructor(file) {
    this.file = file;
    this.sessions = {};
    this.load();
  }

  load() {
    let text;
    try {
      text = fs.readFileSync(this.file, 'utf8');
    } catch (error) {
      if (error?.code === 'ENOENT') return;
      throw new Error(`读取会话映射失败：${this.file}（${error?.message ?? error}）`);
    }
    let parsed;
    try {
      parsed = JSON.parse(text);
    } catch (error) {
      throw new Error(`会话映射不是合法 JSON，已拒绝覆盖：${this.file}（${error?.message ?? error}）。请备份后删除该文件再启动`);
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error(`会话映射顶层必须是对象：${this.file}`);
    }
    const sessions = parsed.sessions ?? {};
    if (!sessions || typeof sessions !== 'object' || Array.isArray(sessions)) {
      throw new Error(`会话映射的 sessions 字段必须是对象：${this.file}`);
    }
    this.sessions = {};
    for (const [group, sessionId] of Object.entries(sessions)) {
      if (typeof group === 'string' && group && typeof sessionId === 'string' && sessionId) {
        this.sessions[group] = sessionId;
      }
    }
  }

  save() {
    atomicWrite(this.file, `${JSON.stringify({ version: 1, sessions: this.sessions }, null, 2)}\n`);
  }

  get(group) {
    return this.sessions[group] ?? null;
  }

  set(group, sessionId) {
    this.sessions[group] = sessionId;
    this.save();
  }

  remove(group) {
    if (!Object.hasOwn(this.sessions, group)) return;
    delete this.sessions[group];
    this.save();
  }

  clear() {
    if (!Object.keys(this.sessions).length) return;
    this.sessions = {};
    this.save();
  }

  entries() {
    return Object.entries(this.sessions);
  }
}

function processAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code === 'EPERM';
  }
}

/**
 * 单实例锁。两个桥接同时跑会对同一条 @ 消息各回一次，且争抢 DSH 会话。
 */
export function acquireInstanceLock(stateDir) {
  fs.mkdirSync(stateDir, { recursive: true, mode: 0o700 });
  const file = path.join(stateDir, 'bridge.lock');
  const content = `${JSON.stringify({ pid: process.pid, startedAt: new Date().toISOString() }, null, 2)}\n`;
  for (;;) {
    try {
      // wx makes the ownership claim atomic, avoiding two bridges passing a
      // read-then-write race at startup.
      fs.writeFileSync(file, content, { encoding: 'utf8', mode: 0o600, flag: 'wx' });
      break;
    } catch (error) {
      if (error?.code !== 'EEXIST') throw error;
      let existing;
      try {
        existing = JSON.parse(fs.readFileSync(file, 'utf8'));
      } catch (readError) {
        if (readError?.code !== 'ENOENT') {
          // A damaged lock cannot prove that another process owns the lock;
          // remove it and retry the atomic claim.
          fs.rmSync(file, { force: true });
        }
        continue;
      }
      if (existing?.pid !== process.pid && processAlive(Number(existing?.pid))) {
        throw new Error(`已有 wx-bridge 实例在运行（pid ${existing.pid}）；请先停止它，或确认进程已退出后删除 ${file}`);
      }
      fs.rmSync(file, { force: true });
    }
  }
  let released = false;
  return {
    file,
    release() {
      if (released) return;
      released = true;
      try {
        const current = JSON.parse(fs.readFileSync(file, 'utf8'));
        if (current?.pid === process.pid) fs.rmSync(file, { force: true });
      } catch {
        // 退出路径不因锁文件清理失败而报错。
      }
    }
  };
}
