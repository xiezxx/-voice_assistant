/* 小音桌面宠物 —— Live2D 仙狐精灵 Senko（狐耳狐尾的精灵少女，Cubism 3/4）。
 *
 * 灵性化：浮空漂浮 / 柔和光晕 / 瞳孔跟随鼠标 / 随机小动作 / 圆形光点。
 * 状态驱动：idle 呼吸眨眼 / listening 狐耳竖起 / thinking 眼珠上翻 /
 * speaking 嘴巴开合 / reject 摇头震惊 / sleep 闭眼打盹（官方睡觉动作）。
 * 桥：JS 每 120ms 调 pywebview.api.pull_state() 拉取 Python 推送的最新状态。
 */
const MODEL_NAME = new URLSearchParams(location.search).get("model") || "senko";
const MODEL_URLS = {
  senko: "/model/senko/senko.model3.json",
  hijiki: "/model/hijiki/hijiki.model3.json",
  pio: "/model/pio/model.json",
};
let app = null, model = null;
let state = "idle", onTop = true, started = false;
let bubbleTimer = null, rejectTimer = null, blinkAt = 3, blinkUntil = 0, blinkClosed = false;
let sparks = [];       // 圆形光点（颜色元素）
let hearts = [];         // 爱心粒子（摸头反馈）
let clickTimer = null;   // 单击/双击区分
let userScale = 1, baseFit = 1, fitted = false;
let baseY = 0;
let mousePX = 0, mousePY = 0, pupilX = 0, pupilY = 0;
let earTwitchAt = 4, earTwitchUntil = 0;
let lookAt = 0, lookUntil = 0, lookDir = 0;
let action = { name: null, until: 0, next: 6 };
let lastTouch = performance.now();   // 最近一次互动时间（自动散步用）
let wander = null;                   // 散步状态 {homeX, homeY, phase, start, dur, dir}
const WANDER_SEC = 90;               // 1 分半钟无互动开始散步
let motionUntil = 0, nextIdleMotion = 8;

function apiCall(name, ...a) {
  if (window.pywebview && window.pywebview.api) {
    try { return window.pywebview.api[name](...a).catch(() => {}); } catch (e) {}
  }
  return Promise.resolve();
}

function init() {
  if (started) return;
  started = true;
  initPixi();
  setInterval(pollState, 120);
  const c = document.getElementById("pet");
  c.addEventListener("click", onPetClick);
  document.addEventListener("contextmenu", e => {
    e.preventDefault();
    if (e.target === c) showMenu();
  });
  document.getElementById("m-top").addEventListener("click", toggleTop);
  document.getElementById("m-model").addEventListener("click", () => { apiCall("switch_model"); hideMenu(); });
  document.getElementById("m-pat").addEventListener("click", () => { patHead(); hideMenu(); });
  document.getElementById("m-big").addEventListener("click", () => { zoomBy(0.1); hideMenu(); });
  document.getElementById("m-small").addEventListener("click", () => { zoomBy(-0.1); hideMenu(); });
  document.getElementById("m-stop").addEventListener("click",
      () => { apiCall("stop"); hideMenu(); });
  document.getElementById("m-exit").addEventListener("click", () => apiCall("exit_app"));
  let wheelT = 0;
  c.addEventListener("wheel", e => {
    e.preventDefault();
    const now = performance.now();
    if (now - wheelT < 50) return;
    wheelT = now;
    zoomBy(e.deltaY < 0 ? 0.08 : -0.08);
  }, { passive: false });
  let dragging = false, moved = 0, lastX = 0, lastY = 0, pendingDx = 0, pendingDy = 0;
  window.__petDragging = () => dragging;
  c.addEventListener("mousedown", e => {
    touchReset();
    dragging = true; moved = 0;
    lastX = e.screenX; lastY = e.screenY;
    pendingDx = 0; pendingDy = 0;
    e.preventDefault();
  });
  window.addEventListener("mousemove", e => {
    mousePX = (e.clientX / window.innerWidth - 0.5) * 1.2;
    mousePY = (e.clientY / window.innerHeight - 0.5) * 1.2;
    if (!dragging) return;
    const dx = e.screenX - lastX, dy = e.screenY - lastY;
    if (dx === 0 && dy === 0) return;
    lastX = e.screenX; lastY = e.screenY;
    moved += Math.abs(dx) + Math.abs(dy);
    pendingDx += dx; pendingDy += dy;
  });
  window.addEventListener("mouseup", () => {
    dragging = false;
    if (pendingDx || pendingDy) {
      apiCall("move_by", pendingDx, pendingDy);
      pendingDx = 0; pendingDy = 0;
    }
  });
  setInterval(() => {
    if (dragging && (pendingDx || pendingDy)) {
      apiCall("move_by", pendingDx, pendingDy);
      pendingDx = 0; pendingDy = 0;
    }
  }, 40);
  c.addEventListener("click", e => {
    if (moved > 5) { e.stopImmediatePropagation(); }
  }, true);
}
document.addEventListener("DOMContentLoaded", init);
window.addEventListener("pywebviewready", init);

// ── 场景 ──
function initPixi() {
  app = new PIXI.Application({
    view: document.getElementById("pet"),
    width: window.innerWidth,
    height: window.innerHeight,
    resolution: Math.min(window.devicePixelRatio || 1, 2),
    autoDensity: true,
    transparent: true,
    backgroundAlpha: 0,
    antialias: true,
    autoStart: true,
  });
  const status = document.getElementById("status");
  status.style.display = "block";
  status.textContent = "模型加载中…";
  apiCall("get_config").then(cfg => {
    if (cfg && cfg.scale) userScale = Math.max(0.5, Math.min(3, cfg.scale));
    return PIXI.live2d.Live2DModel.from(MODEL_URLS[MODEL_NAME] || MODEL_URLS.senko, { autoInteract: false })
      .then(m => {
        model = m;
        m.anchor.set(0.5, 0.5);
        baseY = window.innerHeight * 0.55;
        m.position.set(window.innerWidth / 2, baseY);
        app.stage.addChild(m);
        initSparks();
        applyScale();
        status.style.display = "none";
        status.textContent = "";
        blinkAt = performance.now() / 1000 + 1.5;
        app.ticker.add(animate);
      });
  })
    .catch(e => {
      status.textContent = "模型加载失败：" + e;
      status.style.display = "block";
      apiCall("log_error", "模型加载失败: " + e);
    });
}

// ── 缩放 ──
function applyScale() {
  if (!model || !baseFit) return;
  model.scale.set(baseFit * userScale, baseFit * userScale);
}
let saveTimer = null;
function zoomBy(delta) {
  userScale = Math.max(0.5, Math.min(3, userScale + delta));
  applyScale();
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => apiCall("save_scale", userScale), 500);
}
function fitOnce() {
  if (fitted || !model || !(model.width > 0)) return;
  fitted = true;
  baseFit = Math.min(window.innerWidth / model.width, window.innerHeight / model.height) * 0.42;
  applyScale();
}

// ── 颜色元素：圆形光点 ──
function initSparks() {
  const colors = [0xFFD9A0, 0xFFB080, 0xFFFFFF, 0xFFD6E4];
  for (let i = 0; i < 6; i++) {
    const g = new PIXI.Graphics();
    const r = 2.5 + (i % 3) * 1.6;
    g.beginFill(colors[i % colors.length], 1);
    g.drawCircle(0, 0, r);
    g.endFill();
    g.beginFill(0xFFFFFF, 0.6);
    g.drawCircle(-r * 0.3, -r * 0.3, r * 0.45);
    g.endFill();
    g.alpha = 0;
    app.stage.addChild(g);
    sparks.push({ sprite: g, phase: i * 1.1, orbit: 80 + (i % 3) * 26, speed: 0.5 + (i % 2) * 0.3 });
  }
}

// ── 互动：爱心粒子（摸头反馈）──
function drawHeart(g, x, y, s) {
  g.beginFill(0xFF8FA6, 0.95);
  g.moveTo(x, y + s * 0.32);
  g.bezierCurveTo(x, y, x - s, y, x - s, y - s * 0.32);
  g.bezierCurveTo(x - s, y - s * 0.75, x, y - s * 0.55, x, y - s * 0.1);
  g.bezierCurveTo(x, y - s * 0.55, x + s, y - s * 0.75, x + s, y - s * 0.32);
  g.bezierCurveTo(x + s, y, x, y, x, y + s * 0.32);
  g.endFill();
}

function burstHearts(n = 6) {
  for (let i = 0; i < n; i++) {
    const g = new PIXI.Graphics();
    const s = 7 + Math.random() * 7;
    drawHeart(g, 0, 0, s);
    g.alpha = 0;
    app.stage.addChild(g);
    hearts.push({
      sprite: g,
      x: model.position.x + (Math.random() - 0.5) * 60,
      y: model.position.y - 20,
      vx: (Math.random() - 0.5) * 2.2,
      vy: -2.5 - Math.random() * 1.6,
      life: 0,
      max: 1.4 + Math.random() * 0.6,
      rot: (Math.random() - 0.5) * 0.8,
    });
  }
}

function updateHearts(dt) {
  for (let i = hearts.length - 1; i >= 0; i--) {
    const h = hearts[i];
    h.life += dt;
    if (h.life >= h.max) {
      h.sprite.destroy();
      hearts.splice(i, 1);
      continue;
    }
    h.x += h.vx * dt * 60;
    h.y += h.vy * dt * 60;
    h.vy += 0.06 * dt * 60;
    h.sprite.position.set(h.x, h.y);
    h.sprite.rotation += h.rot * dt;
    h.sprite.alpha = 1 - h.life / h.max;
  }
}

// ── 互动：摸头（双击 / 菜单触发）──
function patHead() {
  if (!model) return;
  if (MODEL_NAME === "pio") playMotion("", 20 + Math.floor(Math.random() * 6));   // Touch1-6
  else if (MODEL_NAME === "hijiki") playMotion("Tap", Math.floor(Math.random() * 3));
  else playMotion("Taphead", 0);
  burstHearts();
}


// ── 参数驱动 ──
function setParam(id, v) {
  try {
    const core = model.internalModel.coreModel;
    if (core.setParameterValueById) core.setParameterValueById(id, v);
    else if (core.setParamFloat) core.setParamFloat(id, v);
  } catch (e) {}
}

// 参数映射：senko 用 ParamXxx（cubism4）；hijiki 用 PARAM_XXX（cubism4）；
// pio 用 PARAM_XXX（cubism2）
const PARAMS = MODEL_NAME === "pio" ? {
  angleX: "PARAM_ANGLE_X", angleZ: "PARAM_ANGLE_Z",
  bodyX: "PARAM_BODY_ANGLE_X", breath: "PARAM_BREATH",
  eyeL: "PARAM_EYE_L_OPEN", eyeR: "PARAM_EYE_R_OPEN",
  ballX: "PARAM_EYE_BALL_X", ballY: "PARAM_EYE_BALL_Y",
  mouthOpen: "PARAM_MOUTH_OPEN_Y", mouthForm: "PARAM_MOUTH_FORM",
  earL: "PARAM_EAR_DEFORM", earR: "PARAM_EAR_DEFORM",
  blush: "PARAM_BLUSH", emotion: "PARAM_EMOTION",
  wing: "PARAM_WING_ANGLE",
  tail: "",
} : MODEL_NAME === "hijiki" ? {
  angleX: "PARAM_ANGLE_X", angleZ: "PARAM_ANGLE_Z",
  bodyX: "PARAM_BODY_ANGLE_X", breath: "PARAM_BREATH",
  eyeL: "PARAM_EYE_L_OPEN", eyeR: "PARAM_EYE_R_OPEN",
  ballX: "PARAM_EYE_BALL_X", ballY: "PARAM_EYE_BALL_Y",
  mouthOpen: "PARAM_MOUTH_OPEN_Y", mouthForm: "PARAM_MOUTH_FORM",
  earL: "PARAM_EAR_L", earR: "PARAM_EAR_R",
  blush: "", emotion: "PARAM_TAIL_ANGRY",
  wing: "", tail: "PARAM_TAIL",
} : {
  angleX: "ParamAngleX", angleZ: "ParamAngleZ",
  bodyX: "ParamBodyAngleX", breath: "ParamBreath",
  eyeL: "ParamEyeLOpen", eyeR: "ParamEyeROpen",
  ballX: "ParamEyeBallX", ballY: "ParamEyeBallY",
  mouthOpen: "ParamMouthOpenY", mouthForm: "ParamMouthForm",
  earL: "ParamEarLeft", earR: "ParamEarRight",
  blush: "ParamCheek", emotion: "ParamShock",
  wing: "", tail: "",
};

function playMotion(group, index) {
  try {
    const motion = model.motion(group, index);
    if (!motion) return;
    motion.play();
    motionUntil = performance.now() / 1000 + (motion.duration || 2);
  } catch (e) {}
}

function animate() {
  if (!model) return;
  try {
    animateInner();
  } catch (e) {
    apiCall("log_error", "animate 异常: " + e);
  }
}

function animateInner() {
  fitOnce();
  const t = performance.now() / 1000;
  // 浮空漂浮
  const hoverAmp = state === "speaking" ? 2 : state === "sleep" ? 1.5 : 5;
  model.position.y = baseY + Math.sin(t * 1.4) * hoverAmp;
  // 光点
  for (const sp of sparks) {
    const spr = sp.sprite;
    if (state === "sleep") { spr.alpha = 0; continue; }
    const ang = t * sp.speed + sp.phase;
    const rad = sp.orbit + Math.sin(t * 1.6 + sp.phase) * 14;
    spr.position.set(
      model.position.x + Math.cos(ang) * rad,
      model.position.y + Math.sin(ang * 1.35) * rad * 0.75
    );
    const base = state === "speaking" ? 0.7 : state === "listening" ? 0.55 : 0.28;
    spr.alpha = base + Math.sin(t * 2.4 + sp.phase) * 0.18;
  }

  updateHearts(1 / 60);

  // 自动散步（仅黑猫）：3 分钟无互动后左右踱步
  if (MODEL_NAME === "hijiki" && state === "idle") {
    if (!wander && t - lastTouch / 1000 > WANDER_SEC) {
      apiCall("get_pos").then(pos => {
        wander = { homeX: pos[0], homeY: pos[1], phase: 0, start: t, dur: 8, dir: -1 };
      });
    }
    if (wander) {
      const w = wander;
      const overlay = document.getElementById("walk-overlay");
      if (overlay && !overlay._walking) {
        overlay._walking = true;
        overlay.style.display = "block";
        model.visible = false;               // 隐藏坐姿模型，显示像素走路猫
      }
      const p = (t - w.start) / w.dur;
      if (w.phase === 0 && p >= 1) {            // 走到左端 → 停 1.5s
        w.phase = 1; w.start = t; w.dur = 1.5;
      } else if (w.phase === 1 && p >= 1) {     // 走到右端（跨过家）
        w.phase = 2; w.start = t; w.dur = 10; w.dir = 1;
      } else if (w.phase === 2 && p >= 1) {     // 走回家
        w.phase = 3; w.start = t; w.dur = 8; w.dir = -0.6;
      } else if (w.phase === 3 && p >= 1) {
        wander = null;
        lastTouch = performance.now();          // 完成一轮，重新计时
        const ov = document.getElementById("walk-overlay");
        if (ov) { ov.style.display = "none"; ov._walking = false; }
        if (model) model.visible = true;
      }
      if (wander) {
        const ease = w.phase === 1 ? 0 : p * p * (3 - 2 * p);   // 平滑缓动
        let targetX = w.homeX;
        if (w.phase === 0) targetX = w.homeX + w.dir * 160 * ease;
        else if (w.phase === 2) targetX = w.homeX - 160 + w.dir * 320 * ease;
        else if (w.phase === 3) targetX = w.homeX + 160 * (1 - ease);
        apiCall("move_to", targetX, w.homeY);
        // 像素猫朝向
        if (overlay) {
          const dir = w.phase === 2 ? 1 : -1;
          overlay.style.transform = "translateX(-50%) scaleX(" + dir + ")";
        }
      }
    }
  }
  // 官方动作播放期间让动作独占
  if (t < motionUntil) return;
  if (state === "idle" && t >= nextIdleMotion) {
    if (MODEL_NAME === "pio") playMotion("idle", 1 + Math.floor(Math.random() * 8));
    else if (MODEL_NAME === "hijiki") playMotion("Idle", Math.floor(Math.random() * 3));
    else playMotion("Idle", 0);
    nextIdleMotion = t + 14 + Math.random() * 10;
  }

  // 瞳孔平滑跟随
  pupilX += (mousePX - pupilX) * 0.08;
  pupilY += (mousePY - pupilY) * 0.08;

  // 基础
  setParam(PARAMS.breath, 0.5 + Math.sin(t * 1.4) * 0.5);
  setParam(PARAMS.bodyX, Math.sin(t * 0.9) * 1.5);
  setParam(PARAMS.angleX, 0);
  setParam(PARAMS.angleZ, 0);
  setParam(PARAMS.ballX, pupilX);
  setParam(PARAMS.ballY, pupilY);
  setParam(PARAMS.mouthOpen, 0);
  setParam(PARAMS.mouthForm, 0);
  setParam(PARAMS.earL, 0.2);
  setParam(PARAMS.earR, 0.2);
  setParam(PARAMS.blush, 0);
  setParam("ParamEyeLSmile", 0);
  setParam("ParamEyeRSmile", 0);
  setParam(PARAMS.emotion, window.__petDragging && window.__petDragging() ? 1 : 0);
  if (PARAMS.tail) setParam(PARAMS.tail, 0.3 + Math.sin(t * 1.7) * 0.3);   // 尾巴轻摆
  if (PARAMS.wing) {
    setParam(PARAMS.wing, 0.3 + Math.sin(t * 2.2) * 0.25);   // 小翅膀轻扇
  }
  model.alpha = 1;

  // 眨眼
  if (state !== "sleep") {
    if (t > blinkAt && !blinkClosed) { blinkClosed = true; blinkUntil = t + 0.12; }
    if (blinkClosed && t > blinkUntil) { blinkClosed = false; blinkAt = t + 2 + Math.random() * 3; }
  }
  const eye = state === "sleep" ? 0 : (blinkClosed ? 0.05 : 1);
  setParam(PARAMS.eyeL, eye);
  setParam(PARAMS.eyeR, eye);

  // 随机小动作（待机）
  if (state === "idle") {
    if (t > earTwitchAt) { earTwitchUntil = t + 0.3; earTwitchAt = t + 5 + Math.random() * 8; }
    if (t < earTwitchUntil) {
      const side = Math.floor(earTwitchAt) % 2;
      setParam(side ? "ParamEarLeft" : "ParamEarRight", Math.sin(t * 40) * 0.8 + 0.8);
    }
    if (t > lookAt) { lookUntil = t + 1.6; lookDir = Math.random() < 0.5 ? 1 : -1; lookAt = t + 6 + Math.random() * 8; }
    if (t < lookUntil && action.name !== "tilt") {
      setParam(PARAMS.ballX, pupilX + lookDir * 0.5);
      setParam(PARAMS.ballY, pupilY + 0.15);
      setParam(PARAMS.angleZ, lookDir * 4);
    }
    if (t >= action.next) {
      const acts = ["yawn", "tilt", "shake", "double_blink"];
      action.name = acts[Math.floor(Math.random() * acts.length)];
      action.until = t + 1.1 + Math.random() * 0.9;
      action.next = t + 8 + Math.random() * 10;
    }
    if (action.name && t >= action.until) action.name = null;
    if (action.name === "yawn") {
      setParam(PARAMS.eyeL, 0.08);
      setParam(PARAMS.eyeR, 0.08);
      setParam(PARAMS.mouthOpen, 0.5 + Math.sin(t * 3) * 0.3);
      setParam(PARAMS.bodyX, -6);
    } else if (action.name === "tilt") {
      setParam(PARAMS.angleZ, 9);
      setParam(PARAMS.earL, 1);
      
      setParam(PARAMS.blush, 0.8);
      setParam(PARAMS.ballX, pupilX + 0.35);
      setParam(PARAMS.ballY, pupilY + 0.2);
    } else if (action.name === "shake") {
      setParam(PARAMS.bodyX, Math.sin(t * 34) * 5);
      setParam(PARAMS.angleZ, Math.sin(t * 34) * 4);
    } else if (action.name === "double_blink") {
      const phase = (t * 8) % 2;
      if (phase < 0.25) {
        setParam(PARAMS.eyeL, 0.05);
        setParam(PARAMS.eyeR, 0.05);
      }
    }
  }

  // 状态姿态
  if (state === "listening") {
    setParam(PARAMS.earL, 1);
    setParam(PARAMS.earR, 1);
    setParam(PARAMS.angleZ, 6);
    setParam("ParamEyeLSmile", 0.6);
    setParam("ParamEyeRSmile", 0.6);
    setParam(PARAMS.blush, 0.5);
  } else if (state === "thinking") {
    setParam(PARAMS.angleZ, -5);
    setParam(PARAMS.ballX, 0.3);
    setParam(PARAMS.ballY, -0.4);
    setParam(PARAMS.mouthForm, 0.3);
  } else if (state === "speaking") {
    const k = 0.25 + Math.abs(Math.sin(t * 9)) * 0.75;
    setParam(PARAMS.mouthOpen, k);
    setParam(PARAMS.angleX, Math.sin(t * 7) * 0.8);
    if (PARAMS.wing) setParam(PARAMS.wing, 0.5 + Math.abs(Math.sin(t * 10)) * 0.5);
  } else if (state === "reject") {
    setParam(PARAMS.angleZ, Math.sin(t * 26) * 16);
    setParam(PARAMS.emotion, 1);
  } else if (state === "sleep") {
    model.alpha = 0.85;
    setParam(PARAMS.bodyX, Math.sin(t * 0.6) * 0.8);
  }
}

// ── 状态桥 ──
function pollState() { apiCall("pull_state").then(applyUpdate); }
function applyUpdate(upd) {
  if (!upd) return;
  if (upd.state) setState(upd.state);
  if (upd.text) showBubble(upd.text, upd.state === "idle" ? 5000 : 0);
  else if (["idle", "listening", "sleep"].includes(upd.state)) hideBubble();
}
function setState(s) {
  if (s === state) return;
  if (s !== "idle") touchReset();
  state = s;
  if (state === "reject" && !rejectTimer)
    rejectTimer = setTimeout(() => setState("idle"), 2200);
  else if (state !== "reject") { clearTimeout(rejectTimer); rejectTimer = null; }
  if (model) {
    if (state === "listening") {
      if (MODEL_NAME === "pio") playMotion("idle", 0);   // WakeUp
      else if (MODEL_NAME === "hijiki") playMotion("Tap", 0);
      else playMotion(Math.random() < 0.5 ? "Tap" : "Taphead", 0);
    }
    else if (state === "sleep") playMotion("Sleeping", 0);
  }
}

function touchReset() {
  lastTouch = performance.now();
  if (wander) {
    const w = wander;
    wander = null;
    apiCall("move_to", w.homeX, w.homeY);   // 被打断：立刻回家
    const ov = document.getElementById("walk-overlay");
    if (ov) { ov.style.display = "none"; ov._walking = false; }
    if (model) model.visible = true;
  }
}

// 点击菜单以外任意处 → 自动收起菜单
document.addEventListener("click", e => {
  const menu = document.getElementById("menu");
  if (menu && menu.style.display === "block" && !menu.contains(e.target)) hideMenu();
});

function onPetClick() {
  hideMenu();
  if (clickTimer) {
    clearTimeout(clickTimer);
    clickTimer = null;
    patHead();                       // 双击 = 摸头卖萌（不聆听）
    return;
  }
  clickTimer = setTimeout(() => {
    clickTimer = null;
    apiCall("listen");               // 单击 = 免唤醒词聆听
  }, 300);
}
let menuAutoHide = null;
function showMenu() {
  const topHint = document.getElementById("m-top-hint");
  if (topHint) topHint.textContent = onTop ? "开" : "关";
  const modelHint = document.getElementById("m-model-hint");
  if (modelHint) modelHint.textContent =
    MODEL_NAME === "senko" ? "仙狐" : MODEL_NAME === "hijiki" ? "黑猫" : "Pio";
  document.getElementById("menu").style.display = "block";
  clearTimeout(menuAutoHide);
  menuAutoHide = setTimeout(hideMenu, 4000);   // 4 秒无操作自动关闭
}
function toggleTop() { onTop = !onTop; apiCall("toggle_on_top"); hideMenu(); }
function hideMenu() {
  document.getElementById("menu").style.display = "none";
  clearTimeout(menuAutoHide);
}
function showBubble(text, autoHideMs) {
  const b = document.getElementById("bubble");
  b.textContent = text; b.style.display = "block";
  clearTimeout(bubbleTimer);
  if (autoHideMs) bubbleTimer = setTimeout(hideBubble, autoHideMs);
}
function hideBubble() { document.getElementById("bubble").style.display = "none"; }
