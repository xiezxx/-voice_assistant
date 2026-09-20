import com.xiaoyin.remote.local.MessageList;
import com.xiaoyin.remote.local.SpeechSegmenter;
import com.xiaoyin.remote.local.Vad;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * 移植比对：拿 gen_cases.py 生成的用例（Python 原件算出的参考结果），
 * 逐组检查 Java 侧实现是否一致。
 *
 * 编译运行（在 android/tools/javatest 下）：
 *   javac -encoding UTF-8 -d out ../../app/src/main/java/com/xiaoyin/remote/local/*.java Compare.java
 *   java -cp out Compare
 */
public class Compare {

    static final String SEP = "\u001f";   // 字段内多值分隔
    static final String US = "\u001e";    // 多条消息分隔
    static final int FRAME = 1600;

    static int pass = 0, fail = 0;
    static final List<String> failures = new ArrayList<>();

    public static void main(String[] args) throws IOException {
        Path dir = Path.of("cases");
        if (!Files.isDirectory(dir)) {
            System.out.println("找不到 cases/ 目录 —— 先跑 python gen_cases.py");
            System.exit(1);
        }
        checkSpeech(dir.resolve("speech.tsv"));
        checkUtterance(dir.resolve("utterance.tsv"));
        checkBarge(dir.resolve("barge.tsv"));
        checkMessages(dir.resolve("messages.tsv"));

        System.out.println();
        System.out.printf("比对结果：%d 组通过，%d 组不一致%n", pass, fail);
        for (String f : failures) {
            System.out.println("  ✗ " + f);
        }
        if (fail == 0) {
            System.out.println("\nJava 移植与 Python 原件行为一致 ✅");
        } else {
            System.exit(1);
        }
    }

    // ── 句子切分 ────────────────────────────────────────────

    static void checkSpeech(Path file) throws IOException {
        int n = 0;
        for (String line : readLines(file)) {
            String[] c = split(line, 3);
            String id = c[0];
            List<String> chunks = splitMulti(c[1]);
            List<String> expect = splitMulti(c[2]);

            SpeechSegmenter seg = new SpeechSegmenter();
            List<String> got = new ArrayList<>();
            for (String ch : chunks) {
                got.addAll(seg.feed(ch));
            }
            String tail = seg.flush();
            if (!tail.isEmpty()) {
                got.add(tail);
            }
            n++;
            if (!got.equals(expect)) {
                record("句子切分 " + id, expect.toString(), got.toString());
            } else {
                pass++;
            }
        }
        System.out.printf("句子切分：%d 组%n", n);
    }

    // ── 采集器 ──────────────────────────────────────────────

    static void checkUtterance(Path file) throws IOException {
        int n = 0;
        for (String line : readLines(file)) {
            String[] c = split(line, 6);
            float thr = Float.parseFloat(c[1]);
            int[] mags = ints(c[2]);
            int expectDone = Integer.parseInt(c[3]);
            int expectFrames = Integer.parseInt(c[4]);
            boolean expectSpeech = "1".equals(c[5]);

            Vad.UtteranceCollector col = new Vad.UtteranceCollector(thr);
            int doneAt = -1;
            for (int k = 0; k < mags.length; k++) {
                short[] frame = frameOf(mags[k]);
                float level = Vad.level(frame, frame.length);
                if (col.feed(frame, frame.length, level)) {
                    doneAt = k;
                    break;
                }
            }
            n++;
            if (doneAt != expectDone || col.frameCount() != expectFrames
                    || col.hasSpeech() != expectSpeech) {
                record("采集器 " + c[0],
                        String.format("done=%d frames=%d speech=%b", expectDone, expectFrames, expectSpeech),
                        String.format("done=%d frames=%d speech=%b", doneAt, col.frameCount(), col.hasSpeech()));
            } else {
                pass++;
            }
        }
        System.out.printf("采集器：%d 组%n", n);
    }

    // ── 打断检测 ────────────────────────────────────────────

    static void checkBarge(Path file) throws IOException {
        int n = 0;
        for (String line : readLines(file)) {
            String[] c = split(line, 5);
            float thr = Float.parseFloat(c[1]);
            int[] mags = ints(c[2]);
            int expectHit = Integer.parseInt(c[3]);

            Vad.BargeDetector det = new Vad.BargeDetector(thr);
            int hit = -1;
            for (int k = 0; k < mags.length; k++) {
                if (det.feed(mags[k] / 32768.0f)) {
                    hit = k;
                    break;
                }
            }
            n++;
            if (hit != expectHit) {
                record("打断检测 " + c[0], "第 " + expectHit + " 块命中", "第 " + hit + " 块命中");
            } else {
                pass++;
            }
        }
        System.out.printf("打断检测：%d 组%n", n);
    }

    // ── 工具调用组配对 ──────────────────────────────────────

    static void checkMessages(Path file) throws IOException {
        int n = 0;
        for (String line : readLines(file)) {
            String[] c = split(line, 3);
            List<MessageList.Msg> conv = new ArrayList<>();
            for (String enc : splitRaw(c[1], US)) {
                String[] f = splitOn(enc, "|", 4);   // 消息内部用 | 分隔（注意不是制表符）
                MessageList.Msg m = new MessageList.Msg();
                m.role = f[0];
                m.content = f[1].isEmpty() ? null : f[1];
                if (!f[2].isEmpty()) {
                    m.toolCalls = new ArrayList<>();
                    for (String one : f[2].split(",", -1)) {
                        String[] tc = one.split(":", 3);
                        m.toolCalls.add(new MessageList.ToolCall(tc[0], tc.length > 1 ? tc[1] : "", tc.length > 2 ? tc[2] : ""));
                    }
                }
                m.toolCallId = f[3].isEmpty() ? null : f[3];
                conv.add(m);
            }
            String got = MessageList.describe(MessageList.build(conv));
            n++;
            if (!got.equals(c[2])) {
                record("工具组配对 " + c[0], c[2], got);
            } else {
                pass++;
            }
        }
        System.out.printf("工具组配对：%d 组%n", n);
    }

    // ── 工具 ────────────────────────────────────────────────

    static short[] frameOf(int mag) {
        short v = (short) Math.min(mag, 32767);
        short[] f = new short[FRAME];
        Arrays.fill(f, v);
        return f;
    }

    static List<String> readLines(Path p) throws IOException {
        List<String> out = new ArrayList<>();
        for (String l : Files.readAllLines(p, StandardCharsets.UTF_8)) {
            if (!l.isEmpty()) {
                out.add(l);
            }
        }
        return out;
    }

    static String[] split(String line, int n) {
        String[] parts = line.split("\t", -1);
        if (parts.length < n) {
            String[] padded = new String[n];
            Arrays.fill(padded, "");
            System.arraycopy(parts, 0, padded, 0, parts.length);
            return padded;
        }
        return parts;
    }

    static List<String> splitMulti(String s) {
        if (s.isEmpty()) {
            return new ArrayList<>();
        }
        List<String> out = new ArrayList<>();
        for (String one : s.split(SEP, -1)) {
            out.add(unescape(one));
        }
        return out;
    }

    /** 反转义：TSV 里换行/制表符会被转成字面量 \\n \\t（见 gen_cases.py 的 esc）。 */
    static String unescape(String s) {
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '\\' && i + 1 < s.length()) {
                char n = s.charAt(++i);
                if (n == 'n') {
                    b.append('\n');
                } else if (n == 't') {
                    b.append('\t');
                } else if (n == '\\') {
                    b.append('\\');
                } else {
                    b.append(n);
                }
            } else {
                b.append(c);
            }
        }
        return b.toString();
    }

    static String[] splitOn(String s, String sep, int n) {
        // ⚠️ 必须 Pattern.quote：Java 的 split 收的是**正则**，而 "|" 在正则里是"或"，
        // 直接 split("|") 会把每个字符都切开（比分对时真踩过，症状是消息全被过滤光）
        String[] parts = s.split(java.util.regex.Pattern.quote(sep), -1);
        if (parts.length < n) {
            String[] padded = new String[n];
            Arrays.fill(padded, "");
            System.arraycopy(parts, 0, padded, 0, parts.length);
            return padded;
        }
        return parts;
    }

    static List<String> splitRaw(String s, String sep) {
        if (s.isEmpty()) {
            return new ArrayList<>();
        }
        return new ArrayList<>(Arrays.asList(s.split(sep, -1)));
    }

    static int[] ints(String s) {
        if (s.isEmpty()) {
            return new int[0];
        }
        String[] parts = s.split(",");
        int[] out = new int[parts.length];
        for (int i = 0; i < parts.length; i++) {
            out[i] = Integer.parseInt(parts[i].trim());
        }
        return out;
    }

    static void record(String what, String expect, String got) {
        fail++;
        if (failures.size() < 12) {
            failures.add(what + "\n      期望 [" + show(expect) + "]\n      实际 [" + show(got) + "]");
        }
    }

    /** 把控制字符显形：不然 \r 会把光标拉回行首，看上去像"内容是空的"（被这个坑过）。 */
    static String show(String s) {
        if (s == null) {
            return "null";
        }
        StringBuilder b = new StringBuilder();
        for (char c : s.toCharArray()) {
            if (c < 32 || c == 127) {
                b.append(String.format("\\u%04x", (int) c));
            } else {
                b.append(c);
            }
        }
        return b.toString();
    }
}
