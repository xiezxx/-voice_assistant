package com.xiaoyin.remote;

import android.content.Context;
import android.content.Intent;
import android.net.Uri;

/**
 * QQ 音乐的调起。
 *
 * 播放用 qqmusic:// 协议，格式经真机验证过：
 * <pre>
 *   qqmusic://qq.com/media/playSonglist?p={"song":[{"songmid":"..."}],"action":"play"}
 * </pre>
 * 注意 song 必须是**对象**数组（[{"songmid":...}]）；写成 ["type","0",...] 那种数组
 * 只会打开 App 而不播放（同一天真机对比过）。
 *
 * 原生 App 用代码调起别的 App 不需要用户确认，所以能做到"说完话就播、零点击"
 * —— 这正是网页做不到、必须做 App 的原因。
 */
public final class QQMusic {

    public static final String PACKAGE = "com.tencent.qqmusic";

    private QQMusic() {
    }

    /** 拼出播放链接；songmid 是 QQ 音乐的歌曲 ID（服务器搜歌时拿到）。 */
    public static String buildPlayUri(String songmid) {
        String json = "{\"song\":[{\"songmid\":\"" + songmid + "\"}],\"action\":\"play\"}";
        return "qqmusic://qq.com/media/playSonglist?p=" + Uri.encode(json);
    }

    /** 调起 QQ 音乐播放指定歌曲。返回是否成功发出 Intent（失败多半是没装 App）。 */
    public static boolean play(Context ctx, String songmid) {
        if (songmid == null || songmid.isEmpty()) {
            return false;
        }
        Intent intent = new Intent(Intent.ACTION_VIEW, Uri.parse(buildPlayUri(songmid)));
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        try {
            ctx.startActivity(intent);
            return true;
        } catch (Exception e) {
            return false;   // 没装 QQ 音乐 / 协议被禁用
        }
    }
}
