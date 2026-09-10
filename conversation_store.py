"""对话持久化：把界面历史与 API 上下文保存到本地 JSON，重启后恢复。"""

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
HISTORY_FILE = DATA_DIR / "conversation.json"


def save_conversation(history: list, conversation: list):
    """保存对话：history 为界面消息 [{role, content}]，conversation 为 API 上下文。"""
    try:
        DATA_DIR.mkdir(exist_ok=True)
        payload = {"history": history, "conversation": conversation}
        HISTORY_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass  # 磁盘写入失败不影响对话本身


def load_conversation() -> tuple[list, list]:
    """载入上次对话，返回 (history, conversation)。无记录或损坏时返回空。"""
    if not HISTORY_FILE.exists():
        return [], []
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        history = data.get("history", [])
        conversation = data.get("conversation", [])
        if isinstance(history, list) and isinstance(conversation, list):
            return history, conversation
    except (OSError, json.JSONDecodeError):
        pass
    return [], []


def clear_conversation():
    """删除保存的对话记录。"""
    try:
        HISTORY_FILE.unlink(missing_ok=True)
    except OSError:
        pass
