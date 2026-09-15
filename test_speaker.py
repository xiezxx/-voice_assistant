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


def test_speech_only():
    """掐静音：唤醒时手上是"唤醒词前后 4 秒"，真正的语音可能只有 1 秒 ——
    不掐掉静音就等于拿"房间的声纹"去比对，这是识别不准的主因。"""
    silence = np.zeros(16000 * 3, dtype=np.float32)
    assert SpeakerVerifier.speech_only(silence) is None, "整段静音应判为无效"

    audio = _load_wake_audio()
    assert SpeakerVerifier.speech_only(audio[:1600]) is None, "0.1 秒应判为太短"

    # 前后各垫 1 秒静音：掐完应该只剩真正出声的那一段。
    # 注意 edge-tts 合成出来的音频尾巴本身也带约 0.9 秒静音，所以不能拿"整段音频长度"当基准，
    # 得另算一个参考值：帧能量超过峰值 1% 的才算有声。
    padded = np.concatenate([np.zeros(16000, dtype=np.float32), audio,
                             np.zeros(16000, dtype=np.float32)])
    cut = SpeakerVerifier.speech_only(padded)
    assert cut is not None, "垫了静音也应该能掐出来"
    n = len(padded) // 1600
    rms = np.sqrt((padded[: n * 1600].reshape(n, 1600) ** 2).mean(axis=1))
    loud = np.nonzero(rms >= rms.max() * 0.01)[0]
    expect = (int(loud[-1]) - int(loud[0]) + 1) * 1600
    assert abs(len(cut) - expect) <= 3200, \
        f"掐完应≈有声段：{len(cut)} vs 参考 {expect}"
    assert np.abs(cut).max() > 0, "掐完不该是静音"
    print(f"✓ 掐静音：3 秒静音判无效 / 0.1 秒判太短 / "
          f"{len(padded)} 样本（含 2 秒垫静音）→ {len(cut)} 样本（有声段）")


def test_similarity_and_adaptive():
    """相似度必须落在 [-1,1] 且同人高于阈值、静音低于阈值（阈值 0.6 的依据）。

    这条专门防"没除以模长"：sherpa 的向量没归一化，漏了归一化会让相似度算成
    几十上百，阈值就永远通过 —— 声纹锁定等于没有。
    """
    audio = _load_wake_audio()
    v = SpeakerVerifier()
    e1, e2 = v.extract(audio), v.extract(audio)
    same = v.similarity(e1, e2)
    assert -1.0 <= same <= 1.0 + 1e-6, f"相似度算出 {same}，不可能是余弦（忘了归一化？）"
    assert same > 0.99, f"同一段音频自比应≈1：{same:.3f}"
    silence = np.zeros(16000, dtype=np.float32)
    diff = v.similarity(e1, v.extract(silence))
    assert -1.0 <= diff <= 1.0, f"相似度越界：{diff}"
    assert diff < v.threshold, f"静音不该过阈值：{diff:.3f} >= {v.threshold}"
    print(f"✓ 相似度：同段自比 {same:.3f} / 静音 {diff:.3f}（阈值 {v.threshold:.2f}，均在 [-1,1] 内）")


if __name__ == "__main__":
    test_speaker_verifier()
    test_speech_only()
    test_similarity_and_adaptive()
    print("\n全部通过 ✅")
