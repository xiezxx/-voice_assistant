# -*- coding: utf-8 -*-
"""声纹锁定离线测试：真实 CAM++ 模型加载 + 同人通过 / 静音拒绝。"""

import sys

sys.stdout.reconfigure(errors="replace")  # 中文 Windows GBK 控制台打印 ✓ 不崩

import numpy as np

from speaker import SpeakerVerifier, speaker_available


def _load_wake_audio() -> np.ndarray:
    """用 edge-tts 合成「小音小音」→ 16k 单声道 float32（与其余测试同源）。"""
    import edge_tts
    import pygame
    from scipy.signal import resample

    import asyncio

    asyncio.run(edge_tts.Communicate("小音小音", "zh-CN-XiaoyiNeural").save("wake_test.mp3"))
    pygame.mixer.init()
    sr_raw = pygame.mixer.get_init()[0]
    import pygame.sndarray

    raw = pygame.sndarray.array(pygame.mixer.Sound("wake_test.mp3"))
    audio = raw.astype(np.float32).mean(axis=1) / 32768.0
    return resample(audio, int(len(audio) * 16000 / sr_raw)).astype(np.float32)


def test_speaker_verifier():
    ok, reason = speaker_available()
    if not ok:
        print(f"⚠ 跳过声纹测试（{reason}）")
        return

    try:
        audio = _load_wake_audio()
    except Exception as e:
        print(f"⚠ 跳过声纹测试（测试音频合成失败：{e}）")
        return

    verifier = SpeakerVerifier()
    manager = verifier.create_manager()

    emb1 = verifier.extract(audio)
    assert isinstance(emb1, list) and len(emb1) > 100, f"声纹维度异常: {len(emb1)}"
    manager.add("主人", emb1)

    # 同一段语音应命中
    emb2 = verifier.extract(audio)
    assert manager.search(emb2, verifier.threshold) == "主人", "同人声纹应命中"

    # 静音不应命中（不同人同理）
    silence = np.zeros(16000 * 3, dtype=np.float32)
    emb3 = verifier.extract(silence)
    assert manager.search(emb3, verifier.threshold) == "", "静音不应命中主人声纹"

    print(f"✓ 声纹锁定：真实模型加载 + 同人命中 + 静音拒绝（维度 {len(emb1)}）")


if __name__ == "__main__":
    test_speaker_verifier()
    print("\n全部通过 ✅")
