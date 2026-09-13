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
import os
import queue
import sys
import threading
import time
from pathlib import Path

# pythonw（双击 bat 启动）无控制台：stdout/stderr 重定向到日志。
# stderr 也必须处理——http.server 等库写 stderr，None 会导致响应截断/卡死
if sys.stdout is None or sys.stderr is None:
    _log = open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "pet_run.log"),
        "a", encoding="utf-8",
    )
    if sys.stdout is None:
        sys.stdout = _log
    if sys.stderr is None:
        sys.stderr = _log

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

# ── 系统托盘驻留（pystray，独立线程）────────────────────────

def _startup_file() -> Path:
    return (
        Path(os.environ.get("APPDATA", ""))
        / "Microsoft/Windows/Start Menu/Programs/Startup"
        / "小音宠物.bat"
    )


def is_autostart_installed() -> bool:
    return _startup_file().exists()


def install_autostart() -> bool:
    """写开机自启脚本到 Startup 目录，成功返回 True。

    ⚠️ 不能直接复制 start_pet.bat：bat 里全是 %~dp0 相对路径，放进 Startup 目录后
    %~dp0 指向 Startup 而非项目目录，登录自启会静默失效（找不到 VBS/pet.py）。
    所以生成一个带绝对路径的包装脚本。
    """
    target = _startup_file()
    project_dir = Path(__file__).parent
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f'@echo off\ncd /d "{project_dir}"\ncall "{project_dir / "start_pet.bat"}"\n',
            encoding="gbk",          # cmd 按系统 ANSI 代码页读 bat，中文路径必须 GBK
        )
    except OSError:
        return False
    return target.exists()


def uninstall_autostart() -> bool:
    """删除开机自启脚本，成功返回 True。"""
    try:
        _startup_file().unlink(missing_ok=True)
    except OSError:
        return False
    return not is_autostart_installed()


def _make_tray_icon():
    """用 PIL 绘制精致版小音托盘图标：渐变脸 + 猫耳 + 高光眼。"""
    from PIL import Image, ImageDraw, ImageFilter

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # 径向渐变圆脸
    grad = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    gd.ellipse([8, 8, size * 2 - 8, size * 2 - 8], fill=(255, 232, 238, 255))
    gd.ellipse([size - 44, size - 52, size + 44, size + 36], fill=(255, 190, 205, 255))
    grad = grad.filter(ImageFilter.GaussianBlur(10))
    img = Image.alpha_composite(img, grad.resize((size, size)).crop((0, 0, size, size)))

    d = ImageDraw.Draw(img)
    # 猫耳
    d.polygon([(16, 22), (10, 4), (30, 12)], fill=(255, 214, 224, 255))
    d.polygon([(48, 22), (54, 4), (34, 12)], fill=(255, 214, 224, 255))
    d.polygon([(18, 18), (15, 10), (26, 14)], fill=(255, 158, 178, 255))
    d.polygon([(46, 18), (49, 10), (38, 14)], fill=(255, 158, 178, 255))
    # 脸
    d.ellipse([12, 10, 52, 54], fill=(255, 224, 231, 255))
    # 眼睛（深可可 + 白色高光）
    d.ellipse([19, 24, 28, 35], fill=(74, 59, 50, 255))
    d.ellipse([36, 24, 45, 35], fill=(74, 59, 50, 255))
    d.ellipse([21, 26, 24, 29], fill=(255, 255, 255, 255))
    d.ellipse([38, 26, 41, 29], fill=(255, 255, 255, 255))
    # 腮红
    d.ellipse([13, 36, 21, 42], fill=(255, 170, 190, 190))
    d.ellipse([43, 36, 51, 42], fill=(255, 170, 190, 190))
    # 微笑
    d.arc([27, 36, 37, 48], 20, 160, fill=(74, 59, 50, 255), width=2)
    return img


def start_tray(window, bridge: "UiBridge"):
    """托盘常驻：显示/隐藏、换模型、开机自启开关、退出。run_detached 独立线程。"""
    import pystray

    def toggle_show(icon, item):
        try:
            if _GLOBALS.get("pet_visible", True):
                window.hide()
                _GLOBALS["pet_visible"] = False
            else:
                window.show()
                _GLOBALS["pet_visible"] = True
        except Exception:
            pass

    def do_switch_model(icon, item):
        cfg = _load_pet_config()
        current = str(cfg.get("model", "senko"))
        if current not in _MODEL_NAMES:
            current = "senko"
        nxt = _MODEL_NAMES[(_MODEL_NAMES.index(current) + 1) % len(_MODEL_NAMES)]
        cfg["model"] = nxt
        _save_pet_config(cfg)
        page = dict((m[0], m[1]) for m in _PET_MODELS)[nxt]
        try:
            _GLOBALS["window"].load_url(_GLOBALS["base_url"] + f"/{page}?model={nxt}")
        except Exception:
            pass

    def toggle_autostart(icon, item):
        if is_autostart_installed():
            uninstall_autostart()
        else:
            install_autostart()

    def is_autostart(item):
        return is_autostart_installed()

    def do_exit(icon, item):
        bridge.stop_event.set()
        try:
            window.destroy()
        except Exception:
            pass
        try:
            icon.stop()
        except Exception:
            pass

        def _force_exit():
            # 兜底：销毁后进程未正常退出则强制结束（pystray/webview 偶发不退出）
            time.sleep(1.5)
            os._exit(0)

        threading.Thread(target=_force_exit, daemon=True).start()

    menu = pystray.Menu(
        pystray.MenuItem("显示 / 隐藏宠物", toggle_show, default=True),
        pystray.MenuItem("换模型", do_switch_model),
        pystray.MenuItem("开机自启动", toggle_autostart, checked=is_autostart),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", do_exit),
    )
    tray = pystray.Icon("小音", _make_tray_icon(), "小音", menu)
    tray.run_detached()
    _GLOBALS["tray"] = tray


def start_static_server(port: int = 0) -> tuple:
    """在本机起一个仅监听 127.0.0.1 的静态服务器（线程内运行），返回 (server, base_url)。"""
    import functools
    import http.server

    class _NoCacheHandler(http.server.SimpleHTTPRequestHandler):
        """禁用缓存 + 强制刷新请求日志（pythonw 下诊断模型加载问题）。"""

        def end_headers(self):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            super().end_headers()

        def log_message(self, fmt, *args):
            try:
                sys.stderr.write(
                    "%s - - [%s] %s\n"
                    % (self.address_string(), self.log_date_time_string(), fmt % args)
                )
                sys.stderr.flush()
            except Exception:
                pass

    handler = functools.partial(_NoCacheHandler, directory=str(PET_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def main():
    # WinForms 未处理异常写入日志而非弹窗：宠物窗口在隐藏命令行下运行，
    # 错误弹窗会变成看不见的卡死，写日志可诊断
    try:
        import clr

        clr.AddReference("System.Windows.Forms")
        import System

        def _log_net_exc(sender, e):
            with open(
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "pet_net_err.log"),
                "a", encoding="utf-8",
            ) as f:
                f.write(
                    "\n===== WinForms 未处理异常 =====\n"
                    + (e.Exception.ToString() if e.Exception else "(无详情)")
                    + "\n"
                )

        System.Windows.Forms.Application.add_ThreadException(
            System.Threading.ThreadExceptionEventHandler(_log_net_exc)
        )
    except Exception as ex:
        print(f"[诊断] .NET 异常日志安装失败: {ex}")

    parser = argparse.ArgumentParser(description="小音桌面宠物（连接语音助手服务器）")
    parser.add_argument("--host", default="127.0.0.1", help="服务器地址")
    parser.add_argument("--port", type=int, default=7860, help="服务器端口")
    parser.add_argument("--voice", default="", help="TTS 音色（可选）")
    parser.add_argument("--scale", type=float, default=1.0, help="窗口缩放（1.0=320x400）")
    parser.add_argument("--x", type=int, default=None, help="窗口 X（默认右下角）")
    parser.add_argument("--y", type=int, default=None)
    parser.add_argument("--no-on-top", action="store_true", help="不置顶")
    parser.add_argument("--no-tray", action="store_true", help="不启用系统托盘")
    parser.add_argument("--install-autostart", action="store_true", help="设置开机自启后退出")
    parser.add_argument("--uninstall-autostart", action="store_true", help="取消开机自启后退出")
    args = parser.parse_args()

    # 自启动开关：纯命令行操作，不开窗口（install_autostart.bat 调用同一份实现）
    if args.install_autostart or args.uninstall_autostart:
        if args.uninstall_autostart:
            ok = uninstall_autostart()
            print("[小音] 已取消开机自启" if ok else "[小音] 取消失败，请检查权限")
        else:
            ok = install_autostart()
            if ok:
                print(f"[小音] 已设置开机自启：{_startup_file()}")
                print("[小音] 下次登录 Windows 会自动拉起服务器 + 宠物")
            else:
                print("[小音] 设置失败，请检查权限")
        return

    import webview                          # 自启动开关不需要 GUI，放在参数处理之后

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


    if not args.no_tray:
        try:
            start_tray(window, bridge)   # 托盘常驻（独立线程）
        except Exception as e:
            print(f"[托盘] 启动失败（可用 --no-tray 关闭）: {e}")

    def _on_loaded():
        # 在 UI 线程内执行窗口级调优。
        # ⚠️ 不能用 window.native.ShowInTaskbar=False：WinForms 会 RecreateHandle，
        #    WebView2 被销毁，页面冻结在「模型加载中」白屏（ef18914 的死锁是同一根因）。
        #    也不能从其他线程 SetWindowLongW：会触发 CoreWebView2Controller 跨线程异常。
        #    这里在 UI 线程直接改扩展样式：
        #      WS_EX_TOOLWINDOW  藏任务栏 + 不进 Alt+Tab
        #      WS_EX_NOACTIVATE  点击不抢焦点
        #      ⚠️ 必须同时清掉 WS_EX_APPWINDOW（WinForms 默认加上的 0x40000）——
        #         它会强制任务栏按钮，把 TOOLWINDOW 的效果盖掉：表现为启动时没按钮，
        #         但托盘点一次「显示/隐藏」（hide/show）后按钮就冒出来（实测复现）。
        try:
            import ctypes

            GWL_EXSTYLE = -20
            WS_EX_APPWINDOW = 0x00040000
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            user32 = ctypes.windll.user32
            hwnd = window.native.Handle.ToInt32()
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            new_style = (
                (style & ~WS_EX_APPWINDOW) | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
            )
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)
            print(f"[调试] 窗口样式 0x{style:08X} -> 0x{new_style:08X}", flush=True)
        except Exception as e:
            print(f"[调试] 窗口样式: {e}", flush=True)
        # 透明触发：WebView2 初始化完成后再 hide/show（UI 线程内安全）
        try:
            time.sleep(0.3)
            window.hide()
            time.sleep(0.25)
            window.show()
        except Exception as e:
            print(f"[调试] 透明 kick: {e}", flush=True)

    window.events.loaded += _on_loaded

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
