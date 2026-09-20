# -*- coding: utf-8 -*-
"""生成"移植比对"的测试用例：Python 原件算出参考结果，写进 tsv，交给 Java 侧逐组比对。

为什么要这么干：句子切分、VAD 判定、工具调用组配对这三段逻辑是**踩坑踩出来的**
（18 字逗号切分、0.8s 尾静音、修永久 400 的配对规则），移植到 Java 时最怕"顺手改写"。
所以拿 Python 原件当标准答案，Java 逐组对齐 —— 移植的活儿就不再是凭感觉。

跑法：
    python gen_cases.py          # 生成 cases/*.tsv
    （然后 javac 编译并运行 Compare.java，见 README）
"""

import asyncio
import json
import random
import sys
from pathlib import Path

sys.stdout.reconfigure(errors="replace")

HERE = Path(__file__).parent
CASES = HERE / "cases"
# 把项目根目录加进 import 路径（本脚本在 android/tools/javatest 下）
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))

from speech_utils import sentence_stream          # noqa: E402
from voice_server import (                        # noqa: E402
    UtteranceCollector, BargeDetector, BARGE_STREAK,
    UTTERANCE_SILENCE_SEC, UTTERANCE_MAX_SEC,
)

SEP = "\x1f"      # 字段内多值分隔（单元分隔符，正常文本里不会出现）
US = "\x1e"       # 多条消息分隔


def esc(s: str) -> str:
    """转义 TSV 里会坏事的字符。

    换行在 TSV 里是行分隔符，而「换行也算句末符」正是要测的行为之一，
    不转义的话那条用例会被拆成两行、两边都测错。
    """
    return s.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t")


# ── 句子切分 ────────────────────────────────────────────────

SPEECH_INPUTS = [
    "你好，我是小音。今天天气不错！",
    "这是一句没有任何标点的话",                       # 残余
    "短，逗号在前",                                    # 逗号还没到 18 字，不该切
    "一二三四五六七八九十一二三四五六七八，后面还有",      # 到 18 字了，逗号处切
    "行一\n行二\n",                                    # 换行当句末符
    "问号？叹号！分号；句号。",                          # 连续切四句
    "中英混排, comma here, 还有更多内容要考虑一下，好不好",  # 半角逗号也算
    "小音小音",                                        # 极短
    "",                                                # 空
    "。",                                              # 只有标点
    "很长的一句" * 6 + "，中间断开的地方",                # 超长
]


async def speech_expected(chunks):
    out = []
    async for s in sentence_stream(_aiter(chunks)):
        out.append(s)
    return out


async def _aiter(items):
    for i in items:
        yield i


def gen_speech():
    random.seed(20260916)
    rows = []
    for i, text in enumerate(SPEECH_INPUTS):
        # 同一句话按不同方式切块，验证"切分与分块方式无关"
        splits = [([text], "整块"),
                  ([text[:1], text[1:]], "逐字两块"),
                  ([text[j:j + 3] for j in range(0, len(text), 3)], "三字一块")]
        for j, (chunks, _how) in enumerate(splits):
            exp = asyncio.run(speech_expected(chunks))
            rows.append((f"s{i}_{j}", chunks, exp))
    # 随机切块再压一批
    for i in range(30):
        text = random.choice([t for t in SPEECH_INPUTS if t])
        pos, chunks = 0, []
        while pos < len(text):
            n = random.randint(1, 4)
            chunks.append(text[pos:pos + n])
            pos += n
        exp = asyncio.run(speech_expected(chunks))
        rows.append((f"r{i}", chunks, exp))

    path = CASES / "speech.tsv"
    # newline="\n"：强制 LF。Windows 默认会把 \n 写成 \r\n，Java 读到行尾带个 \r，
    # 比对时会莫名其妙不一致（踩过）
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for cid, chunks, exp in rows:
            f.write(f"{cid}\t{SEP.join(esc(c) for c in chunks)}\t{SEP.join(esc(e) for e in exp)}\n")
    print(f"✓ 句子切分：{len(rows)} 组 → {path.name}")


# ── VAD：采集器 ─────────────────────────────────────────────
# 用"整数幅度"表示一帧的响度：幅度 m 的帧，电平恰好是 m/32768（两边都这么算，不存在浮点误差）

def gen_utterance():
    random.seed(20260916)
    cases = []
    # 手工构造的典型场景
    cases.append(("quiet", [5] * 20, 0.02))                             # 全程安静：永不 done
    cases.append(("speech_then_silence", [5, 5, 900] + [900] * 5 + [5] * 8, 0.02))  # 说完
    cases.append(("short_then_silence", [5] * 3 + [900] + [5] * 8, 0.02))            # 只一个字
    cases.append(("continuous", [900] * 200, 0.02))                     # 一直说 → 撞最长上限
    # 随机序列
    for i in range(120):
        n = random.randint(5, 160)
        thr = random.choice([0.006, 0.02, 0.05, 0.12])
        mags = [random.choice([0, 30, 200, 800, 2000, 5000]) for _ in range(n)]
        cases.append((f"r{i}", mags, thr))

    rows = []
    for cid, mags, thr in cases:
        col = UtteranceCollector(threshold=thr)
        done_at, has_speech = -1, False
        for k, m in enumerate(mags):
            import numpy as np
            frame = np.full(1600, m / 32768.0, dtype=np.float32)
            if col.feed(frame):
                done_at = k
                break
        has_speech = col.has_speech
        rows.append((cid, thr, mags, done_at, col.frame_count, int(has_speech)))

    path = CASES / "utterance.tsv"
    # newline="\n"：强制 LF。Windows 默认会把 \n 写成 \r\n，Java 读到行尾带个 \r，
    # 比对时会莫名其妙不一致（踩过）
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for cid, thr, mags, done_at, frames, hs in rows:
            f.write(f"{cid}\t{thr}\t{','.join(map(str, mags))}\t{done_at}\t{frames}\t{hs}\n")
    print(f"✓ 采集器：{len(rows)} 组 → {path.name}")
    print(f"  （尾静音 {UTTERANCE_SILENCE_SEC}s / 最长 {UTTERANCE_MAX_SEC}s）")


# ── VAD：打断检测 ───────────────────────────────────────────

def gen_barge():
    random.seed(20260916)
    cases = [("clean", [0, 0, 900, 900, 900, 900], 0.02),
             ("interrupted", [900, 0, 900, 900, 0, 900, 900, 900], 0.02),
             ("never", [10] * 30, 0.02)]
    for i in range(60):
        mags = [random.choice([0, 50, 400, 1500]) for _ in range(random.randint(4, 40))]
        cases.append((f"r{i}", mags, random.choice([0.006, 0.02, 0.08])))

    rows = []
    for cid, mags, thr in cases:
        d = BargeDetector(threshold=thr)
        hit = -1
        for k, m in enumerate(mags):
            if d.feed(m / 32768.0):
                hit = k
                break
        rows.append((cid, thr, mags, hit))

    path = CASES / "barge.tsv"
    # newline="\n"：强制 LF。Windows 默认会把 \n 写成 \r\n，Java 读到行尾带个 \r，
    # 比对时会莫名其妙不一致（踩过）
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for cid, thr, mags, hit in rows:
            f.write(f"{cid}\t{thr}\t{','.join(map(str, mags))}\t{hit}\t{BARGE_STREAK}\n")
    print(f"✓ 打断检测：{len(rows)} 组 → {path.name}（连续 {BARGE_STREAK} 块）")


# ── 工具调用组配对 ──────────────────────────────────────────
# 消息编码：role|content|toolCalls|toolCallId，toolCalls 是 "id:name:args" 逗号分隔

def _ref_build(conv):
    """llm.py:_build_messages 的参考实现（原样抄，只把输出换成紧凑描述）。"""
    msgs = conv[-30:]
    items = []
    for m in msgs:
        role = m.get("role", "")
        if role not in ("user", "assistant", "tool"):
            continue
        content = m.get("content")
        if role != "assistant" and not content:
            continue
        items.append(m)
    out = []
    group_at = -1
    expected = []
    for m in items:
        if m["role"] == "tool":
            if expected and m.get("tool_call_id") == expected[0]:
                expected.pop(0)
                out.append(m)
            continue
        if expected:
            del out[group_at:]
            expected = []
        out.append(m)
        if m["role"] == "assistant" and m.get("tool_calls"):
            expected = [tc["id"] for tc in m["tool_calls"]]
            group_at = len(out) - 1
    if expected:
        del out[group_at:]
    return out


def _describe(msgs):
    """和 Java MessageList.describe 输出格式保持一致。"""
    parts = []
    for m in msgs:
        s = m["role"]
        if m.get("tool_calls"):
            s += "(calls:" + ",".join(sorted(tc["id"] for tc in m["tool_calls"])) + ")"
        if m["role"] == "tool":
            s += f"({m.get('tool_call_id', '')})"
        parts.append(s)
    return " | ".join(parts)


def _tc(cid):
    return {"id": cid, "type": "function",
            "function": {"name": "play_music", "arguments": "{}"}}


def _encode(conv):
    def enc(m):
        calls = ""
        if m.get("tool_calls"):
            calls = ",".join(f"{tc['id']}:f:{tc['function']['arguments']}" for tc in m["tool_calls"])
        return f"{m['role']}|{m.get('content') or ''}|{calls}|{m.get('tool_call_id') or ''}"
    return US.join(enc(m) for m in conv)


def gen_messages():
    filler = []
    for i in range(15):
        filler += [{"role": "user", "content": f"问{i}"},
                   {"role": "assistant", "content": f"答{i}"}]
    cases = [
        ("normal", [{"role": "user", "content": "你好"},
                    {"role": "assistant", "content": "你好呀"}]),
        ("complete_group", [{"role": "user", "content": "放首歌"},
                            {"role": "assistant", "content": None, "tool_calls": [_tc("call_1")]},
                            {"role": "tool", "tool_call_id": "call_1", "content": "已播放"},
                            {"role": "assistant", "content": "放好了"}]),
        ("orphan_tool", [{"role": "user", "content": "放首歌"},
                         {"role": "assistant", "content": None, "tool_calls": [_tc("call_1")]},
                         {"role": "tool", "tool_call_id": "call_1", "content": "已播放"}] + filler[-26:]),
        ("incomplete_group", [{"role": "user", "content": "放首歌"},
                              {"role": "assistant", "content": None,
                               "tool_calls": [_tc("call_1"), _tc("call_2")]},
                              {"role": "tool", "tool_call_id": "call_1", "content": "已播放"},
                              {"role": "user", "content": "你好"},
                              {"role": "assistant", "content": "你好呀"}]),
        ("dangling_tail", [{"role": "user", "content": "放首歌"},
                           {"role": "assistant", "content": None, "tool_calls": [_tc("call_9")]}]),
        ("two_groups", [{"role": "user", "content": "放首歌"},
                        {"role": "assistant", "content": None, "tool_calls": [_tc("a")]},
                        {"role": "tool", "tool_call_id": "a", "content": "ok"},
                        {"role": "assistant", "content": "放好了"},
                        {"role": "user", "content": "再来一首"},
                        {"role": "assistant", "content": None, "tool_calls": [_tc("b")]},
                        {"role": "tool", "tool_call_id": "b", "content": "ok"}]),
        ("empty_content_user", [{"role": "user", "content": ""},
                                {"role": "assistant", "content": "你说啥"}]),
        ("unknown_role", [{"role": "system", "content": "不该出现"},
                          {"role": "user", "content": "在吗"}]),
    ]
    path = CASES / "messages.tsv"
    # newline="\n"：强制 LF。Windows 默认会把 \n 写成 \r\n，Java 读到行尾带个 \r，
    # 比对时会莫名其妙不一致（踩过）
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for cid, conv in cases:
            f.write(f"{cid}\t{_encode(conv)}\t{_describe(_ref_build(conv))}\n")
    print(f"✓ 工具组配对：{len(cases)} 组 → {path.name}")


if __name__ == "__main__":
    CASES.mkdir(parents=True, exist_ok=True)
    random.seed(20260916)
    gen_speech()
    gen_utterance()
    gen_barge()
    gen_messages()
    print("\n用例已生成，接下来用 javac 编译 Java 侧比对")
