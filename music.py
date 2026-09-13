# -*- coding: utf-8 -*-
"""QQ 音乐控制：播放控制走客户端 COM 接口，点歌走搜索 + 界面自动化。

两条路径的可靠性差异很大，这是实测结论：

- **播放控制**（暂停/继续/切歌）走 `QQMusicSvr.QQMusicPlayer` COM 接口，
  客户端缩在托盘里也能控制，不碰界面、不抢焦点，纯 ctypes 调用（客户端把它注册在
  32 位视图，但 Windows 支持跨位数激活，64 位 Python 也能创建）。
- **点歌**（放某首歌）只能靠界面自动化：清空搜索框 → 打歌名 → 点联想列表第一条。
  `/play` 命令行只认本地文件，`ParseSongInfo`/`AddSong` 是未公开接口（类型库为空壳，
  参数结构探不出来），所以没有更干净的入口。
  实测踩实的两点：**点联想的某一条才会播放**（按回车只是跳到搜索结果页，不播）；
  搜索结果页的第一行位置会随"歌手卡片"是否出现而变，所以走联想列表（锚定在搜索框下方，位置稳定）。
  代价是要把窗口拉到前台并抢一次焦点，另外客户端把主窗口藏起来时（藏在屏幕外 -32000）
  要靠 SetWindowPlacement 连位置一起写回才拉得回来，拉不回来就提示用户点托盘图标。

踩过的坑（改代码前先看这两条）：
1. 高 DPI 屏幕上本进程必须是 DPI 感知的，否则 `GetWindowRect`/`SetCursorPos` 拿到的是
   虚拟化坐标（200% 缩放下只有物理值的一半），点击会落在窗口正中间而不是搜索框。
2. QQ 音乐主窗口和一批辅助小窗共用 `TXGuiFoundation` 窗口类，找窗口必须按面积挑最大的。
"""

import ctypes
import json
import subprocess
import time
import urllib.parse
import urllib.request
import winreg
from pathlib import Path

from config import Config

# 搜索接口：免 Key、国内直连实测可用（返回 songmid/songname/singer）
_SEARCH_URL = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
_HTTP_TIMEOUT = 8

# QQMusicSvr.QQMusicPlayer（客户端注册的 COM 播放接口，仅 32 位视图）
_CLSID_PLAYER = "{D4C131B2-5B11-47FF-ABC5-081AB498EA5D}"
_IID_IDISPATCH = "{00020400-0000-0000-C000-000000000046}"
_IID_NULL = "{00000000-0000-0000-0000-000000000000}"
_CLSCTX_LOCAL_SERVER = 4
_DISPATCH_METHOD = 1

# 动作别名 → COM 方法名（模型不一定老实传英文）
_ACTION_METHODS = {
    "pause": "Pause", "暂停": "Pause", "停": "Pause", "停止": "Pause",
    "resume": "Play", "继续": "Play", "继续播放": "Play", "播放": "Play", "play": "Play",
    "next": "PlayNext", "下一首": "PlayNext", "切歌": "PlayNext", "换一首": "PlayNext",
    "prev": "PlayPrev", "previous": "PlayPrev", "上一首": "PlayPrev",
}

# 界面自动化常量
# ⚠️ 坐标一律是**物理像素**：搜索框位置按窗口尺寸取比例（2200x1480 下校准为 (0.42, 0.045)），
#    这样窗口被用户缩放后也能大致命中。前提是进程已设为 DPI 感知（见 _ensure_dpi_aware）——
#    否则 200% 缩放时拿到的是虚拟化坐标（只有物理值一半），点击会落在窗口正中间。
_SEARCH_BOX_RATIO = (0.42, 0.045)
# 输入歌名后弹出的联想列表第一条（锚定在搜索框正下方，位置比搜索结果页稳定）
# 实测：点它才会真正开始播放；按回车只是跳到搜索结果页，不会播
_SUGGESTION_RATIO = (0.418, 0.145)
_MAIN_WINDOW_TITLE = "QQ音乐"         # 主窗口标题（与辅助窗区分，别用尺寸判断）
_SKIP_TITLES = {"播放队列", "TXMenuWindow"}   # 同类的辅助小窗，别当成主窗口
_WINDOW_WAIT_SEC = 8.0                # 冷启动后等主窗口出现的上限
_KEY_INTERVAL = 0.05

_dpi_ready = False


def _ensure_dpi_aware():
    """让本进程按物理像素工作。必须先于任何坐标计算/点击调用。"""
    global _dpi_ready
    if _dpi_ready:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)      # per-monitor DPI aware
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass
    _dpi_ready = True


# ── 定位客户端 ────────────────────────────────────────────────

def find_qqmusic() -> Path | None:
    """定位 QQMusic.exe：配置项 → 注册表 → 安装目录下按版本号取最新。"""
    configured = (Config.QQMUSIC_PATH or "").strip()
    if configured:
        p = Path(configured)
        if p.is_file():
            return p
        exe = p / "QQMusic.exe"
        if exe.is_file():
            return exe

    for hive, key in (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQMusic"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQMusic"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQMusic"),
    ):
        try:
            with winreg.OpenKey(hive, key) as k:
                location, _ = winreg.QueryValueEx(k, "InstallLocation")
        except OSError:
            continue
        exe = Path(location) / "QQMusic.exe"
        if exe.is_file():
            return exe

    # 兜底：安装目录下常并存多个版本子目录（如 QQMusic2131.20.49.42），取版本号最大的
    try:
        parent = Path((Config.QQMUSIC_PATH or "").strip() or r"D:\QQYINYUE\QQMusic")
        if parent.is_dir():
            exes = [p for p in parent.glob("QQMusic*/QQMusic.exe") if p.is_file()]
            if exes:
                return max(exes, key=lambda p: p.parent.name)
    except OSError:
        pass
    return None


def _client_pids() -> set[int]:
    """QQ 音乐客户端进程 PID（不依赖 psutil）。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             'Get-Process | Where-Object { $_.ProcessName -eq "QQMusic" } '
             '| Select-Object -ExpandProperty Id'],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {int(x) for x in out.split() if x.strip().isdigit()}


# ── COM：播放控制 ─────────────────────────────────────────────

class _GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort),
                ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]


class _DISPPARAMS(ctypes.Structure):
    _fields_ = [("rgvarg", ctypes.c_void_p), ("rgdispidNamedArgs", ctypes.c_void_p),
                ("cArgs", ctypes.c_uint), ("cNamedArgs", ctypes.c_uint)]


class _EXCEPINFO(ctypes.Structure):
    _fields_ = [("wCode", ctypes.c_ushort), ("wReserved", ctypes.c_ushort),
                ("bstrSource", ctypes.c_wchar_p), ("bstrDescription", ctypes.c_wchar_p),
                ("bstrHelpFile", ctypes.c_wchar_p), ("dwHelpContext", ctypes.c_ulong),
                ("pvReserved", ctypes.c_void_p), ("pfnDeferredFillIn", ctypes.c_void_p),
                ("scode", ctypes.c_long)]


def _guid(text: str) -> _GUID:
    g = _GUID()
    ctypes.oledll.ole32.CLSIDFromString(text, ctypes.byref(g))
    return g


def _vtable_call(ptr, index, restype, *argtypes):
    """按 vtable 下标调用 COM 方法（IDispatch 晚绑定）。"""
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(vtable[index])


class _Player:
    """QQMusicSvr.QQMusicPlayer 的极简晚绑定封装（只用无参方法）。"""

    def __init__(self):
        self._ptr = ctypes.c_void_p()
        ctypes.oledll.ole32.CoInitialize(None)
        ctypes.oledll.ole32.CoCreateInstance(
            ctypes.byref(_guid(_CLSID_PLAYER)), None, _CLSCTX_LOCAL_SERVER,
            ctypes.byref(_guid(_IID_IDISPATCH)), ctypes.byref(self._ptr),
        )
        if not self._ptr.value:
            raise OSError("CoCreateInstance 未返回接口指针")

    def _dispid(self, name: str) -> int:
        names = (ctypes.c_wchar_p * 1)(ctypes.c_wchar_p(name))
        dispid = ctypes.c_long()
        _vtable_call(
            self._ptr, 5, ctypes.HRESULT, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_wchar_p),
            ctypes.c_uint, ctypes.c_ulong, ctypes.POINTER(ctypes.c_long),
        )(self._ptr, ctypes.byref(_guid(_IID_NULL)), names, 1, 0, ctypes.byref(dispid))
        return dispid.value

    def call(self, name: str):
        """调用无参方法。失败抛 OSError（HRESULT 非 0）。"""
        dispid = self._dispid(name)
        params = _DISPPARAMS(None, None, 0, 0)
        except_info = _EXCEPINFO()
        _vtable_call(
            self._ptr, 6, ctypes.HRESULT, ctypes.c_long, ctypes.POINTER(_GUID), ctypes.c_ulong,
            ctypes.c_ushort, ctypes.POINTER(_DISPPARAMS), ctypes.c_void_p,
            ctypes.POINTER(_EXCEPINFO), ctypes.c_void_p,
        )(self._ptr, dispid, ctypes.byref(_guid(_IID_NULL)), 0, _DISPATCH_METHOD,
          ctypes.byref(params), None, ctypes.byref(except_info), None)


_player: _Player | None = None


def _get_player() -> _Player | None:
    """惰性创建并缓存 COM 对象（客户端没装/没注册时返回 None）。"""
    global _player
    if _player is None:
        try:
            _player = _Player()
        except OSError:
            return None
    return _player


def media_action(action: str) -> str:
    """暂停 / 继续 / 上一首 / 下一首。返回中文结果文案。"""
    key = (action or "").strip().lower()
    method = _ACTION_METHODS.get(key)
    if not method:
        return "支持的操作有：暂停、继续、下一首、上一首"
    if not _client_pids() and find_qqmusic() is None:
        return "没找到 QQ 音乐客户端，没法控制播放"

    player = _get_player()
    if player is None:
        return "QQ 音乐的播放接口不可用（可能是版本差异），你可以直接在客户端里操作"
    try:
        player.call(method)
    except OSError:
        # 客户端刚启动或接口尚未就绪，重置缓存后再试一次
        global _player
        _player = None
        time.sleep(0.8)
        player = _get_player()
        if player is None:
            return "QQ 音乐的播放接口暂时不可用，稍后再试试"
        try:
            player.call(method)
        except OSError:
            return "没能控制 QQ 音乐播放，你可以直接在客户端里操作"

    return {
        "Pause": "已暂停 QQ 音乐",
        "Play": "继续播放",
        "PlayNext": "已切到下一首",
        "PlayPrev": "已切回上一首",
    }.get(method, "已发送播放控制")


# ── 搜索 ─────────────────────────────────────────────────────

def search_song(keyword: str) -> dict | None:
    """按歌名搜索，返回 {songmid, name, singer}；无结果或失败返回 None。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return None
    query = urllib.parse.urlencode({"p": 1, "n": 3, "w": keyword, "format": "json"})
    req = urllib.request.Request(
        f"{_SEARCH_URL}?{query}",
        headers={"User-Agent": "voice-assistant/1.0", "Referer": "https://y.qq.com/"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        songs = (data.get("data") or {}).get("song", {}).get("list") or []
    except (OSError, ValueError):
        return None
    if not songs:
        return None
    top = songs[0]
    singers = top.get("singer") or [{}]
    return {
        "songmid": top.get("songmid", ""),
        "name": top.get("songname", ""),
        "singer": singers[0].get("name", ""),
    }


# ── 界面自动化：点歌 ─────────────────────────────────────────

class _RECT(ctypes.Structure):
    _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                ("r", ctypes.c_long), ("b", ctypes.c_long)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint), ("flags", ctypes.c_uint), ("showCmd", ctypes.c_uint),
                ("ptMinPosition", _POINT), ("ptMaxPosition", _POINT), ("rcNormalPosition", _RECT)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


_INPUT_MOUSE, _INPUT_KEYBOARD = 0, 1
_KEYEVENTF_UNICODE, _KEYEVENTF_KEYUP = 0x0004, 0x0002
_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
_VK_RETURN, _VK_CONTROL, _VK_A, _VK_DELETE = 0x0D, 0x11, 0x41, 0x2E


def _send_input(inp) -> bool:
    n = ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
    return n == 1


def _main_window():
    """找 QQ 音乐主窗口：优先标题为「QQ音乐」的那个，其次取面积最大的非辅助窗。

    主窗口和一堆辅助小窗（TXMenuWindow 24x24、播放队列 896x746、若干无名窗）共用
    `TXGuiFoundation` 类，只能靠标题区分。**不能按尺寸过滤**——主窗口被藏到托盘时
    会被挪到屏幕外并缩成 314x50，那时候还得能找到它才谈得上恢复。
    """
    _ensure_dpi_aware()
    user32 = ctypes.windll.user32
    pids = _client_pids()
    if not pids:
        return None, None
    found = []

    def _cb(hwnd, _):
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if cls.value == "TXGuiFoundation":
                title = ctypes.create_unicode_buffer(128)
                user32.GetWindowTextW(hwnd, title, 128)
                rect = _RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                width = rect.r - rect.l
                name = title.value.strip()
                if name and name not in _SKIP_TITLES:
                    # 排序键：标题是「QQ音乐」的优先，其次按面积
                    found.append(((name == _MAIN_WINDOW_TITLE), width * (rect.b - rect.t),
                                  hwnd, rect))
        return True

    cb = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows(cb(_cb), 0)
    if not found:
        return None, None
    found.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return found[0][2], found[0][3]


def _ensure_window_visible(exe: Path):
    """确保主窗口可见并返回 (hwnd, rect)；客户端没跑就启动，窗口被藏起来就恢复。"""
    user32 = ctypes.windll.user32
    hwnd, rect = _main_window()
    if hwnd is None:
        # 没在跑（或主窗口已销毁）→ 启动客户端，冷启动会正常显示主窗口
        subprocess.Popen([str(exe)], close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        deadline = time.time() + _WINDOW_WAIT_SEC
        while time.time() < deadline and hwnd is None:
            time.sleep(0.5)
            hwnd, rect = _main_window()
        if hwnd is None:
            return None, None

    # 窗口被最小化/藏到屏幕外：ShowWindow / SetWindowPos 都无效（客户端会立刻挪回去），
    # 只有 SetWindowPlacement 有效；而且客户端的「正常位置」本身可能就是屏幕外的，
    # 所以要连 rcNormalPosition 一起写回屏幕内，否则恢复出来还是在屏幕外。
    if user32.IsIconic(hwnd) or rect.l < -10000:
        placement = _WINDOWPLACEMENT()
        placement.length = ctypes.sizeof(placement)
        user32.GetWindowPlacement(hwnd, ctypes.byref(placement))
        if placement.rcNormalPosition.l < -10000:
            screen_w = user32.GetSystemMetrics(0)
            screen_h = user32.GetSystemMetrics(1)
            placement.rcNormalPosition.l = 80
            placement.rcNormalPosition.t = 80
            placement.rcNormalPosition.r = min(80 + 2200, screen_w - 80)
            placement.rcNormalPosition.b = min(80 + 1480, screen_h - 80)
        placement.showCmd = 1                      # SW_SHOWNORMAL
        user32.SetWindowPlacement(hwnd, ctypes.byref(placement))
        time.sleep(1.2)
        hwnd, rect = _main_window()
    return hwnd, rect


def _foreground(hwnd) -> bool:
    """把窗口拉到前台（后台进程直接调 SetForegroundWindow 会被系统拒绝）。"""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    for _ in range(3):
        fg = user32.GetForegroundWindow()
        tid_fg = user32.GetWindowThreadProcessId(fg, None)
        tid_me = kernel32.GetCurrentThreadId()
        user32.AttachThreadInput(tid_fg, tid_me, True)
        user32.SetForegroundWindow(hwnd)
        user32.AttachThreadInput(tid_fg, tid_me, False)
        time.sleep(0.5)
        if user32.GetForegroundWindow() == hwnd:
            return True
    return False


def _press_ctrl(vk):
    """Ctrl + 某键（用于全选清空搜索框）。"""
    _send_input(_INPUT(type=_INPUT_KEYBOARD,
                       u=_INPUTUNION(ki=_KEYBDINPUT(_VK_CONTROL, 0, 0, 0, None))))
    time.sleep(0.03)
    for flag in (0, _KEYEVENTF_KEYUP):
        _send_input(_INPUT(type=_INPUT_KEYBOARD,
                           u=_INPUTUNION(ki=_KEYBDINPUT(vk, 0, flag, 0, None))))
        time.sleep(0.03)
    _send_input(_INPUT(type=_INPUT_KEYBOARD,
                       u=_INPUTUNION(ki=_KEYBDINPUT(_VK_CONTROL, 0, _KEYEVENTF_KEYUP, 0, None))))


def _click(x: int, y: int):
    user32 = ctypes.windll.user32
    user32.SetCursorPos(x, y)
    time.sleep(0.3)
    for flag in (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP):
        _send_input(_INPUT(type=_INPUT_MOUSE, u=_INPUTUNION(mi=_MOUSEINPUT(0, 0, 0, flag, 0, None))))
        time.sleep(0.08)


def _type_text(text: str):
    """用 KEYEVENTF_UNICODE 输入任意字符（中文不能用虚拟键码发）。"""
    for ch in text:
        for flag in (_KEYEVENTF_UNICODE, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP):
            _send_input(_INPUT(type=_INPUT_KEYBOARD,
                               u=_INPUTUNION(ki=_KEYBDINPUT(0, ord(ch), flag, 0, None))))
        time.sleep(_KEY_INTERVAL)


def _send_key(vk: int):
    for flag in (0, _KEYEVENTF_KEYUP):
        _send_input(_INPUT(type=_INPUT_KEYBOARD,
                           u=_INPUTUNION(ki=_KEYBDINPUT(vk, 0, flag, 0, None))))
        time.sleep(_KEY_INTERVAL)


def play_song(song: str) -> str:
    """点歌：搜索 → 把歌名打进客户端搜索框并回车。返回中文结果文案。"""
    song = (song or "").strip()
    if not song:
        return "想听什么歌？说个歌名，比如「放首晴天」"

    hit = search_song(song)
    if hit is None:
        return f"没搜到《{song}》，换个歌名试试？"

    label = f"《{hit['name']}》" + (f" - {hit['singer']}" if hit["singer"] else "")
    exe = find_qqmusic()
    if exe is None:
        return "没找到 QQ 音乐客户端，可以在 .env 里用 QQMUSIC_PATH 指定安装路径"

    try:
        hwnd, rect = _ensure_window_visible(exe)
    except OSError:
        return "没能启动 QQ 音乐，稍后再试试"
    if hwnd is None:
        return "QQ 音乐正在后台运行但主窗口打不开，点一下托盘图标再让我试"

    if not _foreground(hwnd):
        return "QQ 音乐没能切到前台，点一下它的窗口再让我试"

    # 安全约束（重要）：点击/输入前重新确认窗口还在原位、还在前台。
    # 用户可能正在用电脑，窗口随时会被移动或藏起来，那样按键就会打进别的窗口。
    _ensure_dpi_aware()
    still, fresh = _main_window()
    if still != hwnd or ctypes.windll.user32.GetForegroundWindow() != hwnd:
        return "QQ 音乐窗口刚被切换走了，先没敢操作，过一会儿再让我试"

    _click(fresh.l + int(_SEARCH_BOX_RATIO[0] * (fresh.r - fresh.l)),
           fresh.t + int(_SEARCH_BOX_RATIO[1] * (fresh.b - fresh.t)))
    time.sleep(0.5)
    _press_ctrl(_VK_A)          # 搜索框里可能留着上次的文字，先全选清空
    time.sleep(0.15)
    _send_key(_VK_DELETE)
    time.sleep(0.3)
    _type_text(hit["name"])
    time.sleep(1.5)             # 等联想列表弹出来
    if ctypes.windll.user32.GetForegroundWindow() != hwnd:
        return "QQ 音乐窗口刚被切走了，歌名没输完，稍后再让我试"

    # 点联想列表第一条 → 真正开始播放（实测回车只会跳到搜索结果页）
    _click(fresh.l + int(_SUGGESTION_RATIO[0] * (fresh.r - fresh.l)),
           fresh.t + int(_SUGGESTION_RATIO[1] * (fresh.b - fresh.t)))
    return f"正在播放{label}"
