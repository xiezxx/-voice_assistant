/* Web 全双工语音会话 —— 浏览器端麦克风持续推流 + 流式播放服务器下发的 mp3。
 *
 * 协议 /ws/assistant（详见 README「接入协议」）：
 *  - 上行：二进制 int16 16k PCM 帧（持续推流，含 AI 播报期间——服务器检测说话打断）；
 *          控制 {"type":"stop"}。
 *  - 下行：JSON 事件 wake/transcript/sentence/status/barge_in/turn_end/error；
 *          audio 事件后紧跟一个二进制帧 = 完整 mp3。
 * 客户端无状态机：全部由服务器按会话状态路由；UI 聊天框由 Gradio Timer 机制同步。
 *
 * 依赖 DOM：#wake-btn（开关按钮）、#wake-status（状态文字）。
 * 注：ScriptProcessorNode 已标记废弃但全浏览器支持、代码量小；未来可换 AudioWorklet。
 */
(function () {
  'use strict';

  var CHUNK = 1600;  // 每帧样本数（0.1s @ 16k）——服务器按这个时长算 VAD/打断

  var state = 'IDLE';  // IDLE | LISTENING（仅影响按钮/状态文案）
  var ws = null;
  var wsRetry = 0;
  var audioCtx = null;
  var micStream = null;
  var processor = null;
  var ring = [];       // 浮点采样缓冲，凑满一帧发一次
  var playQueue = [];  // 待播放的 Audio 元素队列
  var currentAudio = null;
  var nativeChunk = CHUNK;   // 按实际采样率折算的"凑够多少原生样本发一帧"
  var autoplayArmed = false; // 自动播放被拒后是否已挂上"用户一碰屏幕就重试"
  var rateInfo = '';         // 实际采样率（显示在聆听状态里，便于判断手机是否走了重采样）

  // 聆听中的统一文案：带上真实采样率，手机上一眼能看出有没有走重采样
  function listeningMsg(prefix) {
    return (prefix || '🟢 免提聆听中')
      + (rateInfo ? '（' + rateInfo + '）' : '') + ' — 说「小音」唤醒';
  }

  function setStatus(text) {
    // 必须每次现查：脚本是 defer 执行的，Gradio 水合时会把服务端渲染的节点换掉，
    // 加载时抓到的引用会变成脱离文档的旧节点，导致所有状态文字都写了个寂寞
    var el = document.getElementById('wake-status') || statusEl;
    if (el) el.textContent = text;
  }
  var statusEl = document.getElementById('wake-status');  // 兜底引用（水合前也保留）

  function beep(freqs, dur) {
    try {
      var t0 = audioCtx.currentTime;
      freqs.forEach(function (f, i) {
        var osc = audioCtx.createOscillator();
        var gain = audioCtx.createGain();
        osc.frequency.value = f;
        osc.type = 'sine';
        gain.gain.setValueAtTime(0.25, t0);
        gain.gain.exponentialRampToValueAtTime(0.001, t0 + dur);
        osc.connect(gain).connect(audioCtx.destination);
        osc.start(t0 + i * 0.05);
        osc.stop(t0 + dur + i * 0.05);
      });
    } catch (e) { /* 提示音失败不影响主流程 */ }
  }

  function connectWS() {
    if (ws && ws.readyState <= 1) return;   // 已在连接中/已连上，别重复开
    var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    ws = new WebSocket(proto + location.host + '/ws/assistant');
    ws.binaryType = 'arraybuffer';
    ws.onopen = function () {
      wsRetry = 0;
      if (state === 'LISTENING') setStatus(listeningMsg());
    };
    ws.onmessage = function (ev) {
      if (typeof ev.data === 'string') {
        var m;
        try { m = JSON.parse(ev.data); } catch (e) { return; }
        handleEvent(m);
      } else {
        enqueueMp3(ev.data);  // audio 事件后的二进制帧 = 完整 mp3
      }
    };
    ws.onclose = function () {
      ws = null;
      if (state !== 'IDLE') {
        wsRetry++;
        setStatus('🔌 连接断开，重连中…（第 ' + wsRetry + ' 次）');
        setTimeout(connectWS, 2000);
      }
    };
  }

  function handleEvent(m) {
    switch (m.type) {
      case 'ready':
        setStatus(listeningMsg());
        break;
      case 'wake':
        beep([1320, 1760], 0.18);
        setStatus('🔔 唤醒成功 — 请说话，说完停顿约 1 秒');
        break;
      case 'transcript':
        if (m.text) setStatus('🤔 识别中：「' + m.text + '」');
        break;
      case 'sentence':
        setStatus('🤖 ' + m.text);
        break;
      case 'status':
        setStatus(m.text || '');
        break;
      case 'barge_in':
        stopPlayback();
        setStatus('⏹ 已打断 — 请继续说');
        break;
      case 'speaker_reject':
        setStatus('🔇 声音不是主人，已忽略');
        break;
      case 'turn_end':
        setStatus(m.status || '✅ 完成 — 说「小音」继续');
        break;
      case 'error':
        setStatus('⚠️ ' + (m.error || '出错了'));
        break;
    }
  }

  function stopPlayback() {
    playQueue.forEach(function (a) {
      try { a.pause(); URL.revokeObjectURL(a.src); a.remove(); } catch (e) {}
    });
    playQueue = [];
    if (currentAudio) {
      try {
        currentAudio.pause();
        currentAudio.onended = null;
        URL.revokeObjectURL(currentAudio.src);
        currentAudio.remove();
      } catch (e) {}
      currentAudio = null;
    }
  }

  function enqueueMp3(buf) {
    var a = new Audio(URL.createObjectURL(new Blob([buf], { type: 'audio/mp3' })));
    a.playsInline = true;          // iOS：别进全屏播放器
    document.body.appendChild(a);  // 让「停止播报」按钮的 querySelectorAll('audio') 能暂停它
    playQueue.push(a);
    if (!currentAudio) playNext();
  }

  // 移动端首句常被自动播放策略拦下。原来的实现只提示"点一下页面"却没有监听，
  // 用户点了也没用、队列还会卡死（后续句子永远播不出来）。这里挂一次性手势监听重试。
  function armAutoplayRetry() {
    if (autoplayArmed) return;
    autoplayArmed = true;
    var retry = function () {
      autoplayArmed = false;
      document.removeEventListener('pointerdown', retry, true);
      document.removeEventListener('touchend', retry, true);
      if (state !== 'IDLE') playNext();
    };
    document.addEventListener('pointerdown', retry, true);
    document.addEventListener('touchend', retry, true);
  }

  function playNext() {
    if (!playQueue.length) { currentAudio = null; return; }
    currentAudio = playQueue.shift();
    currentAudio.onended = function () {
      try { URL.revokeObjectURL(currentAudio.src); currentAudio.remove(); } catch (e) {}
      playNext();
    };
    var p = currentAudio.play();
    if (p && p.catch) {
      p.catch(function () {
        setStatus('🔊 浏览器阻止了自动播放 —— 点一下屏幕任意处继续');
        playQueue.unshift(currentAudio);   // 放回队首，等用户手势后重播这句
        currentAudio = null;
        armAutoplayRetry();
      });
    }
  }

  function toInt16(f32) {
    var i16 = new Int16Array(f32.length);
    for (var i = 0; i < f32.length; i++) {
      var s = Math.max(-1, Math.min(1, f32[i]));
      var v = Math.round(s * 32768);
      i16[i] = Math.max(-32768, Math.min(32767, v));
    }
    return i16;
  }

  // 重采样到 16k。移动端（尤其 iOS Safari）会忽略 AudioContext 的 sampleRate 建议，
  // 给硬件速率（44.1k/48k）——不重采样的话每帧只有 33ms 音频，服务器却按 0.1s 计时，
  // 唤醒词和识别都会失效（听感上像是快放 3 倍）。
  //
  // 降采样必须带抗混叠低通：直接抽点会让 8kHz 以上的高频"折返"成语音频段里的噪声，
  // 实测会明显拉低识别率。这里用窗口平均（盒式滤波），便宜且够用。
  function resample(f32, outLen) {
    if (f32.length === outLen) return f32;
    var out = new Float32Array(outLen);
    var step = f32.length / outLen;
    for (var i = 0; i < outLen; i++) {
      var start = Math.floor(i * step);
      var end = Math.min(Math.ceil((i + 1) * step), f32.length);
      if (end <= start) { end = Math.min(start + 1, f32.length); }
      var sum = 0;
      for (var j = start; j < end; j++) { sum += f32[j]; }
      out[i] = sum / (end - start);
    }
    return out;
  }

  function sendToWs(f32) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(toInt16(f32).buffer);
  }

  function stopWakeMode() {
    state = 'IDLE';
    if (ws) { try { ws.close(); } catch (e) {} ws = null; }
    if (processor) { try { processor.disconnect(); } catch (e) {} processor = null; }
    if (micStream) { micStream.getTracks().forEach(function (t) { t.stop(); }); micStream = null; }
    if (audioCtx) { try { audioCtx.close(); } catch (e) {} audioCtx = null; }
    ring = [];
    stopPlayback();
    setStatus('免提唤醒已关闭');
  }

  function startWakeMode() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      // 手机上 navigator.mediaDevices 是 undefined 的头号原因不是浏览器旧，
      // 而是**非安全上下文**：浏览器只在 HTTPS 或 localhost 下把麦克风给网页
      setStatus('⚠️ 这个页面拿不到麦克风权限 —— 浏览器只在 HTTPS 或本机 localhost 下允许录音。'
        + ' 想用免提就用 https:// 打开（先在电脑上跑 python make_cert.py，见 README「手机使用」）；'
        + ' 或者点左边的麦克风按钮手动录音');
      return;
    }
    navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    }).then(function (s) {
      micStream = s;
      audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
      audioCtx.resume();
      // 16k 只是"请求"，移动端常给 44.1k/48k —— 按实测值折算每帧要攒多少原生样本
      nativeChunk = Math.round(CHUNK * (audioCtx.sampleRate || 16000) / 16000);
      rateInfo = Math.round((audioCtx.sampleRate || 16000) / 1000) + 'kHz'
        + (nativeChunk === CHUNK ? '' : ' → 16k重采样');
      beep([880, 1320], 0.15);            // 开启提示音（用户手势内，解锁自动播放）
      connectWS();
      var src = audioCtx.createMediaStreamSource(s);
      processor = audioCtx.createScriptProcessor(1024, 1, 1);
      processor.onaudioprocess = function (ev) {
        var f32 = ev.inputBuffer.getChannelData(0);
        ring.push.apply(ring, f32);
        while (ring.length >= nativeChunk) {
          var chunk = Float32Array.from(ring.splice(0, nativeChunk));
          sendToWs(resample(chunk, CHUNK));  // 重采样回 16k，保证每帧就是 0.1s
        }
      };
      src.connect(processor);
      // ScriptProcessor 必须连到 destination 才会回调；但直连会把麦克风原样放出来
      // （外放啸叫、iOS 还会因此压低音量），所以中间串一个零增益节点
      var silent = audioCtx.createGain();
      silent.gain.value = 0;
      processor.connect(silent);
      silent.connect(audioCtx.destination);
      state = 'LISTENING';
      setStatus(listeningMsg());
    }).catch(function (e) {
      var hint = e.name === 'NotAllowedError' ? '请在浏览器地址栏允许麦克风权限后重试'
        : e.name === 'NotFoundError' ? '未检测到麦克风设备'
        : e.name === 'NotReadableError' ? '麦克风被其他应用占用了，关掉别的录音/通话应用再试'
        : e.name;
      setStatus('⚠️ 无法访问麦克风（' + hint + '）。手动点击麦克风按钮仍可对话');
    });
  }

  function wakeToggle() {
    if (state === 'IDLE') startWakeMode();
    else stopWakeMode();
  }

  // 停止播报：通知服务器终止当前回复 + 本地立即静音
  function wakeStop() {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'stop' }));
    }
    stopPlayback();
  }

  // 按钮点击由 Gradio 的 click 事件（js 参数）调用本函数，勿在此重复绑定防双重切换
  window.wakeToggle = wakeToggle;
  window.wakeStop = wakeStop;
  window.addEventListener('beforeunload', function () {
    if (state !== 'IDLE') stopWakeMode();
  });
  // iOS 上 beforeunload 不可靠，pagehide 更准（切后台/关闭都会触发）
  window.addEventListener('pagehide', function () {
    if (state !== 'IDLE') stopWakeMode();
  });

  // 手机锁屏/切后台后 AudioContext 会被挂起、WebSocket 会被系统掐断。
  // 回前台时恢复音频上下文并立刻重连（不等 2 秒定时器），否则会一直在那重连空转。
  document.addEventListener('visibilitychange', function () {
    if (document.hidden || state === 'IDLE') return;
    if (audioCtx && audioCtx.state === 'suspended') {
      try { audioCtx.resume(); } catch (e) {}
    }
    if (!ws || ws.readyState > 1) connectWS();
  });
})();
