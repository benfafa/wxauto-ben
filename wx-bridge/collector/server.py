"""wxauto 采集端的 HTTP 服务。

设计要点：
- 所有 wxauto 调用都放在一个 UI worker 线程里串行执行；HTTP 线程只读写内存队列。
- 事件带单调递增 seq，客户端用游标增量拉取；长轮询减少空转。
- 所有发送都走同一条 UI worker 队列，避免“轮询窗口”和“发送窗口”互相抢焦点。
"""

from __future__ import annotations


import hmac
import json
import logging
import queue
import shutil
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from collector.stickers import StickerCatalog, StickerError
from collector.inbox import Inbox
from collector.labeler import StickerLabeler
from collector.tts import TtsError, normalize_tts, synthesize
from collector.vision import ImageCache


MAX_EVENTS = 5000
MAX_BODY = 64 * 1024
MAX_TEXT = 4000


def _iso(timestamp):
    if not timestamp:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass
class SendJob:
    group: str
    text: str
    at: str | None
    future: Future


@dataclass
class StickerJob:
    group: str
    sticker_id: str
    future: Future


@dataclass
class ImageJob:
    group: str
    image_id: str
    future: Future


@dataclass
class TtsJob:
    group: str
    path: Path
    folder: Path
    size: int
    future: Future


class CollectorService:
    def __init__(self, backend, token, *, allowed_groups=None, poll_ms=800,
                 host="127.0.0.1", port=0, allow_inject=False, stickers=None, vision=None, tts=None,
                 inbox=None, labeler=None):
        if not token:
            raise ValueError("collector token 不能为空")
        # backend 可以是实例（fake/测试），也可以是工厂函数（wxauto 必须在 UI 线程里构造，
        # 否则 comtypes 的 COM 对象会跨 apartment 使用）。
        self._backend_source = backend
        self.backend = None
        self._backend_ready = threading.Event()
        self._backend_init_error = None
        if not callable(backend):
            self.backend = backend
            self._backend_ready.set()
        self.token = token
        self.allowed_groups = list(allowed_groups or getattr(backend, "groups", []))
        if not self.allowed_groups:
            raise ValueError("allowed_groups 不能为空")
        self.poll_ms = max(100, int(poll_ms))
        self.host = host
        self.port = int(port)
        self.allow_inject = allow_inject
        self.stickers = stickers if stickers is not None else StickerCatalog()
        self.images = ImageCache(vision)
        self.tts = tts if tts is not None else normalize_tts()
        self.inbox = inbox if inbox is not None else Inbox()
        self.labeler = labeler

        self.collector_id = uuid.uuid4().hex
        self._events = deque(maxlen=MAX_EVENTS)
        self._next_seq = 1
        self._cursor = 0
        self._condition = threading.Condition()
        self._jobs = queue.Queue()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._ui_loop, name="wxauto-ui", daemon=True)
        self._httpd = None
        self._http_thread = None
        self._last_poll_at = None
        self._last_error = None
        self._send_count = 0
        self._lock = threading.Lock()

    # ── lifecycle ────────────────────────────────────────────────
    def start(self):
        self._worker.start()
        self._httpd = ThreadingHTTPServer((self.host, self.port), self._handler_class())
        self._httpd.daemon_threads = True
        self._httpd.allow_reuse_address = True
        self.port = self._httpd.server_address[1]
        self._http_thread = threading.Thread(target=self._httpd.serve_forever, name="collector-http", daemon=True)
        self._http_thread.start()
        logging.info("collector listening on http://%s:%s (backend=%s, groups=%s)",
                     self.host, self.port, getattr(self.backend, "name", "unknown"), self.allowed_groups)
        return self

    def request_stop(self):
        self._stop.set()
        self._jobs.put(None)
        with self._condition:
            self._condition.notify_all()

    def stop(self):
        self.request_stop()
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        self._worker.join(timeout=2)

    # ── UI worker ────────────────────────────────────────────────
    def wait_backend_ready(self, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._backend_ready.wait(0.5):
                return True
            if self._stop.is_set():
                return self._backend_ready.is_set()
        return False

    @property
    def backend_init_error(self):
        return self._backend_init_error

    def _ui_loop(self):
        comtypes = None
        if self.labeler is not None:
            self.labeler.start()
        try:
            # wxauto4 uses COM UI Automation. Explicitly initialize the
            # worker apartment; constructing it without this works on the
            # main thread but fails here with RPC_E_CALL_REJECTED-like errors.
            import comtypes as _comtypes
            comtypes = _comtypes
            comtypes.CoInitialize()
        except ImportError:
            pass
        try:
            if self.backend is None:
                try:
                    self.backend = self._backend_source() if callable(self._backend_source) else self._backend_source
                except Exception as error:
                    self._backend_init_error = f"{type(error).__name__}: {error}"
                    logging.exception("backend init failed")
                    self._backend_ready.set()
                    return
                self._backend_ready.set()
            self.backend.vision_enabled = self.images.config["enabled"] or self.inbox.cfg["enabled"]
            if self.backend.vision_enabled and getattr(self.backend, "mode", "") == "legacy":
                logging.warning("legacy backend does not support image download; vision will degrade to text")
            while not self._stop.is_set():
                try:
                    job = self._jobs.get(timeout=self.poll_ms / 1000)
                except queue.Empty:
                    self._poll_once()
                    continue
                if job is None:
                    break
                if isinstance(job, ImageJob):
                    if job.future.set_running_or_notify_cancel():
                        try:
                            job.future.set_result(self.images.get(job.group, job.image_id))
                        except Exception:
                            # Do not expose paths, downloaded bytes or backend exception data.
                            logging.warning("image retrieval failed: group=%s imageId=%s", job.group, job.image_id)
                            job.future.set_exception(ValueError("image-unavailable: 未能读取图片"))
                elif isinstance(job, (SendJob, StickerJob, TtsJob)):
                    self._handle_send(job)
        finally:
            self.images.close()
            if self.labeler is not None:
                self.labeler.stop()
            if comtypes is not None:
                comtypes.CoUninitialize()

    def _poll_once(self):
        self.images.prune()
        try:
            messages = self.backend.poll()
        except Exception as error:
            self._last_error = f"{type(error).__name__}: {error}"
            logging.exception("backend poll failed")
            return
        if messages:
            now = time.time()
            with self._condition:
                for message in messages:
                    event = dict(message)
                    image_message = event.pop("_imageMessage", None)
                    event.pop("imageId", None)
                    if event.get("group") not in self.allowed_groups:
                        continue
                    if self.images.config["enabled"] and event.get("kind") in ("image", "sticker"):
                        event["imageId"] = self.images.register(event["group"], image_message)
                    if image_message is not None and self.inbox.cfg["enabled"]:
                        try:
                            saved = self.inbox.save(
                                image_message, event["group"], event.get("sender"),
                                kind="sticker" if event.get("kind") == "sticker" else "photo")
                            if saved:
                                logging.info("inbox saved: group=%s kind=%s bytes=%s",
                                             event["group"], saved["kind"], saved["bytes"])
                                if saved["kind"] == "sticker" and self.labeler is not None:
                                    self.labeler.submit(saved)
                        except Exception:
                            # 收图失败不该影响消息投递。
                            logging.warning("inbox save failed: group=%s", event.get("group"))
                    event.setdefault("id", f"event-{uuid.uuid4().hex}")
                    event["seq"] = self._next_seq
                    self._next_seq += 1
                    event["timestamp"] = event.get("timestamp") or now
                    self._events.append(event)
                    self._cursor = event["seq"]
                self._condition.notify_all()
        self._last_poll_at = time.time()
        self._last_error = None

    def _handle_send(self, job):
        if not job.future.set_running_or_notify_cancel():
            logging.warning("send job expired before the UI worker started: group=%s", job.group)
            if isinstance(job, TtsJob):
                shutil.rmtree(job.folder, ignore_errors=True)
            return
        sticker_id = job.sticker_id if isinstance(job, StickerJob) else None
        try:
            if isinstance(job, TtsJob):
                result = self.backend.send_file(job.group, str(job.path), what="语音")
                if not isinstance(result, dict) or result.get("ok") is not True:
                    raise RuntimeError("voice send was not confirmed")
                logging.info("voice sent: group=%s bytes=%s", job.group, job.size)
                result = {"ok": True, "bytes": job.size}
            elif isinstance(job, StickerJob):
                file = self.stickers.validate(sticker_id)
                result = self.backend.send_sticker(job.group, str(file), sticker_id)
                if not isinstance(result, dict) or result.get("ok") is not True:
                    raise RuntimeError("sticker send was not confirmed")
                logging.info("sticker sent: group=%s stickerId=%s", job.group, sticker_id)
                result = {"ok": True, "stickerId": sticker_id}
            else:
                result = self.backend.send(job.group, job.text, job.at)
            if result is False or (isinstance(result, dict) and result.get("ok") is False):
                raise RuntimeError("backend reported send failure")
            with self._lock:
                self._send_count += 1
            job.future.set_result(result or {"ok": True})
        except Exception as error:
            logging.exception("backend send failed: group=%s stickerId=%s", job.group, sticker_id)
            job.future.set_exception(error)
        finally:
            if isinstance(job, TtsJob):
                # 合成出来的临时目录只活到这一条发送结束。
                shutil.rmtree(job.folder, ignore_errors=True)

    # ── API operations ───────────────────────────────────────────
    def health(self):
        if self.backend is None:
            return {
                "ok": False,
                "code": "backend-init-failed" if self._backend_init_error else "starting",
                "error": self._backend_init_error or "backend starting",
                "groups": self.allowed_groups,
                "backend": "starting",
            }
        backend_health = {}
        try:
            backend_health = self.backend.health() or {}
        except Exception as error:
            backend_health = {"healthError": f"{type(error).__name__}: {error}"}
        with self._condition:
            queued = len(self._events)
            cursor = self._cursor
        with self._lock:
            send_count = self._send_count
        return {
            "ok": True,
            "collectorId": self.collector_id,
            "cursor": cursor,
            "queuedEvents": queued,
            "lastPollAt": _iso(self._last_poll_at),
            "lastError": self._last_error,
            "sendCount": send_count,
            "backend": getattr(self.backend, "name", "unknown"),
            "selfNickname": backend_health.get("selfNickname", ""),
            "groups": self.allowed_groups,
            "backendHealth": backend_health,
            "inbox": self.inbox.status(),
        }

    def cursor(self):
        with self._condition:
            return {"ok": True, "collectorId": self.collector_id, "cursor": self._cursor}

    def poll(self, since, wait_ms=0, limit=200):
        since = int(since)
        wait_ms = max(0, min(int(wait_ms), 60000))
        limit = max(1, min(int(limit), 500))
        deadline = time.monotonic() + wait_ms / 1000
        with self._condition:
            while self._cursor <= since and not self._stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(timeout=min(remaining, 1.0))
            all_events = [event for event in self._events if event["seq"] > since]
            events = all_events[:limit]
            oldest = self._events[0]["seq"] if self._events else None
            cursor = events[-1]["seq"] if events else self._cursor
        return {
            "ok": True,
            "collectorId": self.collector_id,
            "cursor": cursor,
            "events": events,
            "oldestSeq": oldest,
            "lost": bool(oldest is not None and since and since < oldest - 1),
        }

    def send(self, group, text, at=None):
        group = str(group or "").strip()
        text = str(text or "")
        at = str(at).strip() if at else None
        if self.backend is None:
            raise ApiError(503, "backend-not-ready", self._backend_init_error or "backend starting")
        if group not in self.allowed_groups:
            raise ApiError(403, "group-not-allowed", f"群不在白名单：{group}")
        if not text.strip():
            raise ApiError(400, "empty-text", "text 不能为空")
        if len(text) > MAX_TEXT:
            raise ApiError(400, "text-too-long", f"text 最多 {MAX_TEXT} 个字符")
        if at and len(at) > 100:
            raise ApiError(400, "at-too-long", "@ 对象过长")
        return self._submit_send(SendJob(group=group, text=text, at=at, future=Future()))

    def send_sticker(self, group, sticker_id):
        group = str(group or "").strip()
        if group not in self.allowed_groups:
            raise ApiError(403, "group-not-allowed", f"群不在白名单：{group}")
        self.stickers.validate(sticker_id)
        if self.backend is None:
            raise ApiError(503, "backend-not-ready", self._backend_init_error or "backend starting")
        return self._submit_send(StickerJob(group=group, sticker_id=sticker_id, future=Future()))

    def send_tts(self, group, text):
        group = str(group or "").strip()
        if group not in self.allowed_groups:
            raise ApiError(403, "group-not-allowed", f"群不在白名单：{group}")
        if not self.tts["enabled"]:
            raise ApiError(403, "tts-disabled", "语音回复未启用")
        if self.backend is None:
            raise ApiError(503, "backend-not-ready", self._backend_init_error or "backend starting")
        if not isinstance(text, str) or not text.strip():
            raise ApiError(400, "empty-text", "text 不能为空")
        if len(text) > self.tts["maxChars"] * 4:
            raise ApiError(400, "text-too-long", "text 过长")
        try:
            path, folder = synthesize(self.tts, text)
        except TtsError as error:
            raise ApiError(502, "tts-failed", str(error)) from error
        size = path.stat().st_size
        return self._submit_send(TtsJob(group=group, path=path, folder=folder, size=size, future=Future()))

    def image(self, group, image_id):
        if not isinstance(group, str) or group not in self.allowed_groups:
            raise ApiError(403, "group-not-allowed", "群不在白名单")
        if not self.images.config["enabled"]:
            raise ApiError(403, "vision-disabled", "图片采集未启用")
        if not isinstance(image_id, str) or len(image_id) != 32 or any(c not in "0123456789abcdef" for c in image_id):
            raise ApiError(400, "invalid-image-id", "imageId 格式非法")
        if self.backend is None:
            raise ApiError(503, "backend-not-ready", "backend starting")
        return self._submit_send(ImageJob(group, image_id, Future()))

    def _submit_send(self, job):
        self._jobs.put(job)
        try:
            return job.future.result(timeout=45)
        except FutureTimeout:
            job.future.cancel()
            if isinstance(job, ImageJob):
                raise ApiError(504, "image-timeout", "等待微信 UI 图片下载超时")
            raise ApiError(504, "send-timeout", "等待微信 UI 发送超时")
        except StickerError:
            raise
        except ValueError as error:
            if isinstance(job, ImageJob):
                raise ApiError(400, "image-unavailable", "未能读取图片")
            raise ApiError(400, "send-rejected", str(error))
        except Exception as error:
            raise ApiError(500, "send-failed", f"{type(error).__name__}: {error}")

    def inject(self, message):
        if not self.allow_inject:
            raise ApiError(404, "not-found", "inject 只在 fake 后端下可用")
        if self.backend is None:
            raise ApiError(503, "backend-not-ready", self._backend_init_error or "backend starting")
        inject = getattr(self.backend, "inject", None)
        if not callable(inject):
            raise ApiError(400, "unsupported", "当前后端不支持 inject")
        group = str(message.get("group") or "").strip()
        if group not in self.allowed_groups:
            raise ApiError(403, "group-not-allowed", f"群不在白名单：{group}")
        text = message.get("text")
        if not isinstance(text, str) or not text:
            raise ApiError(400, "invalid-text", "text 必须是非空字符串")
        inject({
            "group": group,
            "sender": str(message.get("sender") or "测试用户"),
            "text": text,
            "id": str(message.get("id") or f"inject-{uuid.uuid4().hex}"),
            "kind": str(message.get("kind") or "text"),
        })
        return {"ok": True}

    # ── HTTP ─────────────────────────────────────────────────────
    def _handler_class(self):
        service = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                logging.debug("%s - %s", self.address_string(), fmt % args)

            def _send_json(self, status, payload):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.send_header("cache-control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _error(self, error):
                if isinstance(error, (ApiError, StickerError)):
                    self._send_json(error.status, {"ok": False, "code": error.code, "error": str(error)})
                else:
                    logging.exception("unhandled collector error")
                    self._send_json(500, {"ok": False, "code": "internal", "error": str(error)})

            def _authorized(self):
                header = self.headers.get("authorization", "")
                if not header.startswith("Bearer "):
                    return False
                return hmac.compare_digest(header[7:].strip(), service.token)

            def _require_auth(self):
                if self._authorized():
                    return
                raise ApiError(401, "unauthorized", "缺少或错误的 Bearer token")

            def _read_json(self):
                try:
                    length = int(self.headers.get("content-length", "0"))
                except ValueError:
                    raise ApiError(400, "bad-length", "content-length 非法")
                if length <= 0 or length > MAX_BODY:
                    raise ApiError(400, "bad-body", "请求体为空或过大")
                raw = self.rfile.read(length)
                try:
                    data = json.loads(raw.decode("utf-8"))
                except Exception:
                    raise ApiError(400, "bad-json", "请求体不是合法 JSON")
                if not isinstance(data, dict):
                    raise ApiError(400, "bad-json", "请求体必须是对象")
                return data

            def do_GET(self):
                try:
                    self._require_auth()
                    parsed = urlparse(self.path)
                    if parsed.path == "/health":
                        payload = service.health()
                        self._send_json(200 if payload.get("ok") else 503, payload)
                        return
                    if parsed.path == "/cursor":
                        self._send_json(200, service.cursor())
                        return
                    if parsed.path == "/poll":
                        qs = parse_qs(parsed.query)
                        try:
                            since = int(qs.get("since", ["0"])[0])
                            wait_ms = int(qs.get("waitMs", ["0"])[0])
                            limit = int(qs.get("limit", ["200"])[0])
                        except ValueError:
                            raise ApiError(400, "bad-query", "since / waitMs / limit 必须是整数")
                        self._send_json(200, service.poll(since, wait_ms, limit))
                        return
                    raise ApiError(404, "not-found", "未知路径")
                except Exception as error:
                    self._error(error)

            def do_POST(self):
                try:
                    self._require_auth()
                    parsed = urlparse(self.path)
                    if parsed.path == "/send":
                        body = self._read_json()
                        result = service.send(body.get("group"), body.get("text"), body.get("at"))
                        self._send_json(200, {"ok": True, **(result or {})})
                        return
                    if parsed.path == "/image":
                        body = self._read_json()
                        if set(body) != {"group", "imageId"}:
                            raise ApiError(400, "bad-image-body", "仅允许 group 和 imageId")
                        self._send_json(200, service.image(body["group"], body["imageId"]))
                        return
                    if parsed.path == "/send-sticker":
                        body = self._read_json()
                        if set(body) != {"group", "stickerId"}:
                            raise ApiError(400, "bad-sticker-body", "仅允许 group 和 stickerId")
                        result = service.send_sticker(body["group"], body["stickerId"])
                        self._send_json(200, result)
                        return
                    if parsed.path == "/send-tts":
                        body = self._read_json()
                        if set(body) != {"group", "text"}:
                            raise ApiError(400, "bad-tts-body", "仅允许 group 和 text")
                        self._send_json(200, service.send_tts(body["group"], body["text"]))
                        return
                    if parsed.path == "/inject":
                        body = self._read_json()
                        self._send_json(200, service.inject(body))
                        return
                    raise ApiError(404, "not-found", "未知路径")
                except Exception as error:
                    self._error(error)

        return Handler
