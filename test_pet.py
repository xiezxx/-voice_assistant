# -*- coding: utf-8 -*-
"""桌面宠物离线测试：状态映射 / UI 桥 / HTML 组装 / client 重构回归（无需 GUI）。"""

import re

import pet as pet_mod                  # pet.py 顶层无重依赖，可无 GUI 导入


def test_event_to_pet_state():
    cases = [
        ({"type": "ready"}, ("idle", "")),
        ({"type": "wake", "keyword": "小音"}, ("listening", "")),
        ({"type": "wake", "source": "click"}, ("listening", "")),
        ({"type": "transcript", "text": "现在几点"}, ("thinking", "现在几点")),
        ({"type": "sentence", "text": "三点。"}, ("speaking", "三点。")),
        ({"type": "barge_in"}, ("listening", "")),
        ({"type": "speaker_reject", "text": "声音不是主人，已忽略"},
         ("reject", "声音不是主人，已忽略")),
        ({"type": "turn_end", "status": "✅ 已完成回复"}, ("idle", "✅ 已完成回复")),
        ({"type": "status", "text": "🎤 正在识别…"}, (None, "🎤 正在识别…")),
        ({"type": "audio", "format": "mp3"}, (None, None)),
        ({"type": "hello_ok"}, (None, None)),
    ]
    for ev, want in cases:
        got = pet_mod.event_to_pet_state(ev)
        assert got == want, f"{ev} → {got}，期望 {want}"
    print(f"✓ 状态映射：{len(cases)} 种事件 → 宠物状态/气泡文本")


def test_ui_bridge():
    b = pet_mod.UiBridge()
    assert b.pull_state() is None
    b.push_state("listening")
    b.push_state("thinking", "现在几点")
    b.push_state("speaking", "三点。")
    assert b.pull_state() == {"state": "speaking", "text": "三点。"}
    assert b.pull_state() is None
    for i in range(200):                       # 满队列丢最旧、永不阻塞
        b.push_state("idle", f"t{i}")
    assert b.pull_state()["text"] == "t199"
    # 未连接时控制消息暂存
    b2 = pet_mod.UiBridge()
    b2.submit_control({"type": "listen"})
    assert not b2._pending.empty()
    print("✓ UI 桥：排空最新优先、满队列丢旧不阻塞、未连接暂存控制")


def test_static_server():
    """静态服务器：起服务 → 请求 pet.html / pet.js → 内容正确 → 关闭。"""
    import urllib.request

    server, base_url = pet_mod.start_static_server(port=0)
    try:
        with urllib.request.urlopen(f"{base_url}/pet.html", timeout=5) as resp:
            html = resp.read().decode("utf-8")
        assert resp.status == 200 and "live2dcubismcore.min.js" in html
        with urllib.request.urlopen(f"{base_url}/pet.js", timeout=5) as resp:
            js = resp.read().decode("utf-8")
        assert "Live2DModel" in js
        # 模型与纹理可访问（若已下载）
        import json as _json
        try:
            with urllib.request.urlopen(f"{base_url}/model/cat102/CAT102.model3.json", timeout=5) as resp:
                _json.loads(resp.read().decode("utf-8"))
            print("  · 模型文件可访问")
        except Exception as e:
            print(f"  ⚠ 模型文件不可访问（{e}）")
    finally:
        server.shutdown()
    print("✓ 静态服务器：pet.html/pet.js/模型可经 HTTP 访问")


def test_pet_config():
    """缩放配置持久化：保存/读取/越界钳位（用临时文件，不碰真实配置）。"""
    import tempfile
    from pathlib import Path

    tmp = tempfile.mkdtemp()
    pet_mod._PET_CONFIG_FILE = Path(tmp) / "pet_config.json"
    try:
        pet_mod._save_pet_config({"scale": 1.5})
        assert pet_mod._load_pet_config()["scale"] == 1.5
        # 越界钳位
        api = pet_mod.PetApi()
        api.save_scale(99)
        assert pet_mod._load_pet_config()["scale"] == pet_mod._SCALE_MAX
        api.save_scale(-5)
        assert pet_mod._load_pet_config()["scale"] == pet_mod._SCALE_MIN
        assert api.get_config()["scale"] == pet_mod._SCALE_MIN
        # 损坏文件兜底
        pet_mod._PET_CONFIG_FILE.write_text("not json", encoding="utf-8")
        assert pet_mod._load_pet_config() == {}
        assert api.get_config()["scale"] == pet_mod._SCALE_DEFAULT
    finally:
        pet_mod._save_pet_config({})
        try:
            pet_mod._PET_CONFIG_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    print("✓ 宠物配置：缩放保存/读取/钳位/损坏兜底")


def test_client_refactor():
    import ws_audio
    import client

    assert client.PygamePlayer is ws_audio.PygamePlayer
    assert client.make_mic_stream is ws_audio.make_mic_stream
    assert ws_audio.SAMPLE_RATE == 16000 and ws_audio.CHUNK == 1600
    print("✓ ws_audio 抽取：client.py 复用同一实现（行为不变）")


if __name__ == "__main__":
    test_event_to_pet_state()
    test_ui_bridge()
    test_static_server()
    test_pet_config()
    test_client_refactor()
    print("\n全部通过 ✅")
