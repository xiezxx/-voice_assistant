# -*- coding: utf-8 -*-
"""免提唤醒服务端离线测试：WAV 编解码 + 队列折叠 + 播报节奏（不联网、不起服务）。"""

import asyncio
import io
import os
import tempfile
import wave

import numpy as np

from wake_server import (
    wav_bytes_to_audio,
    pop_latest_turn,
    should_advance,
    unlink_path,
)


def _make_wav(samples: np.ndarray, sample_rate=16000, nchannels=1, sampwidth=2) -> bytes:
    """与浏览器端 encodeWav 等价的 Python 版（float32 [-1,1] → int16 WAV 字节）。"""
    if sampwidth == 2:
        # 与 web/wake_mode.js 一致：四舍五入 * 32768 后钳位到 int16 全量程
        pcm = np.clip(np.rint(np.clip(samples, -1, 1) * 32768), -32768, 32767).astype("<i2")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(nchannels)
            w.setsampwidth(sampwidth)
            w.setframerate(sample_rate)
            w.writeframes(pcm.tobytes())
        return buf.getvalue()
    # float32 WAV（非法输入测试用）
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(nchannels)
        w.setsampwidth(sampwidth)
        w.setframerate(sample_rate)
        w.writeframes(samples.astype("<f4").tobytes())
    return buf.getvalue()


def test_wav_roundtrip():
    rng = np.random.default_rng(42)
    original = rng.uniform(-1, 1, 16000).astype(np.float32)
    data = _make_wav(original)
    audio, sr = wav_bytes_to_audio(data)
    assert sr == 16000, sr
    assert audio.dtype == np.float32
    assert np.allclose(audio, original, atol=2 / 32768), "WAV 往返误差过大"
    # 短音频（最小 0.3s）
    short = rng.uniform(-0.5, 0.5, 4800).astype(np.float32)
    audio2, sr2 = wav_bytes_to_audio(_make_wav(short))
    assert len(audio2) == 4800 and sr2 == 16000
    print("✓ WAV 编解码往返（含短音频）")


def test_wav_decode_rejects():
    assert wav_bytes_to_audio(b"not a wav at all") is None, "垃圾字节应拒绝"
    assert wav_bytes_to_audio(b"") is None, "空字节应拒绝"
    # float32 WAV 应拒绝（只收 16-bit PCM）
    f32 = np.zeros(1600, dtype=np.float32)
    assert wav_bytes_to_audio(_make_wav(f32, sampwidth=4)) is None, "float32 WAV 应拒绝"
    # 立体声应拒绝
    stereo = np.zeros((1600, 2), dtype=np.float32)
    assert wav_bytes_to_audio(_make_wav(stereo, nchannels=2)) is None, "立体声应拒绝"
    # 0 帧应拒绝
    assert wav_bytes_to_audio(_make_wav(np.zeros(0, dtype=np.float32))) is None, "0 帧应拒绝"
    print("✓ 非法 WAV 全部拒绝（垃圾/空/float32/立体声/0帧）")


def test_queue_collapse():
    """多轮排队时折叠为最新一轮，被丢弃轮次的 mp3 立即删除。"""
    q = asyncio.Queue()
    tmp = tempfile.mkdtemp()
    try:
        turns = []
        for i in range(3):
            paths = []
            for j in range(2):
                p = os.path.join(tmp, f"t{i}s{j}.mp3")
                with open(p, "w") as f:
                    f.write("x")
                paths.append(p)
            turns.append(
                {"sentences": [{"text": f"{i}-{j}", "path": p} for j, p in enumerate(paths)],
                 "status": f"status{i}", "history": [i]}
            )
        q.put_nowait(turns[0])
        q.put_nowait(turns[1])
        q.put_nowait(turns[2])

        latest = pop_latest_turn(q)
        assert latest is turns[2], "应保留最新一轮"
        assert q.empty()
        # 前两轮的 mp3 已删除，最新一轮的还在
        for t in turns[:2]:
            for s in t["sentences"]:
                assert not os.path.exists(s["path"]), f"丢弃轮 mp3 应删除: {s['path']}"
        for s in turns[2]["sentences"]:
            assert os.path.exists(s["path"]), f"最新轮 mp3 应保留: {s['path']}"
        # 空队列返回 None
        assert pop_latest_turn(q) is None
        print("✓ 队列折叠：保留最新轮并清理被丢弃轮次的 mp3")
    finally:
        for root, _, files in os.walk(tmp):
            for f in files:
                unlink_path(os.path.join(root, f))
        try:
            os.rmdir(tmp)
        except OSError:
            pass


def test_should_advance():
    # 未到播完时刻 / 已暂停 → 不推进；到点且未暂停 → 推进
    assert not should_advance(ready_at=10.0, now=9.9, paused=False)
    assert not should_advance(ready_at=10.0, now=11.0, paused=True)
    assert should_advance(ready_at=10.0, now=10.0, paused=False)
    assert should_advance(ready_at=10.0, now=10.1, paused=False)
    print("✓ 播报节奏门控（到点/未到点/暂停）")


if __name__ == "__main__":
    test_wav_roundtrip()
    test_wav_decode_rejects()
    test_queue_collapse()
    test_should_advance()
    print("\n全部通过 ✅")
