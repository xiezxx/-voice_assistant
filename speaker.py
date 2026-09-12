# -*- coding: utf-8 -*-
"""声纹锁定（speaker verification）：识别并记忆主人的声音，其他人唤醒时忽略。

基于 sherpa-onnx 中文声纹模型（CAM++ zh-en 16k，约 28MB，本地推理）。
- 提取器（SpeakerEmbeddingExtractor）为共享资源，需外部串行化（speaker_lock）
- 每个会话用 create_manager() 建独立 SpeakerEmbeddingManager（会话级录入）
- 相似度：sherpa 的 manager.search() 内部用余弦相似度 + 阈值
"""

from pathlib import Path

from config import Config


class SpeakerVerifier:
    """声纹提取与校验；无状态（录入状态在每会话的 manager 里）。"""

    def __init__(self, model_path: str = None, threshold: float = None):
        import sherpa_onnx

        self.model_path = str(model_path or Config.SPEAKER_MODEL_PATH)
        self.threshold = float(
            threshold if threshold is not None else Config.SPEAKER_THRESHOLD
        )
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=self.model_path, num_threads=2
        )
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)

    def create_manager(self):
        """每会话独立的说话人登记表（线程内使用）。"""
        import sherpa_onnx

        return sherpa_onnx.SpeakerEmbeddingManager(
            dim=self._extractor.dim
        )

    def extract(self, samples) -> list:
        """float32 16k 样本 → 声纹向量（约 20~50ms）。"""
        stream = self._extractor.create_stream()
        stream.accept_waveform(16000, samples)
        stream.input_finished()
        return self._extractor.compute(stream)

    def verify(self, manager, samples) -> bool:
        """提取声纹并在 manager 中检索；命中（相似度达标）返回 True。"""
        embedding = self.extract(samples)
        return bool(manager.search(embedding, self.threshold))


def speaker_available() -> tuple:
    """声纹锁定是否可用：模型文件 + sherpa-onnx 齐备。"""
    if not Config.SPEAKER_LOCK:
        return False, "SPEAKER_LOCK 未开启"
    if not Path(Config.SPEAKER_MODEL_PATH).exists():
        return False, "缺少声纹模型文件（models/speech_campplus_sv_zh_en_16k-common_advanced.onnx）"
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        return False, "未安装 sherpa-onnx"
    return True, "就绪"
