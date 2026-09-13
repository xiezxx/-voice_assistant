# -*- coding: utf-8 -*-
"""唤醒词模块测试：唤醒短语匹配 + 模式选择 + sherpa 模型加载（麦克风检测需真机验证）。"""

import sys

sys.stdout.reconfigure(errors="replace")  # 中文 Windows GBK 控制台打印 ✓ 不崩

import numpy as np

from wakeword import (
    is_wake_phrase,
    create_wake_listener,
    picovoice_available,
    sherpa_available,
)


def test_is_wake_phrase():
    for text in ["小音", "小音小音", "小音，小音", "你好小音在吗", "小英小英"]:
        assert is_wake_phrase(text), f"应命中: {text}"
    for text in ["你好", "现在几点", "今天天气怎么样", "小艺"]:
        assert not is_wake_phrase(text), f"不应命中: {text}"
    print("✓ 唤醒短语匹配（含同音字与标点容忍）")


def test_sherpa_pipeline():
    """sherpa-onnx 模型加载 + 静音输入不误报（真实唤醒需真机验证）。"""
    ok, reason = sherpa_available()
    if not ok:
        print(f"⚠ 跳过 sherpa 测试（{reason}）")
        return
    from wakeword import KwsFeedDetector

    detector = KwsFeedDetector()
    stream = detector.create_stream()
    # 喂 2 秒静音，不应检测到关键词
    silence = np.zeros(16000 * 2, dtype=np.float32)
    result = detector.feed(stream, silence)
    assert not result, f"静音不应触发: {result}"
    print("✓ sherpa 模型加载成功，静音不误报（KwsFeedDetector.feed）")


def test_sherpa_e2e():
    """端到端：edge-tts 合成「小音小音」→ 应检测到唤醒词（需联网）。"""
    ok, reason = sherpa_available()
    if not ok:
        print(f"⚠ 跳过端到端测试（{reason}）")
        return
    import asyncio

    import edge_tts
    import pygame
    from scipy.signal import resample

    asyncio.run(edge_tts.Communicate("小音小音", "zh-CN-XiaoyiNeural").save("wake_test.mp3"))

    pygame.mixer.init()
    sr_raw = pygame.mixer.get_init()[0]
    import pygame.sndarray

    raw = pygame.sndarray.array(pygame.mixer.Sound("wake_test.mp3"))
    audio = raw.astype(np.float32).mean(axis=1) / 32768.0
    audio = resample(audio, int(len(audio) * 16000 / sr_raw))

    from wakeword import KwsFeedDetector

    detector = KwsFeedDetector()
    stream = detector.create_stream()
    found = ""
    chunk = 1600
    for i in range(0, len(audio), chunk):
        part = audio[i:i + chunk].astype(np.float32)
        if len(part) < chunk:
            part = np.pad(part, (0, chunk - len(part)))
        r = detector.feed(stream, part)
        if r:
            found = r
            break
    if not found:
        found = detector.feed(stream, np.zeros(8000, dtype=np.float32))
    assert found, "合成语音未触发唤醒词"
    print(f"✓ 端到端唤醒检测: TTS「小音小音」→ 检测到 {found}")


def test_listener_selection():
    ok, reason = sherpa_available()
    if ok:
        # CLI 监听器（麦克风版）仍可构造——重构后的回归面
        from wakeword import SherpaKwsWakeWordListener
        cli_listener = SherpaKwsWakeWordListener()
        assert cli_listener._detector is not None
        listener, mode = create_wake_listener(None)
        assert mode == "sherpa", mode
        print("✓ 优先选择 sherpa-onnx 本地方案（CLI 监听器构造正常）")
        return
    ok, reason = picovoice_available()
    if ok:
        print("✓ Picovoice 已配置，使用低延迟方案")
        return
    # 都未配置时自动回落到 ASR 方案（不打开麦克风，创建安全）
    class FakeSTT:
        pass
    listener, mode = create_wake_listener(FakeSTT())
    assert mode == "asr", mode
    from wakeword import AsrWakeWordListener
    assert isinstance(listener, AsrWakeWordListener)
    print(f"✓ 自动回落 ASR 方案（{reason}）")


if __name__ == "__main__":
    test_is_wake_phrase()
    test_sherpa_pipeline()
    test_sherpa_e2e()
    test_listener_selection()
    print("全部通过 ✅")
