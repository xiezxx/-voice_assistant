package com.xiaoyin.remote;

import android.content.Context;
import android.media.session.MediaController;
import android.media.session.MediaSessionManager;
import android.service.notification.NotificationListenerService;

import java.util.List;

/**
 * 媒体控制（暂停/继续/上一首/下一首）。
 *
 * QQ 音乐没有公开的控制协议，所以不用界面自动化那套，改用 Android 提供的标准接口：
 * 拿到「通知使用权」后，MediaSessionManager 就能看到 QQ 音乐的媒体会话，直接指挥它。
 *
 * 代价是用户要在系统设置里授予一次「通知使用权」（见 MainActivity 的引导按钮）——
 * 这是系统对媒体控制的硬要求，绕不开。
 */
public class MediaListener extends NotificationListenerService {

    private static MediaListener instance;

    @Override
    public void onListenerConnected() {
        instance = this;
    }

    @Override
    public void onListenerDisconnected() {
        if (instance == this) {
            instance = null;
        }
    }

    /** 用户是否已授予「通知使用权」。 */
    public static boolean available() {
        return instance != null;
    }

    /** 一次控制的结果。**必须带原因**：只回一个 false 的话，
     *  电脑那边只知道"失败了"，用户只会听到"没控制成功"，谁也不知道该去修什么。 */
    public static final class Result {
        public final boolean ok;
        public final String detail;

        Result(boolean ok, String detail) {
            this.ok = ok;
            this.detail = detail;
        }

        public static Result ok() {
            return new Result(true, "");
        }

        public static Result fail(String why) {
            return new Result(false, why);
        }
    }

    /**
     * 对正在播放的 QQ 音乐执行一次控制。
     *
     * @param action pause / resume / next / prev
     */
    public static Result control(String action) {
        MediaListener self = instance;
        if (self == null) {
            return Result.fail("还没授予「通知使用权」");
        }
        try {
            MediaSessionManager manager =
                    (MediaSessionManager) self.getSystemService(Context.MEDIA_SESSION_SERVICE);
            if (manager == null) {
                return Result.fail("系统没有媒体会话服务");
            }
            List<MediaController> sessions = manager.getActiveSessions(null);
            boolean sawQqMusicPackage = false;
            for (MediaController controller : sessions) {
                if (!QQMusic.PACKAGE.equals(controller.getPackageName())) {
                    continue;
                }
                // 有会话、但可能没在放（暂停状态）——这种情况也要能控制，所以不看 playbackState
                sawQqMusicPackage = true;
                MediaController.TransportControls t = controller.getTransportControls();
                switch (action) {
                    case "pause": t.pause(); return Result.ok();
                    case "resume": t.play(); return Result.ok();
                    case "next": t.skipToNext(); return Result.ok();
                    case "prev": t.skipToPrevious(); return Result.ok();
                    default: return Result.fail("不认识的操作：" + action);
                }
            }
            if (sawQqMusicPackage) {
                return Result.fail("QQ 音乐的会话没在播放");
            }
            return Result.fail("找不到 QQ 音乐的播放会话（手机上 QQ 音乐开着吗？）");
        } catch (Exception e) {
            return Result.fail(e.getClass().getSimpleName() + "：" + e.getMessage());
        }
    }
}
