# -*- coding: utf-8 -*-
"""上下文配平测试：`_build_messages()` 给出的消息列表必须永远是 API 能接受的形状。

为什么值得单独测：工具调用（点歌/天气/日程）要求「assistant(tool_calls) 紧跟它要的
每一条 tool 结果」，而 `conversation[-30:]` 是从中间截断的，天然会截出两种残组。
畸形历史会让 DeepSeek 直接 400，而 400 之后回滚会把上下文恢复原样 —— 窗口起点不变，
于是**每一轮都 400，永久失败**（用户实际踩到过：界面上一直「LLM 调用失败」）。

所以这里不联网，只断言形状；真正的 400 报文见 README 里的记录。
"""

import sys

sys.stdout.reconfigure(errors="replace")  # 中文 Windows GBK 控制台打印 ✓ 不崩

from llm import ChatBot


def _bot_with(conv):
    bot = ChatBot()
    bot.conversation = conv
    return bot


def _filler(n):
    """n 组无关的问答，用来把窗口撑到截断线以外。"""
    out = []
    for i in range(n):
        out.append({"role": "user", "content": f"问{i}"})
        out.append({"role": "assistant", "content": f"答{i}"})
    return out


def _call(cid, name="play_music", args="{}"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}


def test_normal_conversation_untouched():
    """普通对话不该被动任何一刀。"""
    conv = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好呀"}]
    msgs = _bot_with(conv)._build_messages()
    assert [m["role"] for m in msgs] == ["user", "assistant"], msgs
    print("✓ 普通对话原样保留")


def test_complete_tool_group_kept():
    """完整的工具调用组要留下 —— 别把正常功能误删了。"""
    conv = [
        {"role": "user", "content": "放首晴天"},
        {"role": "assistant", "content": None, "tool_calls": [_call("call_1")]},
        {"role": "tool", "tool_call_id": "call_1", "content": "已播放"},
        {"role": "assistant", "content": "放好了"},
    ]
    msgs = _bot_with(conv)._build_messages()
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant", "tool", "assistant"], roles
    assert msgs[1]["tool_calls"][0]["id"] == "call_1", msgs[1]
    print("✓ 完整的工具调用组被保留")


def test_orphan_tool_message_dropped():
    """窗口第一条正好是 tool 消息（本 bug 的主因）：必须丢掉，不能开头就是 tool。"""
    head = [
        {"role": "user", "content": "放首歌"},
        {"role": "assistant", "content": None, "tool_calls": [_call("call_1")]},
        {"role": "tool", "tool_call_id": "call_1", "content": "已播放"},
    ]
    tail = [{"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
            {"role": "user", "content": "在吗"}]
    # 补到 32 条：这样 conversation[-30] 正好是 head 里那条 tool 消息（索引 2）
    n = (32 - len(head) - len(tail)) // 2
    conv = head + _filler(n) + tail
    bot = _bot_with(conv)
    # 前置条件：窗口的第一条确实落在 tool 消息上（不然这个测试就没测到点上）
    assert bot.conversation[-30]["role"] == "tool", bot.conversation[-30]
    msgs = bot._build_messages()
    assert msgs[0]["role"] != "tool", f"开头还是 tool 消息：{msgs[0]}"
    assert not any(m["role"] == "tool" for m in msgs), msgs
    # 后面的正常对话不能被牵连
    assert [m["content"] for m in msgs if m["role"] == "user"][-1] == "在吗", msgs
    print("✓ 窗口开头的孤儿 tool 消息被丢掉")


def test_incomplete_group_midway_dropped():
    """工具调用没凑齐就来下一句（被打断后接着问）：整组丢掉，不能留下裸调用。"""
    conv = [
        {"role": "user", "content": "放首歌"},
        {"role": "assistant", "content": None, "tool_calls": [_call("call_1"), _call("call_2")]},
        {"role": "tool", "tool_call_id": "call_1", "content": "已播放"},   # 少了 call_2
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好呀"},
    ]
    bot = _bot_with(conv)
    assert any(m.get("tool_calls") for m in bot.conversation[-30:]), "前提不成立：这组没在窗口里"
    msgs = bot._build_messages()
    calls = [m for m in msgs if m["role"] == "assistant" and m.get("tool_calls")]
    assert not calls, f"残缺的 tool_calls 没被丢掉：{calls}"
    assert not [m for m in msgs if m["role"] == "tool"], "留下了没有归属的 tool 结果"
    assert [m["content"] for m in msgs if m["role"] == "user"][-1] == "你好", msgs
    print("✓ 没凑齐的工具调用整组被丢掉")


def test_dangling_group_at_the_end_dropped():
    """被打断留下的"结尾裸调用"也要清掉。"""
    conv = [{"role": "user", "content": "放首歌"},
            {"role": "assistant", "content": None, "tool_calls": [_call("call_9")]}]
    msgs = _bot_with(conv)._build_messages()
    assert [m["role"] for m in msgs] == ["user"], msgs
    print("✓ 结尾的裸工具调用被丢掉")


def test_every_shape_is_api_legal():
    """总检：随便怎么截，出来的列表都要满足 API 的两条硬规则。"""
    cases = [
        [{"role": "user", "content": "放首歌"},
         {"role": "assistant", "content": None, "tool_calls": [_call("a")]},
         {"role": "tool", "tool_call_id": "a", "content": "ok"}] + _filler(20),
        [{"role": "assistant", "content": None, "tool_calls": [_call("a")]}] + _filler(20),
        _filler(20) + [{"role": "tool", "tool_call_id": "zzz", "content": "孤儿"}],
        _filler(3),
    ]
    for conv in cases:
        msgs = _bot_with(conv)._build_messages()
        # 规则一：tool 消息前面必须有带 tool_calls 的 assistant
        for i, m in enumerate(msgs):
            if m["role"] == "tool":
                assert i > 0 and msgs[i - 1]["role"] in ("tool", "assistant"), msgs
                assert any(
                    x["role"] == "assistant" and x.get("tool_calls") for x in msgs[:i]
                ), f"孤儿 tool 消息：{msgs}"
        # 规则二：assistant 的每个 tool_call_id 都要有对应的 tool 结果跟着
        for i, m in enumerate(msgs):
            if m["role"] == "assistant" and m.get("tool_calls"):
                want = [tc["id"] for tc in m["tool_calls"]]
                got = []
                for nxt in msgs[i + 1:]:
                    if nxt["role"] != "tool":
                        break
                    got.append(nxt["tool_call_id"])
                assert want == got, f"tool_calls 与结果不配对：{want} vs {got}"
    print(f"✓ {len(cases)} 种截断形状全部满足 API 的两条硬规则")


if __name__ == "__main__":
    test_normal_conversation_untouched()
    test_complete_tool_group_kept()
    test_orphan_tool_message_dropped()
    test_incomplete_group_midway_dropped()
    test_dangling_group_at_the_end_dropped()
    test_every_shape_is_api_legal()
    print("\n全部通过 ✅")
