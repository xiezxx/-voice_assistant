package com.xiaoyin.remote.local;

import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 把大模型的流式文本切成一句一句，喂给语音合成。
 *
 * <p>逐行照搬 {@code speech_utils.py} 的 {@code sentence_stream}（那个是电脑端的原件）：
 * <ol>
 *   <li>遇到句号/问号/叹号/分号/换行<b>立即切</b>，标点跟着前一句；</li>
 *   <li>没等到句末符时，缓冲区**达到 18 字**就允许在第一个逗号处提前切
 *       —— 这是降首响延迟的关键：不然模型要说完一整段长句才开口；</li>
 *   <li>流结束后残余单独产出。</li>
 * </ol>
 *
 * <p>纯逻辑、零 Android 依赖，所以能在电脑上用 javac 单独编译、和 Python 原件逐组比对
 * （见 {@code android/tools/javatest/}）。
 */
public class SpeechSegmenter {

    /** 中文句子结束符：句号/问号/叹号/分号/换行 */
    private static final Pattern SENT_END = Pattern.compile("[。！？!?；;\n]");
    /** 长句细分阈值：缓冲区超过这个长度就允许在逗号处提前切开 */
    private static final int MIN_COMMA_SPLIT = 18;
    private static final Pattern COMMA = Pattern.compile("[，,]");

    private final StringBuilder buffer = new StringBuilder();

    /**
     * 喂一段流式文本，返回这次能切出来的完整句子（可能 0 句、也可能好几句）。
     */
    public List<String> feed(String chunk) {
        List<String> out = new ArrayList<>();
        if (chunk == null || chunk.isEmpty()) {
            return out;
        }
        buffer.append(chunk);
        while (true) {
            int idx = -1;
            Matcher m = SENT_END.matcher(buffer);
            if (m.find()) {
                idx = m.end();
            } else {
                // 长句在逗号处提前切：从阈值位置起找第一个逗号
                // （Python 是 _COMMA.search(buffer, MIN_COMMA_SPLIT - 1)，pos 是从 0 数的下标）
                //
                // ⚠️ 起点必须夹住：Python 的 search(s, pos) 在 pos 超出长度时**返回 None**，
                // 而 Java 的 Matcher.find(pos) 会直接抛 IndexOutOfBoundsException ——
                // 缓冲区还没到 18 字时就走这条路，不夹就会崩（逐组比对时真崩出来过）。
                Matcher c = COMMA.matcher(buffer);
                if (c.find(Math.min(MIN_COMMA_SPLIT - 1, buffer.length()))) {
                    idx = c.end();
                }
            }
            if (idx < 0) {
                break;
            }
            String sentence = buffer.substring(0, idx).trim();
            buffer.delete(0, idx);
            if (!sentence.isEmpty()) {
                out.add(sentence);
            }
        }
        return out;
    }

    /** 流结束了：把残余吐出来（没有就算了）。 */
    public String flush() {
        String tail = buffer.toString().trim();
        buffer.setLength(0);
        return tail;
    }
}
