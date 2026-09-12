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

  var CHUNK = 1600;  // 每帧样本数（0.1s @ 16k）

  var state = 'IDLE';  // IDLE | LISTENING（仅影响按钮/状态文案）
  var ws = null;
  var wsRetry = 0;
  var audioCtx = null;
  var micStream = null;
  var processor = null;
  var ring = [];       // 浮点采样缓冲，凑满 CHUNK 发一帧
  var playQueue = [];  // 待播放的 Audio 元素队列
  var currentAudio = null;

  var statusEl = document.getElementById('wake-status');

  function setStatus(text) {
    if (statusEl) statusEl.textContent = text;
  }

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
    var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    ws = new WebSocket(proto + location.host + '/ws/assistant');
    ws.binaryType = 'arraybuffer';
    ws.onopen = function () {
      wsRetry = 0;
      if (state === 'LISTENING') setStatus('🟢 免提聆听中 — 说「小音」唤醒');
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
        setStatus('🟢 免提聆听中 — 说「小音」唤醒');
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
    document.body.appendChild(a);  // 让「停止播报」按钮的 querySelectorAll('audio') 能暂停它
    playQueue.push(a);
    if (!currentAudio) playNext();
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
        setStatus('🔊 浏览器阻止自动播放，请点击页面任意处');
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
      setStatus('⚠️ 当前浏览器不支持麦克风采集，请使用 Chrome/Edge');
      return;
    }
    navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    }).then(function (s) {
      micStream = s;
      audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
      audioCtx.resume();
      beep([880, 1320], 0.15);            // 开启提示音（用户手势内，解锁自动播放）
      connectWS();
      var src = audioCtx.createMediaStreamSource(s);
      processor = audioCtx.createScriptProcessor(1024, 1, 1);
      processor.onaudioprocess = function (ev) {
        var f32 = ev.inputBuffer.getChannelData(0);
        ring.push.apply(ring, f32);
        while (ring.length >= CHUNK) {
          var chunk = ring.splice(0, CHUNK);
          sendToWs(Float32Array.from(chunk));  // 持续推流：服务器按会话状态路由
        }
      };
      src.connect(processor);
      processor.connect(audioCtx.destination);  // 必须连 destination 才会回调
      state = 'LISTENING';
      setStatus('🟢 免提聆听中 — 说「小音」唤醒');
    }).catch(function (e) {
      var hint = e.name === 'NotAllowedError' ? '请在浏览器地址栏允许麦克风权限后重试'
        : e.name === 'NotFoundError' ? '未检测到麦克风设备'
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
})();
