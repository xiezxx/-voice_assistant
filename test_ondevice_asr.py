# -*- coding: utf-8 -*-
"""端侧识别模型选型对比：同一批句子，Whisper（现状）vs 端侧候选模型。

为什么值得单独测：搬端侧最大的风险就是**识别变差**（用户已经抱怨过一次中文识别不准）。
端侧只能塞几十 MB 的模型，比电脑上的 Whisper base 小一个数量级，所以必须先量出
"到底差多少"，再决定 APK 里放哪个模型、值不值得为此把包做大。

跑法（要联网，edge-tts 合成测试音频）：
    python test_ondevice_asr.py
"""

import sys

sys.stdout.reconfigure(errors="replace")  # 中文 Windows GBK 控制台打印 ✓ 不崩

import asyncio
import os
import tempfile
from pathlib import Path

import numpy as np

from test_stt_accuracy import SENTENCES, TRADITIONAL_ONLY, mp3_to_pcm16k, score
from tts import SpeechSynthesizer

# 端侧候选模型（解压在 models/candidates/ 下）
CANDIDATES = {
    "端侧 zipformer-ctc-small-zh-int8": Path(
        "models/candidates/sherpa-onnx-zipformer-ctc-small-zh-int8-2025-07-16"
    ),
}


def load_candidate(model_dir: Path):
    """按模型目录里的文件自动挑一个 sherpa-onnx 识别器（zipformer-ctc / paraformer / sense-voice）。"""
    import sherpa_onnx

    model = model_dir / "model.int8.onnx"
    if not model.exists():
        model = model_dir / "model.onnx"
    tokens = model_dir / "tokens.txt"
    if not model.exists() or not tokens.exists():
        return None

    # 目录里的 bbpe.model 是给热词/同音字替换用的，基础解码用不上 ——
    # 实测不传它照样出通顺简体（拿模型自带的 test_wavs 试过），所以不传。
    if (model_dir / "encoder.int8.onnx").exists() or (model_dir / "encoder.onnx").exists():
        return sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(model_dir / "encoder.int8.onnx"),
            decoder=str(model_dir / "decoder.onnx"),
            joiner=str(model_dir / "joiner.int8.onnx"),
            tokens=str(tokens), num_threads=2,
        )
    return sherpa_onnx.OfflineRecognizer.from_zipformer_ctc(
        model=str(model), tokens=str(tokens), num_threads=2
    )


def transcribe_local(recognizer, audio: np.ndarray) -> str:
    s = recognizer.create_stream()
    s.accept_waveform(16000, audio)
    recognizer.decode_stream(s)
    return s.result.text.strip()


async def synth_all() -> list[tuple[str, np.ndarray]]:
    tts = SpeechSynthesizer()
    out = []
    for text in SENTENCES:
        try:
            mp3 = await tts.synthesize_stream(text)
        except Exception as e:
            print(f"⚠ 合成失败（{type(e).__name__}），跳过：{text}")
            continue
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(mp3)
            path = f.name
        try:
            out.append((text, mp3_to_pcm16k(path)))
        finally:
            os.unlink(path)
    return out


def report(name: str, rows: list[tuple[str, str, float]]) -> float:
    avg = sum(r[2] for r in rows) / len(rows)
    traditional = sorted({ch for _, got, _ in rows for ch in got if ch in TRADITIONAL_ONLY})
    print(f"\n{'=' * 72}\n【{name}】平均命中率 {avg:.0%}"
          f"{'  ⚠ 出现繁体字: ' + ''.join(traditional) if traditional else '  ✓ 简体'}")
    print("=" * 72)
    for text, got, sc in rows:
        flag = "✓" if sc >= 0.8 else ("~" if sc >= 0.5 else "✗")
        print(f"{flag} {text:<20} → {got[:34]:<36}{sc:.0%}")
    return avg


async def main():
    samples = await synth_all()
    if not samples:
        print("⚠ 一句话都没合成成功（多半是没联网），跳过")
        return

    results = {}

    # 基准：现在的 Whisper
    from stt import SpeechRecognizer
    stt = SpeechRecognizer()
    stt.load()
    rows = [(t, stt.transcribe(a, sample_rate=16000).strip(), 0.0) for t, a in samples]
    results["电脑 Whisper base（现状）"] = report(
        "电脑 Whisper base（现状）", [(t, g, score(t, g)) for t, g, _ in rows]
    )

    # 各候选
    for name, d in CANDIDATES.items():
        if not d.exists():
            print(f"\n⚠ 找不到 {d}，跳过 {name}")
            continue
        rec = load_candidate(d)
        if rec is None:
            print(f"\n⚠ {d} 里没有可识别的模型文件，跳过")
            continue
        rows = [(t, transcribe_local(rec, a), 0.0) for t, a in samples]
        results[name] = report(name, [(t, g, score(t, g)) for t, g, _ in rows])

    print(f"\n{'=' * 72}\n小结（同一批句子，同一套打分）")
    base = results.get("电脑 Whisper base（现状）", 0)
    for name, avg in results.items():
        delta = avg - base
        mark = "（基准）" if name.startswith("电脑") else f"（比现状 {delta:+.0%}）"
        print(f"  {avg:5.0%}  {name} {mark}")
    print("\n判据：端侧模型平均分不低于 Whisper 基准的 85% 才值得搬；差距过大就换更大的端侧模型")


if __name__ == "__main__":
    asyncio.run(main())
