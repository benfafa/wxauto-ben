import fs from 'node:fs';
import path from 'node:path';

const MAX_LOG_BYTES = 5 * 1024 * 1024;

function rotate(file) {
  try {
    const stat = fs.statSync(file);
    if (stat.size < MAX_LOG_BYTES) return;
    const rotated = `${file}.1`;
    fs.rmSync(rotated, { force: true });
    fs.renameSync(file, rotated);
  } catch (error) {
    if (error?.code !== 'ENOENT') {
      // 日志轮转失败不能把主流程带死；控制台还能看到。
      console.error(`[logger] rotate failed: ${error?.message ?? error}`);
    }
  }
}

function append(file, line) {
  try {
    rotate(file);
    fs.appendFileSync(file, line, { encoding: 'utf8', mode: 0o600 });
  } catch (error) {
    console.error(`[logger] write failed ${file}: ${error?.message ?? error}`);
  }
}

export class Logger {
  constructor({ stateDir, name = 'bridge', echo = true }) {
    fs.mkdirSync(stateDir, { recursive: true, mode: 0o700 });
    this.logFile = path.join(stateDir, `${name}.log`);
    this.messageFile = path.join(stateDir, 'messages.jsonl');
    this.activityFile = path.join(stateDir, 'activity.jsonl');
    this.echo = echo;
  }

  #line(level, args) {
    const text = args.map((value) => {
      if (typeof value === 'string') return value;
      try {
        return JSON.stringify(value);
      } catch {
        return String(value);
      }
    }).join(' ');
    return `${new Date().toISOString()} [${level}] ${text}`;
  }

  #write(level, args) {
    const line = this.#line(level, args);
    if (this.echo) console.log(line);
    append(this.logFile, `${line}\n`);
  }

  info(...args) {
    this.#write('info', args);
  }

  warn(...args) {
    this.#write('warn', args);
  }

  error(...args) {
    this.#write('error', args);
  }

  activity(record) {
    append(this.activityFile, `${JSON.stringify({ time: new Date().toISOString(), ...record })}\n`);
  }

  message(record) {
    append(this.messageFile, `${JSON.stringify({ time: new Date().toISOString(), ...record })}\n`);
  }
}
