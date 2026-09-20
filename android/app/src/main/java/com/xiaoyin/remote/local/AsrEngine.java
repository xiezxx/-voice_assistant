package com.xiaoyin.remote.local;

import android.content.res.AssetManager;

import com.k2fsa.sherpa.onnx.FeatureConfig;
import com.k2fsa.sherpa.onnx.OfflineModelConfig;
import com.k2fsa.sherpa.onnx.OfflineRecognizer;
import com.k2fsa.sherpa.onnx.OfflineRecognizerConfig;
import com.k2fsa.sherpa.onnx.OfflineStream;
import com.k2fsa.sherpa.onnx.OfflineZipformerCtcModelConfig;

/**
 * 语音识别（端侧）：把一整句话的音频转成文字。
 *
 * <p><b>为什么用"非流式"</b>：整条链路的做法是「听到你说完 → 再整段识别」（电脑端也是这么干的），
 * 不是边听边出字。非流式模型在同样的体积下**明显更准**——实测同一批句子，
 * 这个小模型 94%，比电脑上原来的 Whisper base（86%）还高（见 {@code test_ondevice_asr.py}）。
 *
 * <p>模型约 63MB，和唤醒词一样必须放 assets（AAR 的构造器只认 AssetManager）。
 * 目录里那个 {@code bbpe.model} 不用拷：实测基础解码不需要它。
 *
 * <p>⚠️ 不是线程安全的，只在采集线程里调用。
 */
public class AsrEngine {

    private static final String DIR = "models/asr/";
    private static final int SAMPLE_RATE = 16000;
    private static final int FEATURE_DIM = 80;

    private final OfflineRecognizer recognizer;

    public AsrEngine(AssetManager assets) {
        FeatureConfig feat = new FeatureConfig();
        feat.setSampleRate(SAMPLE_RATE);
        feat.setFeatureDim(FEATURE_DIM);

        OfflineZipformerCtcModelConfig ctc = new OfflineZipformerCtcModelConfig();
        ctc.setModel(DIR + "model.int8.onnx");

        OfflineModelConfig model = new OfflineModelConfig();
        model.setZipformerCtc(ctc);
        model.setTokens(DIR + "tokens.txt");
        model.setNumThreads(2);
        model.setProvider("cpu");
        model.setDebug(false);

        OfflineRecognizerConfig cfg = new OfflineRecognizerConfig();
        cfg.setFeatConfig(feat);
        cfg.setModelConfig(model);
        cfg.setDecodingMethod("greedy_search");

        recognizer = new OfflineRecognizer(assets, cfg);
    }

    /**
     * 识别一整段音频。
     *
     * @param samples    float32、16k 单声道（{@link Vad.UtteranceCollector#take()} 的输出）
     * @return 识别出的文字；没听清返回空串
     */
    public String transcribe(float[] samples) {
        if (samples == null || samples.length == 0) {
            return "";
        }
        // 注意：OfflineStream 只有 acceptWaveform / getOption，
        // **没有** inputFinished()（那是流式 OnlineStream 才有的），也没有 release()
        // —— 它的原生对象靠 finalize 回收。这两条是编译报错后才核实的。
        OfflineStream stream = recognizer.createStream();
        stream.acceptWaveform(samples, SAMPLE_RATE);
        recognizer.decode(stream);
        String text = recognizer.getResult(stream).getText();
        return text == null ? "" : text.trim();
    }

    public void release() {
        recognizer.release();
    }
}
