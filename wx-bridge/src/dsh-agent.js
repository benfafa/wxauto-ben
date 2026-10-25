import fs from 'node:fs';
import { validateImages } from './vision.js';
import { NodeApiClient, unwrap, createTurnCollector, discoverDshLaunchToken } from './vendor/dsh-client.js';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function isNotFound(error) {
  return /not[- ]?found|session\/not-found/i.test(String(error?.message ?? error));
}

function withTimeout(promise, ms, label) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} 超时（${ms}ms）`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

/**
 * 一次 turn 的等待器。
 *
 * 只在收到 turn/start 后建立 collector：会话流重连时会补发历史事件，
 * 若任何事件都建 collector，迟到的 assistant/message 会留下永不结束的空壳。
 */
class TurnWaiter {
  constructor({ sessionId, timeoutMs, label }) {
    this.sessionId = sessionId;
    this.label = label;
    this.collector = null;
    this.settled = false;
    this.timer = setTimeout(() => {
      this.reject(new Error(`${label} 等待 agent 回合超时（${timeoutMs}ms）`));
    }, timeoutMs);
    this.promise = new Promise((resolve, reject) => {
      this._resolve = resolve;
      this._reject = reject;
    });
    this.promise.catch(() => {});
  }

  push(event) {
    if (this.settled) return;
    if (!this.collector) {
      if (event?.type !== 'turn/start') return;
      this.collector = createTurnCollector();
    }
    const ended = this.collector.push(event);
    if (!ended) return;
    const reason = ended.reason?.kind ?? 'unknown';
    if (reason !== 'completed') {
      this.reject(new Error(`agent 回合未正常完成：${reason}`));
      return;
    }
    const text = String(ended.text ?? '').trim();
    if (!text) {
      this.reject(new Error('agent 回合完成但没有文本回复'));
      return;
    }
    this.resolve({ text, turn: ended.turn });
  }

  resolve(value) {
    if (this.settled) return;
    this.settled = true;
    clearTimeout(this.timer);
    this._resolve(value);
  }

  reject(error) {
    if (this.settled) return;
    this.settled = true;
    clearTimeout(this.timer);
    this._reject(error);
  }
}

export class DshAgent {
  constructor(cfg, { store, logger, api }) {
    this.cfg = cfg;
    this.store = store;
    this.log = logger;
    this.waiters = new Map();
    this.readyWaiters = new Set();
    this.modelApplied = new Set();
    this.streamReady = false;
    this.lastStreamError = null;
    this.abort = new AbortController();
    this.pumpPromise = null;
    this.stopping = false;

    if (api) {
      this.api = api;
    } else {
      const authToken = cfg.dsh.authToken || discoverDshLaunchToken();
      this.api = new NodeApiClient(cfg.dsh.baseUrl, undefined, {
        token: authToken,
        tokenExplicit: Boolean(cfg.dsh.authToken),
        header: 'authorization',
        prefix: 'Bearer',
        allowRemote: cfg.dsh.allowRemote === true
      });
    }
  }

  async start() {
    this.log.info(`连接 DSH：${this.cfg.dsh.baseUrl}`);
    if (this.cfg.dsh.allowDefaultPreset && !this.cfg.dsh.agentPreset) {
      this.log.warn('[警告] dsh.agentPreset 为空且 allowDefaultPreset=true：群消息将进入 DSH 默认 preset，可能携带本地工具');
    }
    await this.#verifyPreset();
    this.pumpPromise = this.#pump();
    await this.#waitStreamReady(60000);
    this.log.info('DSH 事件流已连接');
  }

  async stop() {
    this.stopping = true;
    this.abort.abort(new Error('bridge stopping'));
    for (const waiter of this.waiters.values()) waiter.reject(new Error('bridge stopping'));
    this.waiters.clear();
    this.#wakeReadyWaiters();
    try {
      await withTimeout(this.pumpPromise ?? Promise.resolve(), 5000, '停止 DSH 事件流');
    } catch {
      // 退出路径不再阻塞。
    }
  }

  async #verifyPreset() {
    const preset = this.cfg.dsh.agentPreset;
    if (!preset) return;
    let presets;
    const deadline = Date.now() + 60000;
    for (;;) {
      try {
        ({ presets } = unwrap(await this.api.agentPresets.list({}), 'agentPresets.list'));
        break;
      } catch (error) {
        if (!this.cfg.dsh.verifyPreset) {
          this.log.warn(`无法读取 DSH preset 列表，按配置继续：${error?.message ?? error}`);
          return;
        }
        if (this.abort.signal.aborted || Date.now() > deadline) {
          throw new Error(`无法从 DSH 读取 preset 列表：${error?.message ?? error}。请确认 DSH 已启动，或把 dsh.verifyPreset 设为 false（不推荐）`);
        }
        this.log.warn(`DSH 尚未就绪（${error?.message ?? error}），2 秒后重试…`);
        await sleep(2000);
      }
    }
    const ids = (presets ?? []).map((item) => String(item?.id ?? '')).filter(Boolean);
    if (!ids.includes(preset)) {
      const message = `DSH 没有 preset "${preset}"。可用：${ids.join(', ') || '（空）'}。`
        + '请先运行 node scripts/install-preset.mjs 并重启 dsh web；'
        + '请确认配置的 preset 已注册，不自动退回默认 preset。';
      if (this.cfg.dsh.verifyPreset) throw new Error(message);
      this.log.warn(`[警告] ${message}`);
      return;
    }
    this.log.info(`已验证 DSH preset：${preset}`);
  }

  async #workspaceId() {
    fs.mkdirSync(this.cfg.dsh.sessionCwd, { recursive: true });
    const value = unwrap(await this.api.workspace.create({ path: this.cfg.dsh.sessionCwd }), 'workspace.create');
    const workspaceId = value?.workspace?.workspaceId;
    if (!workspaceId) throw new Error('DSH workspace.create 未返回 workspaceId');
    if (value.created && this.cfg.dsh.workspaceTitle) {
      try {
        await this.api.workspace.rename({ workspaceId, title: this.cfg.dsh.workspaceTitle });
      } catch (error) {
        this.log.warn(`重命名 DSH workspace 失败（不影响会话）：${error?.message ?? error}`);
      }
    }
    return workspaceId;
  }

  async #applyModel(sessionId) {
    if (this.modelApplied.has(sessionId)) return 'ok';
    const { provider, model, reasoningEffort } = this.cfg.dsh;
    let lastError;
    for (let attempt = 1; attempt <= 2; attempt += 1) {
      try {
        unwrap(await this.api.sessions.selectModel({ sessionId, provider, model, reasoningEffort }), 'session.selectModel');
        this.modelApplied.add(sessionId);
        return 'ok';
      } catch (error) {
        lastError = error;
        if (isNotFound(error)) return 'not-found';
        if (attempt < 2) await sleep(500);
      }
    }
    if (this.cfg.vision?.enabled) throw new Error('vision 已启用但配置模型设置失败；拒绝切换默认模型');
    this.log.warn(`设置会话模型失败，将使用 DSH 默认模型：${lastError?.message ?? lastError}`);
    return 'failed';
  }

  async #createSession() {
    const workspaceId = await this.#workspaceId();
    const params = { workspaceId };
    if (this.cfg.dsh.agentPreset) params.agentPreset = this.cfg.dsh.agentPreset;
    const value = unwrap(await this.api.sessions.create(params), 'session.create');
    const sessionId = value?.sessionId;
    if (!sessionId) throw new Error('DSH session.create 未返回 sessionId');
    this.api.events.follow(sessionId);
    const applied = await this.#applyModel(sessionId);
    if (applied === 'not-found') throw new Error(`新会话 ${sessionId} 在 DSH 侧立即不存在`);
    return sessionId;
  }

  async ensureSession(group) {
    const existing = this.store.get(group);
    if (existing) {
      const applied = await this.#applyModel(existing);
      if (applied !== 'not-found') {
        // follow 集合只活在进程内存里（DSH 客户端跨重连保留，但跨进程重启不保留）。
        // 复用旧会话时必须重新 follow，否则这个会话的 turn 事件永远收不到，
        // 表现就是每次 @ 都干等到 turnTimeoutMs 才报超时。
        this.api.events.follow(existing);
        return existing;
      }
      this.log.warn(`群「${group}」的 DSH 会话已不存在，重建：${existing}`);
      this.store.remove(group);
    }
    const sessionId = await this.#createSession();
    this.store.set(group, sessionId);
    this.log.info(`新会话：群「${group}」 -> ${sessionId}（preset ${this.cfg.dsh.agentPreset || '默认'}）`);
    return sessionId;
  }

  #waitStreamReady(timeoutMs) {
    if (this.streamReady) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.readyWaiters.delete(waiter);
        const detail = this.lastStreamError ? `：${this.lastStreamError}` : '';
        reject(new Error(`连接 DSH 事件流超时（${timeoutMs}ms）${detail}`));
      }, timeoutMs);
      const waiter = () => {
        clearTimeout(timer);
        this.readyWaiters.delete(waiter);
        resolve();
      };
      this.readyWaiters.add(waiter);
    });
  }

  #wakeReadyWaiters() {
    for (const waiter of [...this.readyWaiters]) waiter();
  }

  async #pump() {
    while (!this.abort.signal.aborted) {
      try {
        this.log.info('打开 DSH 事件流…');
        const stream = this.api.events.mux({}, this.abort.signal, () => {
          this.streamReady = true;
          this.#wakeReadyWaiters();
        });
        for await (const envelope of stream) {
          this.#handleEnvelope(envelope);
        }
        this.streamReady = false;
        this.log.warn('DSH 事件流已结束；准备重连');
      } catch (error) {
        if (this.abort.signal.aborted) break;
        this.lastStreamError = this.cfg.vision?.enabled ? 'vision 事件流中断（省略上游内容）' : String(error?.message ?? error);
        this.log.warn(`DSH 事件流中断：${this.lastStreamError}`);
      }
      this.streamReady = false;
      if (this.abort.signal.aborted) break;
      await sleep(this.cfg.dsh.streamReconnectMs);
    }
  }

  #handleEnvelope(envelope) {
    const frame = envelope?.payload;
    if (!frame || typeof frame.type !== 'string') return;
    if (frame.type === 'session/event') {
      const waiter = this.waiters.get(frame.sessionId);
      if (waiter) waiter.push(frame.event);
      return;
    }
    if (frame.type === 'session/snapshot') {
      // 开场快照含历史 turn/end，不能当实时事件喂给等待器。
      return;
    }
    if (frame.type === 'approval/requested') {
      const sessionId = frame.sessionId;
      // $events 是宿主级通道，只处理本桥接已跟随、存储或正在等待的会话。
      // 使用客户端的当前 follow 集合，避免 forget 后仍凭历史订阅自动放行。
      const owned = typeof sessionId === 'string' && sessionId.length > 0
        && (this.api._desiredFollows?.has(sessionId)
          || this.waiters.has(sessionId)
          || this.store.entries().some(([, id]) => id === sessionId));
      if (!owned) {
        this.log.warn(`不自动审批工具（未知会话 ${sessionId ?? '缺失'}，工具 ${frame.toolName ?? '未知'}）：非本项目会话，未发送回执`);
        return;
      }
      this.log.info(`自动放行工具审批 allowed-once（本项目会话 ${sessionId}，工具 ${frame.toolName ?? '未知'}）`);
      this.api.respond({
        clientId: frame.clientId,
        eventId: frame.eventId,
        outcome: { kind: 'result', value: 'allowed-once' }
      }).catch((error) => this.log.warn(`回执审批失败：${error?.message ?? error}`));
      return;
    }
    if (frame.type === 'question/requested') {
      this.log.warn(`自动跳过 agent 提问（会话 ${frame.sessionId}）：微信群机器人不接收交互式提问`);
      this.api.respond({
        clientId: frame.clientId,
        eventId: frame.eventId,
        outcome: { kind: 'result', value: { answers: [] } }
      }).catch((error) => this.log.warn(`回执提问失败：${error?.message ?? error}`));
      return;
    }
    if (frame.type === 'stream/error') {
      this.log.warn(this.cfg.vision?.enabled ? 'DSH 会话流错误（vision 已启用，省略上游内容）' : `DSH 会话流错误：${JSON.stringify(frame.error)}`);
    }
  }

  async #promptAndWait(group, sessionId, prompt, images) {
    if (this.stopping) throw new Error('bridge stopping');
    await this.#waitStreamReady(60000);
    if (this.stopping) throw new Error('bridge stopping');
    if (this.waiters.has(sessionId)) {
      throw new Error(`会话 ${sessionId} 已有等待中的回合；同一会话必须串行处理`);
    }
    const waiter = new TurnWaiter({
      sessionId,
      timeoutMs: this.cfg.dsh.turnTimeoutMs,
      label: `会话 ${sessionId}`
    });
    this.waiters.set(sessionId, waiter);
    try {
      let accepted;
      try {
        accepted = await this.api.sessions.prompt({
          sessionId,
          mode: 'queue',
          content: [{ type: 'text', text: prompt }, ...images]
        });
      } catch (error) {
        if (!waiter.collector && /session\/not-found/i.test(String(error?.message ?? error))) {
          this.modelApplied.delete(sessionId);
          throw Object.assign(new Error(`DSH 会话 ${sessionId} 不存在：${error?.message ?? error}`), { sessionGone: true, group });
        }
        throw error;
      }
      if (!accepted?.result?.ok) {
        const code = accepted?.result?.error?.code ?? 'unknown';
        const message = accepted?.result?.error?.message ?? 'unknown error';
        if (!waiter.collector && code === 'session/not-found') {
          this.modelApplied.delete(sessionId);
          throw Object.assign(new Error(`DSH 会话 ${sessionId} 不存在：${code}: ${message}`), { sessionGone: true, group });
        }
        throw new Error(`DSH 拒绝了消息：${code}: ${message}`);
      }
      return await waiter.promise;
    } finally {
      if (!waiter.settled) waiter.reject(new Error('prompt 投递未完成'));
      if (this.waiters.get(sessionId) === waiter) this.waiters.delete(sessionId);
    }
  }

  /**
   * 向群对应的 DSH 会话投递一条 prompt，并等待该回合的最终文本。
   * 只有“会话不存在”会在重建后重试一次；回合中途失败不重试，避免重复回复。
   */
  async ask(group, prompt, { images = [] } = {}) {
    const content = validateImages(images, this.cfg.vision);
    const sessionId = await this.ensureSession(group);
    try {
      return await this.#promptAndWait(group, sessionId, prompt, content);
    } catch (error) {
      if (!error?.sessionGone) throw error;
      this.log.warn(`会话丢失，重建后重试一次：群「${group}」`);
      this.store.remove(group);
      const recreated = await this.ensureSession(group);
      return this.#promptAndWait(group, recreated, prompt, content);
    }
  }
}
