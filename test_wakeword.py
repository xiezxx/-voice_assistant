# -*- coding: utf-8 -*-
"""唤醒词模块测试：重点验证未配置时的优雅降级（真机检测需 AccessKey + 模型）。"""

from config import Config
from wakeword import wakeword_available


def test_graceful_degradation():
    ok, reason = wakeword_available()
    if not Config.PICOVOICE_ACCESS_KEY:
        assert ok is False, "未配置 AccessKey 时应不可用"
        assert "PICOVOICE_ACCESS_KEY" in reason, reason
        print("✓ 未配置 AccessKey 时优雅降级:", reason)
    else:
        print(f"✓ 已配置 AccessKey，可用性: {ok}（{reason}）")


if __name__ == "__main__":
    test_graceful_degradation()
    print("全部通过 ✅")
