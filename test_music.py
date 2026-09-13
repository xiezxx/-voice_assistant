# -*- coding: utf-8 -*-
"""QQ 音乐工具测试：客户端定位 + 搜索接口 + COM 接口 + 参数兜底 + 降级文案。

真实播放/控制部分需要客户端在场，按是否装客户端分支断言或跳过（不真的控制播放，
免得测试跑一半把用户正在听的歌切了）。
"""

import sys

sys.stdout.reconfigure(errors="replace")  # 中文 Windows GBK 控制台打印 ✓ 不崩

import ctypes

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
    assert "暂停" in music.media_action("") or "支持" in music.media_action("")
    r = music.media_action("把音量调到最大")
    assert "支持的操作" in r, r
    assert "暂停" in r and "下一首" in r, r
    print("✓ 未知动作兜底:", r)


def test_action_aliases():
    """中文口语说法要能映射到 COM 方法（不真的调用）。"""
    assert music._ACTION_METHODS["暂停"] == "Pause"
    assert music._ACTION_METHODS["继续"] == "Play"
    assert music._ACTION_METHODS["下一首"] == "PlayNext"
    assert music._ACTION_METHODS["切歌"] == "PlayNext"
    assert music._ACTION_METHODS["上一首"] == "PlayPrev"
    print("✓ 动作别名:", len(music._ACTION_METHODS), "种说法")


def test_play_song_args():
    r = music.play_song("")
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


def test_window_finder():
    """主窗口查找：只认标题，不能把「播放队列」等辅助窗当成主窗口。"""
    hwnd, rect = music._main_window()
    if hwnd is None:
        print("✓ 主窗口查找: 客户端未运行，返回空（不抛异常）")
        return
    user32 = ctypes.windll.user32
    title = ctypes.create_unicode_buffer(128)
    user32.GetWindowTextW(hwnd, title, 128)
    assert title.value.strip() not in music._SKIP_TITLES, title.value
    print(f"✓ 主窗口查找: hwnd={hwnd} 标题={title.value.strip()!r} 尺寸={rect.r - rect.l}x{rect.b - rect.t}")


if __name__ == "__main__":
    test_find_client()
    test_search()
    test_search_degradation()
    test_media_action_args()
    test_action_aliases()
    test_play_song_args()
    test_input_struct_layout()
    test_com_interface()
    test_window_finder()
    print("\n全部通过 ✅")
