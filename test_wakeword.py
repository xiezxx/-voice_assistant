# -*- coding: utf-8 -*-
"""唤醒词模块测试：唤醒短语匹配 + 模式选择（麦克风检测需真机验证）。"""

from wakeword import is_wake_phrase, create_wake_listener, picovoice_available


def test_is_wake_phrase():
    for text in ["小音", "小音小音", "小音，小音", "你好小音在吗", "小英小英"]:
        assert is_wake_phrase(text), f"应命中: {text}"
    for text in ["你好", "现在几点", "今天天气怎么样", "小艺"]:
        assert not is_wake_phrase(text), f"不应命中: {text}"
    print("✓ 唤醒短语匹配（含同音字与标点容忍）")


def test_listener_selection():
    ok, reason = picovoice_available()
    if ok:
        print("✓ Picovoice 已配置，使用低延迟方案")
        return
    # 未配置时自动回落到 ASR 方案（不打开麦克风，创建安全）
    class FakeSTT:
        pass
    listener, mode = create_wake_listener(FakeSTT())
    assert mode == "asr", mode
    from wakeword import AsrWakeWordListener
    assert isinstance(listener, AsrWakeWordListener)
    print(f"✓ 自动回落 ASR 方案（{reason}）")


if __name__ == "__main__":
    test_is_wake_phrase()
    test_listener_selection()
    print("全部通过 ✅")
