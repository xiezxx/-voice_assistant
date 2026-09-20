package com.xiaoyin.remote.local;

import android.content.Context;

import com.xiaoyin.remote.MediaListener;
import com.xiaoyin.remote.QQMusic;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.IOException;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;

/**
 * 手机独立模式下能用的工具。**只放手机上真能做的** —— 做不到的（天气/新闻/快递/农历）
 * 从 schema 里直接删掉，不留给模型一个永远报错的工具去反复调用。
 *
 * <p>和电脑端的关系：{@code get_time}/{@code calculate}/{@code play_music}/{@code control_music}
 * 这几个是 {@code tools.py} 同名工具的移植（那 15 个里的一部分），行为对齐；
 * 其余（天气/新闻/快递/空气质量/农历/汇率）**这一版不做**，以后要加就照着 {@link #execute} 加分支。
 */
public class LocalTools {

    private final Context context;
    private final OkHttpClient http = new OkHttpClient.Builder()
            .connectTimeout(6, TimeUnit.SECONDS)
            .readTimeout(8, TimeUnit.SECONDS)
            .build();

    /** 电脑端用的同一个免 Key 搜索接口（实测国内直连可用） */
    private static final String SEARCH_URL = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp";

    public LocalTools(Context context) {
        this.context = context.getApplicationContext();
    }

    /** 工具清单（OpenAI function schema），直接塞进请求的 tools 字段。 */
    public static JSONArray schemas() {
        JSONArray arr = new JSONArray();
        arr.put(fn("get_time", "查询当前日期、时间、星期。用户问「今天几号」「现在几点」「星期几」时调用。"));
        arr.put(fn("calculate",
                "做数学计算。用户说「算一下」「几乘几」「等于多少」时调用。只支持数字和 + - * / // % ** 括号。"));
        arr.put(fnWith("play_music",
                "用手机上的 QQ 音乐播放指定歌曲。用户说「放首歌」「放首晴天」「我想听周杰伦的稻香」时调用。"
                        + "只传歌名，不要带书名号。",
                "song", "歌曲名，可以带歌手，例如：晴天、晴天 周杰伦"));
        arr.put(fnEnum("control_music",
                "控制手机 QQ 音乐的播放状态。用户说「暂停」「继续」「下一首」「切歌」「上一首」时调用。",
                "action", new String[]{"pause", "resume", "next", "prev"},
                "pause=暂停，resume=继续，next=下一首，prev=上一首"));
        return arr;
    }

    /** 工具名 → 界面提示（和电脑端的 TOOL_DISPLAY 一个用途） */
    public static String display(String name) {
        switch (name) {
            case "get_time": return "查询时间";
            case "calculate": return "计算";
            case "play_music": return "用QQ音乐放歌";
            case "control_music": return "控制播放";
            default: return name;
        }
    }

    /** 执行一个工具，返回给模型看的结果文本（**不抛异常**，失败也返回一句人话）。 */
    public String execute(String name, JSONObject args) {
        try {
            switch (name == null ? "" : name) {
                case "get_time":
                    return nowText();
                case "calculate":
                    return calculate(args.optString("expression", args.optString("expr", "")));
                case "play_music":
                    return playMusic(args.optString("song", ""));
                case "control_music":
                    return controlMusic(args.optString("action", ""));
                default:
                    return "未知工具: " + name;
            }
        } catch (Exception e) {
            return "工具执行失败: " + e;
        }
    }

    // ── 具体实现 ────────────────────────────────────────────

    private static String nowText() {
        Date now = new Date();
        String date = new SimpleDateFormat("yyyy年M月d日", Locale.CHINA).format(now);
        String time = new SimpleDateFormat("HH:mm", Locale.CHINA).format(now);
        String[] week = {"星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六"};
        java.util.Calendar c = java.util.Calendar.getInstance();
        return date + " " + week[c.get(java.util.Calendar.DAY_OF_WEEK) - 1] + "，" + time;
    }

    /** 点歌：搜 → 用 qqmusic:// 调起手机上的 QQ 音乐 */
    private String playMusic(String song) {
        String kw = song == null ? "" : song.trim();
        if (kw.isEmpty()) {
            return "想听什么歌？说个歌名，比如「放首晴天」";
        }
        JSONObject hit = search(kw);
        if (hit == null) {
            return "没搜到《" + kw + "》，换个歌名试试？";
        }
        String name = hit.optString("name");
        String singer = hit.optString("singer");
        boolean ok = QQMusic.play(context, hit.optString("songmid"));
        if (!ok) {
            return "没能调起 QQ 音乐（手机上装了吗？）";
        }
        return "正在播放《" + name + "》" + (singer.isEmpty() ? "" : " - " + singer);
    }

    private String controlMusic(String action) {
        MediaListener.Result r = MediaListener.control(action);
        if (!r.ok) {
            return "没控制成功：" + r.detail;
        }
        switch (action) {
            case "pause": return "已暂停手机上的 QQ 音乐";
            case "resume": return "手机继续播放";
            case "next": return "手机已切到下一首";
            case "prev": return "手机已切回上一首";
            default: return "已执行 " + action;
        }
    }

    /** 按歌名搜索，返回 {songmid, name, singer}；搜不到返回 null。 */
    private JSONObject search(String keyword) {
        try {
            String url = SEARCH_URL + "?p=1&n=3&format=json&w="
                    + java.net.URLEncoder.encode(keyword, "UTF-8");
            Request req = new Request.Builder().url(url)
                    .header("User-Agent", "voice-assistant/1.0")
                    .header("Referer", "https://y.qq.com/")
                    .build();
            try (Response resp = http.newCall(req).execute()) {
                if (!resp.isSuccessful() || resp.body() == null) {
                    return null;
                }
                JSONObject data = new JSONObject(resp.body().string());
                JSONArray list = data.optJSONObject("data") == null ? null
                        : data.getJSONObject("data").optJSONObject("song") == null ? null
                        : data.getJSONObject("data").getJSONObject("song").optJSONArray("list");
                if (list == null || list.length() == 0) {
                    return null;
                }
                JSONObject top = list.getJSONObject(0);
                JSONObject out = new JSONObject();
                out.put("songmid", top.optString("songmid"));
                out.put("name", top.optString("songname"));
                JSONArray singers = top.optJSONArray("singer");
                out.put("singer", singers != null && singers.length() > 0
                        ? singers.getJSONObject(0).optString("name") : "");
                return out;
            }
        } catch (Exception e) {
            return null;
        }
    }

    // ── 计算器（纯逻辑，和 tools.py 的 _eval_node 行为对齐）──
    //
    // Python 那边靠 ast 白名单，Java 没有对应的东西，所以老老实实写个递归下降解析。
    // 运算符和优先级照抄：+ - | * / // % | 一元正负 | ** （右结合）

    static String calculate(String expression) {
        String expr = expression == null ? "" : expression.trim()
                .replace("×", "*").replace("÷", "/");
        if (expr.isEmpty()) {
            return "计算失败: 表达式为空";
        }
        try {
            Parser p = new Parser(expr);
            double v = p.parseExpr();
            p.expectEnd();
            return format(v);
        } catch (ArithmeticException e) {
            return "计算失败: " + e.getMessage();
        } catch (Exception e) {
            return "计算失败: " + e.getMessage();
        }
    }

    /** 整数就别显示小数点（和 Python 的 f"{x:g}" 观感一致） */
    private static String format(double v) {
        if (Double.isNaN(v) || Double.isInfinite(v)) {
            return String.valueOf(v);
        }
        if (Math.abs(v - Math.rint(v)) < 1e-9 && Math.abs(v) < 1e15) {
            return String.valueOf((long) Math.rint(v));
        }
        String s = String.format(Locale.US, "%g", v);
        return s;
    }

    private static final class Parser {
        private final String s;
        private int i = 0;

        Parser(String s) {
            this.s = s;
        }

        double parseExpr() {
            double v = parseTerm();
            while (true) {
                skipSpace();
                if (eat('+')) {
                    v += parseTerm();
                } else if (eat('-')) {
                    v -= parseTerm();
                } else {
                    return v;
                }
            }
        }

        double parseTerm() {
            double v = parseUnary();
            while (true) {
                skipSpace();
                if (eat('*')) {
                    if (eat('*')) {           // ** 交给 parsePower 处理，这里退回去
                        i -= 2;
                        return v;
                    }
                    v *= parseUnary();
                } else if (eat('/')) {
                    if (eat('/')) {
                        double d = parseUnary();
                        if (d == 0) {
                            throw new ArithmeticException("不能除以零");
                        }
                        v = Math.floor(v / d);
                    } else {
                        double d = parseUnary();
                        if (d == 0) {
                            throw new ArithmeticException("不能除以零");
                        }
                        v /= d;
                    }
                } else if (eat('%')) {
                    double d = parseUnary();
                    if (d == 0) {
                        throw new ArithmeticException("不能除以零");
                    }
                    v = v - Math.floor(v / d) * d;    // Python 的取模：结果符号跟着除数
                } else {
                    return v;
                }
            }
        }

        double parseUnary() {
            skipSpace();
            if (eat('-')) {
                return -parseUnary();
            }
            if (eat('+')) {
                return parseUnary();
            }
            return parsePower();
        }

        double parsePower() {
            double base = parsePrimary();
            skipSpace();
            if (peek() == '*' && i + 1 < s.length() && s.charAt(i + 1) == '*') {
                i += 2;
                return Math.pow(base, parseUnary());     // 右结合
            }
            return base;
        }

        double parsePrimary() {
            skipSpace();
            if (eat('(')) {
                double v = parseExpr();
                skipSpace();
                if (!eat(')')) {
                    throw new IllegalArgumentException("括号不匹配");
                }
                return v;
            }
            int start = i;
            while (i < s.length() && (Character.isDigit(s.charAt(i)) || s.charAt(i) == '.')) {
                i++;
            }
            if (start == i) {
                throw new IllegalArgumentException("这里应该是数字：" + s.substring(Math.min(start, s.length())));
            }
            return Double.parseDouble(s.substring(start, i));
        }

        void expectEnd() {
            skipSpace();
            if (i < s.length()) {
                throw new IllegalArgumentException("有多余内容：" + s.substring(i));
            }
        }

        private char peek() {
            return i < s.length() ? s.charAt(i) : '\0';
        }

        private void skipSpace() {
            while (i < s.length() && Character.isWhitespace(s.charAt(i))) {
                i++;
            }
        }

        private boolean eat(char c) {
            skipSpace();
            if (i < s.length() && s.charAt(i) == c) {
                i++;
                return true;
            }
            return false;
        }
    }

    // ── schema 构造小工具 ───────────────────────────────────

    private static JSONObject fn(String name, String desc) {
        try {
            JSONObject f = new JSONObject();
            f.put("name", name);
            f.put("description", desc);
            f.put("parameters", new JSONObject()
                    .put("type", "object")
                    .put("properties", new JSONObject())
                    .put("required", new JSONArray()));
            return new JSONObject().put("type", "function").put("function", f);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private static JSONObject fnWith(String name, String desc, String p, String pDesc) {
        try {
            JSONObject props = new JSONObject();
            props.put(p, new JSONObject().put("type", "string").put("description", pDesc));
            JSONObject f = new JSONObject();
            f.put("name", name);
            f.put("description", desc);
            f.put("parameters", new JSONObject()
                    .put("type", "object").put("properties", props)
                    .put("required", new JSONArray().put(p)));
            return new JSONObject().put("type", "function").put("function", f);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private static JSONObject fnEnum(String name, String desc, String p, String[] values, String pDesc) {
        try {
            JSONArray enums = new JSONArray();
            for (String v : values) {
                enums.put(v);
            }
            JSONObject props = new JSONObject();
            props.put(p, new JSONObject().put("type", "string")
                    .put("enum", enums).put("description", pDesc));
            JSONObject f = new JSONObject();
            f.put("name", name);
            f.put("description", desc);
            f.put("parameters", new JSONObject()
                    .put("type", "object").put("properties", props)
                    .put("required", new JSONArray().put(p)));
            return new JSONObject().put("type", "function").put("function", f);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }
}
