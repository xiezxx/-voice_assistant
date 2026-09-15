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

    def embed(self, samples) -> list | None:
        """先掐掉头尾的静音/噪声再提声纹；有效语音太短返回 None（调用方应放行）。

        为什么必须掐：唤醒时手上是「唤醒词前后 4 秒」的整段音频，里面真正在说话的
        可能只有一秒，其余是静音和环境噪声 —— 直接拿去提声纹，等于拿"房间的声纹"。
        """
        speech = self.speech_only(samples)
        if speech is None:
            return None
        return self.extract(speech)

    @staticmethod
    def speech_only(samples, min_sec: float = 0.4, frame: int = 1600):
        """按帧能量掐掉头尾的静音，返回说话那一段；有效语音不足 min_sec 秒返回 None。

        门限取「本底噪声的 4 倍」，本底噪声用 20 分位数估计 —— 说话通常比环境噪声
        高一个数量级，所以这样掐得干净又不会把轻声的字尾削掉（试过"峰值的 15%"，
        太狠，把说话的后半句都切了）。
        """
        import numpy as np

        x = np.asarray(samples, dtype=np.float32).ravel()
        if x.size < frame:
            return None
        n = x.size // frame
        frames = x[: n * frame].reshape(n, frame)
        rms = np.sqrt((frames ** 2).mean(axis=1))
        peak = float(rms.max())
        if peak <= 0:
            return None
        noise = float(np.percentile(rms, 20))
        gate = max(noise * 4.0, peak * 0.02)   # 兜底：别低于峰值的 2%，免得整段静音也算数
        idx = np.nonzero(rms >= gate)[0]
        if idx.size == 0:
            return None
        lo = max(0, int(idx[0]) - 1)          # 前后各留一帧，别削掉字头字尾
        hi = min(n, int(idx[-1]) + 2)
        cut = x[lo * frame: hi * frame]
        if cut.size < int(16000 * min_sec):
            return None
        return np.ascontiguousarray(cut, dtype=np.float32)

    @staticmethod
    def similarity(a, b) -> float:
        """两个声纹向量的余弦相似度，落在 [-1, 1]。

        ⚠️ 必须自己除以模长：sherpa 的 extract() 给的是**未归一化**的向量
        （实测自比点积约 95），直接点积会让阈值形同虚设 —— 谁都能过。
        """
        import numpy as np

        va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
        na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return float(np.dot(va, vb) / (na * nb))

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
