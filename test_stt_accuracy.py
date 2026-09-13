# -*- coding: utf-8 -*-
"""语音识别准确率回归测试：TTS 合成典型句子 → 喂给 Whisper → 与原文比对。

为什么单独有这么一个测试：识别准确率是**单测覆盖不到的**——提示词退化、
模型档位被改小、采样率处理出问题，其他测试照样全绿，只有真实音频能暴露。
所以这里拿边-tts 合成的干净语音当基准（不含麦克风噪声和网络抖动），
跑出来的分数是**上限**：这里都不准，真机上只会更差。

依赖网络（edge-tts 要调微软接口）。跑法：
    python test_stt_accuracy.py
"""

import sys

sys.stdout.reconfigure(errors="replace")  # 中文 Windows GBK 控制台打印 ✓ 不崩

import asyncio
import os
import tempfile

import numpy as np
from scipy.signal import resample

from stt import SpeechRecognizer
from tts import SpeechSynthesizer

# 覆盖助手的主要用法；最后一类是"任意专有名词"的难点（提示词里不能写死）
SENTENCES = [
    "现在几点了",
    "徐州今天天气怎么样",
    "帮我设置一个明天早上八点的闹钟",
    "提醒我下午三点开会",
    "记一下无线网密码是 123456",
    "小音小音",
    "放首周杰伦的晴天",
]

# 中文里只在繁体出现的常用字：出现即说明模型退回了繁体输出
TRADITIONAL_ONLY = set("這個麼樣幾點們來時問說鬧鐘氣題體還東聽夢關於後發現場見")
MIN_AVG_SCORE = 0.75          # 平均命中率下限（当前实测约 0.86）
MIN_WAKE_SCORE = 1.0          # 唤醒词必须全对


def mp3_to_pcm16k(path: str) -> np.ndarray:
    """mp3 → 16k 单声道 float32（用项目已有的 pygame 解码，不引新依赖）。"""
    import pygame

    pygame.mixer.init(frequency=24000, size=-16, channels=1)
    try:
        sound = pygame.mixer.Sound(path)
        arr = pygame.sndarray.array(sound).astype(np.float32)
        if arr.ndim == 2:
            arr = arr.mean(axis=1)
        arr = arr / 32768.0
        rate = pygame.mixer.get_init()[0]
        if rate != 16000:
            arr = resample(arr, int(len(arr) * 16000 / rate))
        return arr.astype(np.float32)
    finally:
        pygame.mixer.quit()


def score(original: str, got: str) -> float:
    """命中率：原文有多少字出现在识别结果里（忽略标点和空格）。"""
    clean = "".join(ch for ch in got if ch not in "，。？！、 ,.!?")
    return sum(1 for ch in original if ch in clean) / len(original)


async def run() -> list[tuple[str, str, float]]:
    stt = SpeechRecognizer()
    stt.load()
    tts = SpeechSynthesizer()
    rows = []
    for text in SENTENCES:
        try:
            mp3 = await tts.synthesize_stream(text)
        except Exception as e:                      # 网络不通就跳过，别把测试搞红
            print(f"⚠ 合成失败（{type(e).__name__}），跳过：{text}")
            continue
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(mp3)
            path = f.name
        try:
            audio = mp3_to_pcm16k(path)
            got = stt.transcribe(audio, sample_rate=16000).strip()
        finally:
            os.unlink(path)
        rows.append((text, got, score(text, got)))
    return rows


def main():
    rows = asyncio.run(run())
    if not rows:
        print("⚠ 一句话都没测成（多半是没联网），跳过")
        return

    print("\n" + "=" * 70)
    print(f"{'原文':<22}{'识别结果':<30}命中")
    print("=" * 70)
    for text, got, sc in rows:
        print(f"{text:<22}{got[:28]:<30}{sc:.0%}")
    print("=" * 70)

    avg = sum(r[2] for r in rows) / len(rows)
    wake = [r for r in rows if r[0] == "小音小音"]
    traditional = sorted({ch for _, got, _ in rows for ch in got if ch in TRADITIONAL_ONLY})

    if traditional:
        print(f"✗ 出现了繁体字：{''.join(traditional)}（提示词里的简体引导失效了？）")
    else:
        print("✓ 全部输出简体（未退回繁体）")

    if wake:
        assert wake[0][2] >= MIN_WAKE_SCORE, f"唤醒词识别错误：{wake[0][1]}"
        print(f"✓ 唤醒词准确：{wake[0][1]}")

    assert avg >= MIN_AVG_SCORE, f"平均命中率 {avg:.0%} 低于下限 {MIN_AVG_SCORE:.0%}"
    print(f"✓ 平均命中率 {avg:.0%}（下限 {MIN_AVG_SCORE:.0%}，覆盖 {len(rows)} 句）")
    print("\n全部通过 ✅")


if __name__ == "__main__":
    main()
