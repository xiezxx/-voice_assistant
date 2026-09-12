# -*- coding: utf-8 -*-
"""语音会话服务端离线测试：帧切分 + VAD 采集 + 打断检测 + 队列折叠 + 播报节奏。"""

import asyncio
import os
import tempfile

import numpy as np

from voice_server import (
    FRAME_BYTES,
    UTTERANCE_SILENCE_SEC,
    iter_frames,
    UtteranceCollector,
    BargeDetector,
    pop_latest_turn,
    should_advance,
    unlink_path,
)

_SIL_FRAMES = int(UTTERANCE_SILENCE_SEC * 10)  # 尾静音判定块数（随配置变化）


def test_iter_frames():
    # 小于一帧：全部余留
    frames, rest = iter_frames(b"x" * 7)
    assert frames == [] and rest == b"x" * 7
    # 恰好一帧 / 多帧 + 余留
    frames, rest = iter_frames(b"a" * FRAME_BYTES)
    assert len(frames) == 1 and frames[0] == b"a" * FRAME_BYTES and rest == b""
    frames, rest = iter_frames(b"b" * (FRAME_BYTES * 2 + 1))
    assert len(frames) == 2 and rest == b"b"
    # 多次喂入可拼帧（模拟网络分片）
    frames, rest = iter_frames(b"c" * 100)
    assert frames == [] and len(rest) == 100
    frames, rest = iter_frames(rest + b"d" * (FRAME_BYTES - 100))
    assert len(frames) == 1
    print("✓ 帧切分（不足/恰好/多帧/跨消息拼接）")


def _make_chunk(level: float, n: int = 1600) -> np.ndarray:
    return np.full(n, level, dtype=np.float32)


def test_utterance_collector():
    # 长静音预卷 → 0.5s 语音 → 尾静音满额 → done，且超长前置静音被截到预卷上限
    c = UtteranceCollector()
    for _ in range(20):  # 2s 静音预卷（超过预卷上限，应被截掉一半）
        assert not c.feed(_make_chunk(0.0))
    for _ in range(5):  # 0.5s 语音
        assert not c.feed(_make_chunk(0.5))
    for _ in range(_SIL_FRAMES - 1):  # 尾静音差一块（不结束）
        assert not c.feed(_make_chunk(0.0))
    assert c.feed(_make_chunk(0.0))  # 最后一块静音 → done
    audio = c.take()
    blocks = len(audio) // 1600
    assert blocks <= _SIL_FRAMES + 5 + _SIL_FRAMES, f"前置静音应被截断: {blocks} 块"
    assert blocks >= 5, f"语音不应被截掉: {blocks} 块"
    # 短语音（0.2s）+ 尾静音也会 done（含尾静音，由调用方按总时长过滤，对齐 audio_utils）
    c = UtteranceCollector()
    for _ in range(2):
        assert not c.feed(_make_chunk(0.5))
    for _ in range(_SIL_FRAMES + 2):
        c.feed(_make_chunk(0.0))
    assert c.done and len(c.take()) == (2 + _SIL_FRAMES) * 1600
    # 长语音 12s 上限强制结束
    c = UtteranceCollector()
    for i in range(12 * 10):
        if c.feed(_make_chunk(0.5)):
            break
    assert c.done
    # 无语音静音永不结束
    c = UtteranceCollector()
    for _ in range(50):
        assert not c.feed(_make_chunk(0.0))
    print("✓ VAD 采集（预卷截断/尾静音/12s 上限/纯静音不触发）")


def test_barge_detector():
    d = BargeDetector()
    assert not d.feed(_make_chunk(0.5))  # 第 1 块
    assert not d.feed(_make_chunk(0.5))  # 第 2 块
    assert d.feed(_make_chunk(0.5))      # 第 3 块 → 触发（0.3s 连续语音）
    # 静音重置后重新计数
    d = BargeDetector()
    d.feed(_make_chunk(0.5))
    d.feed(_make_chunk(0.0))
    assert not d.feed(_make_chunk(0.5))
    assert not d.feed(_make_chunk(0.5))
    assert d.feed(_make_chunk(0.5))
    print("✓ 打断检测（3 块触发/静音重置）")


def test_vad_calibration():
    """唤醒电平自适应：说话轻的人得到更低的 VAD 阈值，范围 [基础/4, 基础]。"""
    from voice_server import _Session, VAD_THRESHOLD

    s = _Session.__new__(_Session)  # 绕过 __init__，只测纯校准逻辑
    s._level_ring = []
    assert s._calibrate_vad() == VAD_THRESHOLD, "无电平数据时保持基础阈值"
    s._level_ring = [0.005, 0.01, 0.008]  # 唤醒语音电平 0.01（较轻）
    t = s._calibrate_vad()
    assert VAD_THRESHOLD / 4 <= t <= VAD_THRESHOLD / 2, t
    s._level_ring = [0.5, 0.4]  # 大声说话：封顶在基础阈值
    assert s._calibrate_vad() == VAD_THRESHOLD
    s._level_ring = [0.001]  # 极轻：压到下限
    assert s._calibrate_vad() == VAD_THRESHOLD / 4
    print("✓ VAD 阈值自适应校准（轻说话降阈值/大声封顶/下限保护）")


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
        for t in turns[:2]:
            for s in t["sentences"]:
                assert not os.path.exists(s["path"]), f"丢弃轮 mp3 应删除: {s['path']}"
        for s in turns[2]["sentences"]:
            assert os.path.exists(s["path"]), f"最新轮 mp3 应保留: {s['path']}"
        # 空队列返回 None；sentences 为空的轮次不炸
        assert pop_latest_turn(q) is None
        q.put_nowait({"sentences": [], "status": "x", "history": []})
        assert pop_latest_turn(q) is not None
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
    assert not should_advance(ready_at=10.0, now=9.9, paused=False)
    assert not should_advance(ready_at=10.0, now=11.0, paused=True)
    assert should_advance(ready_at=10.0, now=10.0, paused=False)
    assert should_advance(ready_at=10.0, now=10.1, paused=False)
    print("✓ 播报节奏门控（到点/未到点/暂停）")


if __name__ == "__main__":
    test_iter_frames()
    test_utterance_collector()
    test_barge_detector()
    test_vad_calibration()
    test_queue_collapse()
    test_should_advance()
    print("\n全部通过 ✅")
