export class CollectorError extends Error {
  constructor(message, { status = 0, code = '' } = {}) {
    super(message);
    this.name = 'CollectorError';
    this.status = status;
    this.code = code;
  }
}

async function readJson(response, maxBytes) {
  let text;
  if (maxBytes) {
    if (Number(response.headers.get('content-length')) > maxBytes) {
      await response.body?.cancel();
      throw new CollectorError('采集端图片响应过大');
    }
    const reader = response.body?.getReader();
    const chunks = [];
    let size = 0;
    if (reader) {
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          size += value.byteLength;
          if (size > maxBytes) {
            await reader.cancel();
            throw new CollectorError('采集端图片响应过大');
          }
          chunks.push(value);
        }
      } finally { reader.releaseLock(); }
    }
    text = Buffer.concat(chunks).toString('utf8');
  } else text = await response.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    throw new CollectorError(`采集端返回了非 JSON 响应（HTTP ${response.status}）`, { status: response.status });
  }
}

/**
 * Windows 采集端的 HTTP 客户端。
 *
 * 采集端只绑定 127.0.0.1，token 是第二道门：本机其它进程也可能读这个端口。
 */
export class CollectorClient {
  constructor({ baseUrl, token, timeoutMs = 30000 }) {
    this.baseUrl = String(baseUrl).replace(/\/+$/, '');
    this.token = token;
    this.timeoutMs = timeoutMs;
  }

  async request(pathname, { method = 'GET', body, timeoutMs, signal, maxResponseBytes } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(new Error('collector request timeout')), timeoutMs ?? this.timeoutMs);
    const onAbort = () => controller.abort(signal.reason);
    signal?.addEventListener('abort', onAbort, { once: true });
    if (signal?.aborted) controller.abort(signal.reason);
    try {
      const response = await fetch(`${this.baseUrl}${pathname}`, {
        method,
        redirect: 'error',
        headers: {
          authorization: `Bearer ${this.token}`,
          ...(body === undefined ? {} : { 'content-type': 'application/json' })
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal
      });
      const payload = await readJson(response, maxResponseBytes);
      if (!response.ok || payload?.ok === false) {
        throw new CollectorError(payload?.error || `采集端 HTTP ${response.status}`, {
          status: response.status,
          code: payload?.code
        });
      }
      return payload;
    } catch (error) {
      if (error instanceof CollectorError) throw error;
      if (signal?.aborted) throw error;
      throw new CollectorError(`采集端请求失败：${error?.message ?? error}`);
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', onAbort);
    }
  }

  health(signal) {
    return this.request('/health', { signal });
  }

  cursor(signal) {
    return this.request('/cursor', { signal });
  }

  poll({ since, waitMs, signal }) {
    const params = new URLSearchParams({ since: String(since ?? 0), waitMs: String(waitMs ?? 0) });
    return this.request(`/poll?${params}`, {
      signal,
      timeoutMs: Math.max(this.timeoutMs, (waitMs ?? 0) + 5000)
    });
  }

  image({ group, imageId, signal }) {
    return this.request('/image', { method: 'POST', body: { group, imageId }, signal,
      maxResponseBytes: Math.ceil(5242880 / 3) * 4 + 1024,
      timeoutMs: Math.max(this.timeoutMs, 50000) });
  }

  sendSticker({ group, stickerId, signal }) {
    return this.request('/send-sticker', {
      method: 'POST',
      body: { group, stickerId },
      signal,
      timeoutMs: Math.max(this.timeoutMs, 50000)
    });
  }

  sendTts({ group, text, signal }) {
    // 合成在采集端做（mp3 不出回环），这里只给文字。
    return this.request('/send-tts', {
      method: 'POST',
      body: { group, text },
      signal,
      timeoutMs: Math.max(this.timeoutMs, 60000)
    });
  }

  send({ group, text, at, signal }) {
    return this.request('/send', {
      method: 'POST',
      body: { group, text, at: at || null },
      signal
    });
  }
}
