# -*- coding: utf-8 -*-
"""小音桌面宠物 —— 透明无边框置顶窗口（pywebview + Live2D 双模型 + 像素散步动画）。

用法：
    python pet.py                        # 本机（需先 python app.py 启动服务器）
    python pet.py --host 192.168.1.8     # 连远程服务器
    python pet.py --scale 1.2 --x 100 --y 100   # 缩放/手动定位
    python pet.py --no-on-top            # 不置顶

线程模型：
- 主线程 = pywebview GUI 事件循环（webview.start() 阻塞）。
- WS 线程 = asyncio 事件循环（连接 /ws/assistant、麦克风推流、事件分发、mp3 播放）。
- UI 桥 = queue.Queue：WS 线程 push 状态，JS 每 120ms 经 js_api pull_state() 拉取。
  js_api 方法在 pywebview 独立线程执行且非线程安全，但 queue.Queue 本身线程安全；
  全程不使用 evaluate_js（主线程调用会死锁）。
"""

import argparse
import asyncio
import json
import queue
import threading
import time
from pathlib import Path

PET_DIR = Path(__file__).parent / "web" / "pet"


# ── 纯函数：WS 事件 → (宠物状态, 气泡文本)，None 表示不变（可离线测试）──

def event_to_pet_state(ev: dict):
    t = ev.get("type")
    if t == "ready":
        return ("idle", "")
    if t == "wake":
        return ("listening", "")
    if t == "transcript":
        return ("thinking", ev.get("text", ""))
    if t == "sentence":
        return ("speaking", ev.get("text", ""))
    if t == "barge_in":
        return ("listening", "")
    if t == "speaker_reject":
        return ("reject", ev.get("text", ""))
    if t == "turn_end":
        return ("idle", ev.get("status", ""))
    if t == "status":
        return (None, ev.get("text", ""))
    if t == "error":
        return ("idle", ev.get("error", ""))
    return (None, None)


# ── UI 桥（唯一跨线程通道）──────────────────────────────────

class UiBridge:
    def __init__(self):
        self._q: queue.Queue = queue.Queue(maxsize=64)
        self._pending = queue.Queue()          # 连接建立前的控制消息暂存
        self._ctrl_q: asyncio.Queue = None     # 绑定 WS 线程 loop 后创建
        self._loop = None
        self.stop_event = threading.Event()    # 退出信号（JS/窗口关闭/WS 共用）

    def bind_loop(self, loop):
        self._loop = loop

    def push_state(self, state, text=""):
        self._push({"state": state, "text": text})

    def _push(self, item):
        while True:                            # 满则丢最旧，永不阻塞 WS 线程
            try:
                self._q.put_nowait(item)
                return
            except queue.Full:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    pass

    def pull_state(self):
        """JS 轮询入口（js_api 线程调用）：排空并返回最新一条。"""
        latest = None
        while True:
            try:
                latest = self._q.get_nowait()
            except queue.Empty:
                return latest

    def submit_control(self, ctrl: dict):
        """JS 动作 → WS 线程发送控制消息；未连接时暂存，连接后补发。"""
        if self._loop is None or self._ctrl_q is None:
            self._pending.put_nowait(ctrl)
        else:
            asyncio.run_coroutine_threadsafe(self._ctrl_q.put(ctrl), self._loop)


# ── JS 可调 API ──────────────────────────────────────────────
# ⚠️ pywebview 会递归遍历 js_api 对象的所有属性（get_functions），
#    因此 PetApi 只能包含方法；bridge/window 等状态放模块级 _GLOBALS。

_GLOBALS = {}

# 宠物本地配置（缩放比例等，持久化到 data/pet_config.json）
_PET_CONFIG_FILE = Path(__file__).parent / "data" / "pet_config.json"
_SCALE_MIN, _SCALE_MAX, _SCALE_DEFAULT = 0.5, 3.0, 1.0

# 模型注册表：模型名 → (页面文件, 说明)。cubism4 用 pet.html，cubism2 用 pet2.html
_PET_MODELS = [
    ("senko", "pet.html", "仙狐精灵"),
    ("hijiki", "pet.html", "黑猫精灵"),
    ("pio", "pet2.html", "Pio 小精灵"),
]
_MODEL_NAMES = [m[0] for m in _PET_MODELS]


def _load_pet_config() -> dict:
    try:
        import json as _json

        return _json.loads(_PET_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_pet_config(cfg: dict):
    try:
        import json as _json

        _PET_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PET_CONFIG_FILE.write_text(
            _json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


class PetApi:
    def pull_state(self):
        return _GLOBALS["bridge"].pull_state()

    def get_config(self):
        cfg = _load_pet_config()
        scale = float(cfg.get("scale", _SCALE_DEFAULT))
        model = str(cfg.get("model", "senko"))
        if model not in _MODEL_NAMES:
            model = "senko"
        return {
            "scale": max(_SCALE_MIN, min(_SCALE_MAX, scale)),
            "model": model,
            "models": _MODEL_NAMES,
        }

    def save_scale(self, scale):
        try:
            scale = max(_SCALE_MIN, min(_SCALE_MAX, float(scale)))
        except (TypeError, ValueError):
            return
        cfg = _load_pet_config()
        cfg["scale"] = round(scale, 3)
        _save_pet_config(cfg)

    def switch_model(self):
        """切换宠物模型：保存选择后重载对应页面（cubism2/4 不同运行时）。"""
        cfg = _load_pet_config()
        current = str(cfg.get("model", "senko"))
        if current not in _MODEL_NAMES:
            current = "senko"
        nxt = _MODEL_NAMES[(_MODEL_NAMES.index(current) + 1) % len(_MODEL_NAMES)]
        cfg["model"] = nxt
        _save_pet_config(cfg)
        page = dict((m[0], m[1]) for m in _PET_MODELS)[nxt]
        try:
            _GLOBALS["window"].load_url(
                _GLOBALS["base_url"] + f"/{page}?model={nxt}"
            )
        except Exception:
            pass

    def log_error(self, msg):
        """前端错误上报（调试用）：打印到服务器端日志。"""
        print(f"[宠物前端错误] {msg}", flush=True)

    def listen(self):
        _GLOBALS["bridge"].submit_control({"type": "listen"})

    def stop(self):
        _GLOBALS["bridge"].submit_control({"type": "stop"})

    def toggle_on_top(self):
        window = _GLOBALS["window"]
        window.on_top = not window.on_top

    def move_by(self, dx, dy):
        """按住猫身拖动窗口（屏幕像素增量）。"""
        try:
            window = _GLOBALS["window"]
            window.move(window.x + int(dx), window.y + int(dy))
        except Exception:
            pass

    def move_to(self, x, y):
        """绝对定位窗口（自动散步用）。"""
        try:
            _GLOBALS["window"].move(int(x), int(y))
        except Exception:
            pass

    def get_pos(self):
        """返回窗口当前位置 [x, y]。"""
        try:
            w = _GLOBALS["window"]
            return [w.x, w.y]
        except Exception:
            return [0, 0]

    def exit_app(self):
        _GLOBALS["bridge"].stop_event.set()
        _GLOBALS["window"].destroy()           # 后台线程 destroy：pywebview 同步 API，合法


# ── WS 线程 ─────────────────────────────────────────────────

async def recv_loop(ws, player, bridge: UiBridge):
    from websockets.exceptions import ConnectionClosed

    try:
        async for msg in ws:
            if isinstance(msg, bytes):
                player.enqueue(msg)            # audio 事件后的 mp3 帧
                continue
            try:
                ev = json.loads(msg)
            except ValueError:
                continue
            if ev.get("type") == "barge_in":
                player.stop()
            state, text = event_to_pet_state(ev)
            if state is not None or text:
                bridge.push_state(state, text)
    except ConnectionClosed:
        pass  # 连接断开由主循环负责重连


async def ws_main(bridge: UiBridge, args):
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed
    from ws_audio import PygamePlayer, make_mic_stream

    # 子任务在连接断开时静默退出（重连由本主循环负责）
    async def _safe_send(ws, data):
        try:
            await ws.send(data)
        except ConnectionClosed:
            raise asyncio.CancelledError  # 结束该子任务

    bridge._ctrl_q = asyncio.Queue()
    bridge.bind_loop(asyncio.get_running_loop())
    while not bridge._pending.empty():         # 补发连接前的点击
        try:
            bridge._ctrl_q.put_nowait(bridge._pending.get_nowait())
        except queue.Empty:
            break

    player = PygamePlayer()                    # mixer 在 WS 线程初始化（与 client.py 同模式）
    stop = bridge.stop_event
    try:
        while not stop.is_set():
            try:
                async with connect(f"ws://{args.host}:{args.port}/ws/assistant") as ws:
                    await ws.send(json.dumps({"type": "hello", "voice": args.voice or ""}))
                    bridge.push_state("idle")
                    loop = asyncio.get_running_loop()
                    mic_q: asyncio.Queue = asyncio.Queue()
                    stream = make_mic_stream(loop, mic_q)
                    stream.start()

                    async def sender():
                        while True:
                            data = await mic_q.get()
                            await _safe_send(ws, data)

                    async def ctrl_sender():
                        while True:
                            ctrl = await bridge._ctrl_q.get()
                            await _safe_send(ws, json.dumps(ctrl))

                    ptask = asyncio.create_task(player.run())
                    tasks = [
                        asyncio.create_task(sender()),
                        asyncio.create_task(ctrl_sender()),
                        asyncio.create_task(recv_loop(ws, player, bridge)),
                        asyncio.create_task(asyncio.to_thread(stop.wait)),
                    ]
                    # 等任一结束：子任务退出 = 连接断开（外层重连）；stop 置位 = 用户退出
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for t in tasks:
                        if t not in done:
                            t.cancel()
                    ptask.cancel()
                    stream.stop()
                    stream.close()
                    if stop.is_set():
                        break
            except ConnectionClosed:
                bridge.push_state("sleep", "连接断开，重连中…")
            except OSError as e:
                bridge.push_state("sleep", f"无法连接服务器：{e}")
            await asyncio.sleep(2.0)
    finally:
        import pygame

        try:
            pygame.mixer.quit()
        except Exception:
            pass


# ── 静态文件服务器（Live2D 模型/纹理需经 HTTP 正常加载）────

def start_static_server(port: int = 0) -> tuple:
    """在本机起一个仅监听 127.0.0.1 的静态服务器（线程内运行），返回 (server, base_url)。"""
    import functools
    import http.server

    class _NoCacheHandler(http.server.SimpleHTTPRequestHandler):
        """禁用缓存：避免 WebView2 加载旧版页面（页面迭代频繁）。"""

        def end_headers(self):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            super().end_headers()

    handler = functools.partial(_NoCacheHandler, directory=str(PET_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def main():
    import webview

    parser = argparse.ArgumentParser(description="小音桌面宠物（连接语音助手服务器）")
    parser.add_argument("--host", default="127.0.0.1", help="服务器地址")
    parser.add_argument("--port", type=int, default=7860, help="服务器端口")
    parser.add_argument("--voice", default="", help="TTS 音色（可选）")
    parser.add_argument("--scale", type=float, default=1.0, help="窗口缩放（1.0=320x400）")
    parser.add_argument("--x", type=int, default=None, help="窗口 X（默认右下角）")
    parser.add_argument("--y", type=int, default=None)
    parser.add_argument("--no-on-top", action="store_true", help="不置顶")
    args = parser.parse_args()

    W, H = int(250 * args.scale), int(312 * args.scale)
    screens = webview.screens
    primary = (
        screens[0] if screens else type("S", (), {"width": 1920, "height": 1080})()
    )
    x = args.x if args.x is not None else (primary.width - W - 40)
    y = args.y if args.y is not None else (primary.height - H - 80)  # 留任务栏余量

    bridge = UiBridge()
    api = PetApi()
    static_server, base_url = start_static_server()
    cfg = _load_pet_config()
    start_model = str(cfg.get("model", "senko"))
    if start_model not in _MODEL_NAMES:
        start_model = "senko"
    start_page = dict((m[0], m[1]) for m in _PET_MODELS)[start_model]
    window = webview.create_window(
        "小音",
        url=f"{base_url}/{start_page}?model={start_model}",
        x=x, y=y, width=W, height=H,
        frameless=True, transparent=True, on_top=not args.no_on_top,
        easy_drag=False,                      # 整窗拖会吞点击；拖拽限定顶部把手条
        background_color="#000000",           # 仅加载前底色；透明由 transparent=True 处理
        js_api=api,
    )
    _GLOBALS["bridge"] = bridge
    _GLOBALS["window"] = window
    _GLOBALS["static_server"] = static_server
    _GLOBALS["base_url"] = base_url

    threading.Thread(target=lambda: asyncio.run(ws_main(bridge, args)), daemon=True).start()

    def _win_tweaks():
        """窗口级调优：透明触发 + 不抢焦点（挂件化）。"""
        import ctypes

        hwnd = 0
        for _ in range(60):                    # 轮询直到窗口创建
            time.sleep(0.2)
            try:
                hwnd = ctypes.windll.user32.FindWindowW(None, "小音")
            except Exception:
                pass
            if hwnd:
                break
        if hwnd:
            try:
                GWL_EXSTYLE = -20
                WS_EX_NOACTIVATE = 0x08000000  # 点击不激活窗口（不抢焦点）
                WS_EX_TOOLWINDOW = 0x00000080  # 从任务栏/Alt-Tab 隐藏
                style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                ctypes.windll.user32.SetWindowLongW(
                    hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
                )
            except Exception:
                pass

    threading.Thread(target=_win_tweaks, daemon=True).start()

    def _transparency_kick():
        # 页面加载完成后再 hide/show：WebView2 初始化完成，透明才生效
        time.sleep(0.5)
        try:
            window.hide()
            time.sleep(0.3)
            window.show()
        except Exception:
            pass

    window.events.loaded += lambda: threading.Thread(
        target=_transparency_kick, daemon=True
    ).start()

    def _on_closing():                         # 主线程事件回调：只置位，不碰窗口 API
        bridge.stop_event.set()

    window.events.closing += _on_closing

    try:
        webview.start()                        # 阻塞主线程（GUI 事件循环）
    except KeyboardInterrupt:
        bridge.stop_event.set()
    finally:
        bridge.stop_event.set()
        static_server.shutdown()
        print("宠物已退出")


if __name__ == "__main__":
    main()
