# -*- coding: utf-8 -*-
"""QQ 音乐工具测试：客户端定位 + 搜索接口 + COM 接口 + 参数兜底 + 降级文案。

真实播放/控制部分需要客户端在场，按是否装客户端分支断言或跳过（不真的控制播放，
免得测试跑一半把用户正在听的歌切了）。
"""

import sys

sys.stdout.reconfigure(errors="replace")  # 中文 Windows GBK 控制台打印 ✓ 不崩

import asyncio
import ctypes
import json
import threading

import devices
import music


def test_find_client():
    exe = music.find_qqmusic()
    if exe is None:
        print("⚠ 本机没找到 QQ 音乐客户端，跳过安装相关的断言")
        return
    assert exe.is_file(), exe
    assert exe.name.lower() == "qqmusic.exe", exe
    print("✓ 客户端定位:", exe)


def test_search():
    hit = music.search_song("晴天 周杰伦")
    assert hit is not None, "搜索接口没返回结果（网络或接口变动？）"
    assert hit["songmid"], hit
    assert "晴" in hit["name"], hit
    assert "周杰伦" in hit["singer"], hit
    print(f"✓ 搜歌: {hit['name']} - {hit['singer']} (songmid={hit['songmid']})")

    assert music.search_song("") is None
    assert music.search_song("   ") is None
    print("✓ 空关键词兜底: 返回 None")


def test_search_degradation(monkeypatch_url=None):
    """接口不通时返回 None，不抛异常。"""
    origin = music._SEARCH_URL
    music._SEARCH_URL = "https://127.0.0.1:9/not-exist"   # 必然连不上
    try:
        assert music.search_song("晴天") is None
    finally:
        music._SEARCH_URL = origin
    print("✓ 接口不可用时优雅降级: 返回 None 不抛异常")


def test_media_action_args():
    assert "支持" in asyncio.run(music.media_action(""))
    r = asyncio.run(music.media_action("把音量调到最大"))
    assert "支持的操作" in r, r
    assert "暂停" in r and "下一首" in r, r
    print("✓ 未知动作兜底:", r)


def test_choose_target():
    """在哪播的判定：只有"手机在说话 + 遥控 App 在线"才推给手机。"""
    assert music.choose_target("mobile", True) == "device"
    assert music.choose_target("mobile", False) == "pc"      # App 没连 → 回落电脑
    assert music.choose_target("browser", True) == "pc"      # 电脑网页 → 电脑播
    assert music.choose_target("pet", True) == "pc"          # 桌宠 → 电脑播
    assert music.choose_target("cli", True) == "pc"
    assert music.choose_target("", True) == "pc"             # 未知来源保守走电脑
    assert music.choose_target("MOBILE", True) == "device"   # 大小写不敏感
    print("✓ 播放目标判定：手机+在线→手机；其余一律电脑（电脑端行为不变）")


async def _recv_and_ack(ws, ok=True, detail=""):
    """收一条指令并回执 —— 模拟安卓 App 的行为（见 RemoteService.replyResult）。"""
    cmd = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
    await ws.send(json.dumps({"type": "result", "id": cmd.get("id", ""),
                              "ok": ok, "detail": detail}))
    return cmd


async def _device_push_roundtrip():
    """起一个带 /ws/device 的测试服务器，连一个假设备，验证指令真能推到。"""
    import uvicorn
    import websockets
    from fastapi import FastAPI
    from voice_server import register_device_routes

    app = FastAPI()
    register_device_routes(app)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8767,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        async with websockets.connect("ws://127.0.0.1:8767/ws/device") as ws:
            assert json.loads(await ws.recv())["type"] == "ready"
            assert devices.online()

            # 模拟"手机在说话"：点歌 → 指令推给 App（且不碰电脑端的界面自动化）
            #
            # 现在要**边等回执边发指令**：play_song 会一直等到设备回执（或超时）。
            # 回执是必须的 —— 没有它，手机上失败也会被说成成功
            # （用户实际遇到过：手机报"没控制成功"，小音却说"已经切到下一首啦"）
            devices.set_turn_origin("mobile")

            waiter = asyncio.create_task(_recv_and_ack(ws, ok=True))
            reply = await music.play_song("晴天")
            cmd = await waiter
            assert "手机" in reply, reply
            assert cmd["type"] == "play" and cmd["songmid"], cmd
            assert cmd["name"], cmd
            assert cmd.get("id"), f"指令必须带 id 才能对回执：{cmd}"

            # 控制类同样推给手机
            waiter = asyncio.create_task(_recv_and_ack(ws, ok=True))
            reply = await music.media_action("暂停")
            cmd = await waiter
            assert "手机" in reply, reply
            assert cmd["type"] == "media" and cmd["action"] == "pause", cmd

            # ★ 设备回执说"没成功"时，回复必须**如实转告**，不能假装成功
            waiter = asyncio.create_task(_recv_and_ack(ws, ok=False, detail="找不到 QQ 音乐的播放会话"))
            reply = await music.media_action("下一首")
            await waiter
            assert "没控制成功" in reply and "播放会话" in reply, f"失败被说成成功了：{reply}"

            # 来源切回电脑 → 不推给手机（这里只断言不再收到指令）
            devices.set_turn_origin("pet")
            await music.media_action("下一首")
            try:
                await asyncio.wait_for(ws.recv(), timeout=1.5)
                raise AssertionError("电脑端来源不应推给手机")
            except asyncio.TimeoutError:
                pass
    finally:
        devices.set_turn_origin("")
        server.should_exit = True
        thread.join(timeout=5)


def test_device_push():
    if music.find_qqmusic() is None and not devices.online():
        # 设备推送与是否装客户端无关，照跑；这里只是留个提示
        pass
    asyncio.run(_device_push_roundtrip())
    print("✓ 设备推送：手机点歌/暂停推给 App；电脑来源不推（电脑端行为不变）")


def test_action_aliases():
    """中文口语说法要能映射到 COM 方法（不真的调用）。"""
    assert music._ACTION_METHODS["暂停"] == "Pause"
    assert music._ACTION_METHODS["继续"] == "Play"
    assert music._ACTION_METHODS["下一首"] == "PlayNext"
    assert music._ACTION_METHODS["切歌"] == "PlayNext"
    assert music._ACTION_METHODS["上一首"] == "PlayPrev"
    print("✓ 动作别名:", len(music._ACTION_METHODS), "种说法")


def test_play_song_args():
    r = asyncio.run(music.play_song(""))
    assert "歌名" in r or "想听" in r, r
    print("✓ 空歌名兜底:", r)


def test_input_struct_layout():
    """INPUT 结构体尺寸写错会让 SendInput 静默失败，这里守住。"""
    expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    actual = ctypes.sizeof(music._INPUT)
    assert actual == expected, f"INPUT 结构体尺寸 {actual} != 预期 {expected}"
    assert ctypes.sizeof(music._WINDOWPLACEMENT) == 44, ctypes.sizeof(music._WINDOWPLACEMENT)
    print(f"✓ ctypes 结构体布局: INPUT={actual}B, WINDOWPLACEMENT=44B")


def test_com_interface():
    """COM 播放接口可用性（只创建对象 + 查方法号，不动播放状态）。"""
    if music.find_qqmusic() is None:
        print("⚠ 没装客户端，跳过 COM 断言")
        return
    player = music._get_player()
    if player is None:
        print("⚠ COM 接口不可用（客户端版本差异？），控制类功能会走降级文案")
        return
    for method in ("Play", "Pause", "PlayNext", "PlayPrev"):
        assert player._dispid(method) > 0, method
    print("✓ COM 接口: Play/Pause/PlayNext/PlayPrev 方法号均可解析")


def test_auxiliary_titles():
    """辅助窗识别（纯函数）：提示浮层必须被挡掉。

    这条是补的：主窗口最小化到托盘时只剩 314x50，而「已开始播放提示」那种浮层
    有整屏那么大 —— 不挡掉它，按面积排序就会把浮层当成主窗口，
    之后所有点击坐标全算错（实测碰上过）。
    """
    for bad in ["播放队列", "TXMenuWindow", "已开始播放提示", "已暂停播放提示", "音量提示"]:
        assert music._is_auxiliary_title(bad), f"{bad} 应该被判为辅助窗"
    # 主窗口：空闲时叫「QQ音乐」，放歌时变成「歌名 - 歌手」，两个都得认
    for good in ["QQ音乐", "Snowing - 松井祐貴", "晴天 - 周杰伦"]:
        assert not music._is_auxiliary_title(good), f"{good} 是主窗口，不该被挡掉"
    print("✓ 辅助窗识别：提示浮层挡掉，主窗口（含放歌时的「歌名 - 歌手」）保留")


def test_window_finder():
    """主窗口查找：只认标题，不能把「播放队列」等辅助窗当成主窗口。"""
    hwnd, rect = music._main_window()
    if hwnd is None:
        print("✓ 主窗口查找: 客户端未运行，返回空（不抛异常）")
        return
    user32 = ctypes.windll.user32
    title = ctypes.create_unicode_buffer(128)
    user32.GetWindowTextW(hwnd, title, 128)
    assert not music._is_auxiliary_title(title.value.strip()), title.value
    print(f"✓ 主窗口查找: hwnd={hwnd} 标题={title.value.strip()!r} 尺寸={rect.r - rect.l}x{rect.b - rect.t}")


if __name__ == "__main__":
    test_find_client()
    test_search()
    test_search_degradation()
    test_media_action_args()
    test_action_aliases()
    test_choose_target()
    test_device_push()
    test_play_song_args()
    test_input_struct_layout()
    test_com_interface()
    test_auxiliary_titles()
    test_window_finder()
    print("\n全部通过 ✅")
