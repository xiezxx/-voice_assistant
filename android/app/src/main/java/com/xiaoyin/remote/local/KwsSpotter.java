package com.xiaoyin.remote.local;

import android.content.res.AssetManager;

import com.k2fsa.sherpa.onnx.FeatureConfig;
import com.xiaoyin.remote.MicCapture;
import com.k2fsa.sherpa.onnx.KeywordSpotter;
import com.k2fsa.sherpa.onnx.KeywordSpotterConfig;
import com.k2fsa.sherpa.onnx.OnlineModelConfig;
import com.k2fsa.sherpa.onnx.OnlineStream;
import com.k2fsa.sherpa.onnx.OnlineTransducerModelConfig;

/**
 * 唤醒词检测（端侧）：说「小音」就醒。
 *
 * <p>模型和电脑上跑的**是同一套**（sherpa-onnx 中文 KWS，wenetspeech int8，约 4.8MB），
 * 关键词文件也原样搬过来 —— 电脑上调好的灵敏度，端侧行为一致。
 *
 * <p>⚠️ 两条来自实测的约束（见 android/README.md 的「依赖与事实清单」）：
 * <ol>
 *   <li>{@link KeywordSpotter} **只有 AssetManager 那个构造器**，没有文件路径版，
 *       所以模型必须放在 {@code assets/}，配置里的路径也是相对 assets 的；</li>
 *   <li>这个类**不是线程安全的**，只允许从采集线程调用（和电脑端靠锁串行是同一个道理）。</li>
 * </ol>
 */
public class KwsSpotter {

    /** 模型在 assets 里的目录 */
    private static final String DIR = "models/kws/";
    private static final int SAMPLE_RATE = 16000;
    private static final int FEATURE_DIM = 80;

    private final KeywordSpotter spotter;
    private OnlineStream stream;
    private final float[] floatBuf = new float[MicCapture.FRAME_SAMPLES];

    /**
     * @param threshold 关键词阈值，和电脑端 {@code KWS_KEYWORDS_THRESHOLD} 同义：
     *                  调高更难唤醒（抗误触发），调低更容易唤醒。默认 0.25
     */
    public KwsSpotter(AssetManager assets, float threshold) {
        FeatureConfig feat = new FeatureConfig();
        feat.setSampleRate(SAMPLE_RATE);
        feat.setFeatureDim(FEATURE_DIM);

        OnlineTransducerModelConfig transducer = new OnlineTransducerModelConfig();
        transducer.setEncoder(DIR + "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx");
        transducer.setDecoder(DIR + "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx");
        transducer.setJoiner(DIR + "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx");

        OnlineModelConfig model = new OnlineModelConfig();
        model.setTransducer(transducer);
        model.setTokens(DIR + "tokens.txt");
        model.setNumThreads(2);        // 手机上面别开太多，采集线程还要留 CPU
        model.setProvider("cpu");
        model.setDebug(false);

        KeywordSpotterConfig cfg = new KeywordSpotterConfig();
        cfg.setFeatConfig(feat);
        cfg.setModelConfig(model);
        cfg.setMaxActivePaths(4);
        cfg.setKeywordsFile(DIR + "wake_keywords.txt");
        cfg.setKeywordsScore(1.0f);
        cfg.setKeywordsThreshold(threshold);

        spotter = new KeywordSpotter(assets, cfg);
        stream = spotter.createStream("");     // 空串 = 用 keywordsFile 里那几行
    }

    /**
     * 喂一帧（1600 样本 / 0.1 秒），返回命中的关键词；没命中返回空串。
     */
    public String feed(short[] frame) {
        if (stream == null || frame == null) {
            return "";
        }
        int n = Math.min(frame.length, floatBuf.length);
        for (int i = 0; i < n; i++) {
            floatBuf[i] = frame[i] / 32768.0f;
        }
        stream.acceptWaveform(floatBuf, SAMPLE_RATE);
        while (spotter.isReady(stream)) {
            spotter.decode(stream);
        }
        String kw = spotter.getResult(stream).getKeyword();
        return kw == null ? "" : kw;
    }

    /**
     * 重置检测流：清掉内部状态。
     *
     * <p>播报结束后**必须**调一次 —— 小音念回复时完全可能说到「小音」两个字，
     * 不重置的话它会把自己念的内容当成唤醒词，然后对着自己说话。
     */
    public void reset() {
        if (stream != null) {
            spotter.reset(stream);
        }
    }

    public void release() {
        if (stream != null) {
            stream.release();
            stream = null;
        }
        spotter.release();
    }
}
