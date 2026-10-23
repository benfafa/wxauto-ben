#!/usr/bin/env python3
"""启动微信采集端。

生产（Windows）：
    cd wx-bridge
    python -m collector.wx_collector --config config.json

本机联调（macOS/Linux，无微信）：
    python -m collector.wx_collector --backend fake --groups 测试群 \
        --token 0123456789abcdef0123456789abcdef --allow-inject
"""

import argparse
import json
import logging
import secrets
import signal
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

# 让 `python -m collector.wx_collector` 与直接运行脚本都能 import 本包。
HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

from collector.server import CollectorService  # noqa: E402
from collector.backends.fake_backend import FakeBackend  # noqa: E402
from collector.stickers import StickerCatalog  # noqa: E402
from collector.vision import vision_from_file  # noqa: E402


def load_wechat_config(path):
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    data = json.loads(text)
    wechat = data.get("wechat") if isinstance(data, dict) else None
    return wechat if isinstance(wechat, dict) else {}


def parse_args(argv):
    parser = argparse.ArgumentParser(description="WeChat collector for wx-bridge")
    parser.add_argument("--config", default="config.json", help="与 bridge 共用的 config.json")
    parser.add_argument("--backend", choices=["wxauto", "fake"], default="wxauto")
    parser.add_argument("--groups", default="", help="逗号分隔的群名；不填则读 config.wechat.groups")
    parser.add_argument("--token", default="", help="覆盖 config.wechat.token")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None, help="不填则从 collectorUrl 推导（默认 8765）；0 表示随机空闲端口")
    parser.add_argument("--poll-ms", type=int, default=0, help="0 表示读 config.wechat.pollMs")
    parser.add_argument("--send-log", default="", help="fake 后端把发送记录写到这里")
    parser.add_argument("--allow-inject", action="store_true", help="fake 后端开放 POST /inject")
    parser.add_argument("--allow-remote", action="store_true", help="允许绑定非回环地址（token 会暴露到网络）")
    return parser.parse_args(argv)


def is_loopback(host):
    return host in ("127.0.0.1", "localhost", "::1")


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    wechat = load_wechat_config(args.config)
    stickers = StickerCatalog.from_config_file(args.config)
    collector_url = str(wechat.get("collectorUrl") or "http://127.0.0.1:8765")
    parsed = urlparse(collector_url)
    port = args.port if args.port is not None else (parsed.port or 8765)

    groups = [item.strip() for item in args.groups.split(",") if item.strip()]
    if not groups:
        groups = [str(item).strip() for item in (wechat.get("groups") or []) if str(item).strip()]
    if not groups:
        raise SystemExit("没有配置群：请在 config.json 的 wechat.groups 填写，或使用 --groups")

    token = (args.token or str(wechat.get("token") or "")).strip()
    if not token or token.upper().startswith("REPLACE"):
        if args.backend == "fake":
            token = secrets.token_hex(16)
            print(f"[collector] fake 后端临时 token：{token}", flush=True)
        else:
            raise SystemExit("wechat.token 仍是空值/占位符；请生成随机 token 并同时填到 config.json 与 bridge")

    if not is_loopback(args.host) and not args.allow_remote:
        raise SystemExit(f"拒绝绑定非回环地址 {args.host}：采集端没有 TLS，token 会明文暴露。确实需要时加 --allow-remote，并由网络层保护")

    poll_ms = args.poll_ms or int(wechat.get("pollMs") or 800)

    if args.backend == "fake":
        backend_source = FakeBackend(groups, send_log=args.send_log or None)
    else:
        def backend_source():
            # 必须在这个 UI worker 线程里构造 wxauto/COM 对象。
            from collector.backends.wxauto_backend import WxAutoBackend
            return WxAutoBackend(groups)

    service = CollectorService(
        backend_source,
        token,
        allowed_groups=groups,
        poll_ms=poll_ms,
        host=args.host,
        port=port,
        allow_inject=args.allow_inject and args.backend == "fake",
        stickers=stickers,
        vision=vision_from_file(args.config),
    )

    stop = threading.Event()

    def _stop(signum, _frame):
        logging.info("收到信号 %s，退出中…", signum)
        stop.set()
        service.request_stop()

    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is not None:
            try:
                signal.signal(sig, _stop)
            except (ValueError, OSError):
                pass

    service.start()
    if not service.wait_backend_ready(60):
        service.stop()
        if stop.is_set():
            return 0
        raise SystemExit("等待微信 UI 初始化超时（60 秒）")
    if service.backend is None:
        service.stop()
        raise SystemExit(f"启动微信 UI 自动化失败：{service.backend_init_error or '未知错误'}。请确认微信已登录、版本为 3.9.11.17，且运行在 Windows")
    health = service.health()
    print(f"[collector] url=http://{args.host}:{service.port}", flush=True)
    print(f"[collector] backend={health['backend']} nickname={health.get('selfNickname')!r}", flush=True)
    print(f"[collector] listening={health['groups']}", flush=True)
    if health.get("backendHealth", {}).get("errors"):
        print(f"[collector] 部分群监听失败：{health['backendHealth']['errors']}", flush=True)
    if args.allow_inject:
        print("[collector] POST /inject 已开启（仅 fake 后端）", flush=True)

    try:
        while not stop.is_set():
            time.sleep(0.5)
    finally:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
