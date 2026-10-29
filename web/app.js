/* 解机 jieji · 前端
 * 纯原生 JS，没有构建步骤，也没有依赖。
 * 只做四件事：连 SSE、画现场、发指令、改设置。
 */
(() => {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const state = {
    snapshot: null,
    settings: null,
    meta: null,
    mode: 'idle',
    estop: false,
    lastFrameSeq: 0,
    frameTimes: [],
    history: { decisions: 0, tokens: 0, latency: 0, blocked: 0, cache: 0 },
    // 默认闭嘴：不碰你的音箱。要出声必须自己在页面上勾「朗读」。
    speak: false,
  };

  // ---------------------------------------------------------------- 小工具
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));

  const fmt = {
    ms: (v) => (v == null ? '—' : `${Math.round(v)}ms`),
    m: (v) => (v == null ? '—' : `${Number(v).toFixed(2)}m`),
  };

  let toastTimer = null;
  function toast(text, bad = false) {
    const el = $('#toast');
    el.textContent = text;
    el.className = 'toast' + (bad ? ' bad' : '');
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 2600);
  }

  async function api(url, options = {}) {
    const res = await fetch(url, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (_) { /* ignore */ }
      throw new Error(`${res.status} ${detail}`);
    }
    return res.status === 204 ? null : res.json();
  }

  // ---------------------------------------------------------------- 现场流
  const feed = $('#feed');
  let rendered = 0;

  function pushNode(node) {
    const empty = feed.querySelector('.empty');
    if (empty) empty.remove();
    feed.appendChild(node);
    rendered += 1;
    while (feed.children.length > 90) feed.removeChild(feed.firstChild);
    feed.scrollTop = feed.scrollHeight;
  }

  function bubble(kind, text, meta) {
    const el = document.createElement('div');
    el.className = `msg ${kind}`;
    el.innerHTML = `<div class="say-text">${esc(text)}</div>` +
      (meta ? `<div class="meta">${esc(meta)}</div>` : '');
    pushNode(el);
  }

  function decisionCard(ev) {
    const a = ev.action || {};
    const el = document.createElement('div');
    el.className = 'decision';
    const blocked = ev.blocked ? `<span class="tag blocked">被拦：${esc(ev.blocked)}</span>` : '';
    const button = a.button ? `<span class="tag">键 ${esc(a.button)}</span>` : '';
    const say = a.say ? `<div class="say-text">「${esc(a.say)}」</div>` : '';
    el.innerHTML = `
      <div class="row">
        <span class="tag ${esc(a.action)}">${esc(a.action)}</span>
        ${a.action === 'forward' || a.action === 'backward' || a.action === 'turn_left' || a.action === 'turn_right'
          ? `<span class="tag">速度 ${esc(a.speed)}</span><span class="tag">${esc(a.duration_ms)}ms</span>` : ''}
        ${button}${blocked}
      </div>
      ${say}
      <div class="why">${esc(a.why || '')}</div>
      <div class="row" style="gap:6px">
        <span class="tag">${esc(a.mood || '平静')}</span>
        <span class="tag">${esc(fmt.ms(ev.latency_ms))}</span>
        <span class="tag">${esc(ev.tokens || 0)} tok</span>
        <span class="tag">${esc(ev.model || '')}</span>
      </div>`;
    pushNode(el);

    state.history.decisions += 1;
    state.history.tokens = ev.tokens || 0;
    state.history.latency = ev.latency_ms || state.history.latency;
    renderStats();
    speak(a.say);
  }

  function actionResult(ev) {
    const a = ev.action || {};
    const r = ev.result || {};
    if (a.action === 'say' || a.action === 'stop') {
      if (!r.ok && r.detail) bubble('sys', `⚠️ ${r.detail}`);
      return;
    }
    const ok = r.ok ? '✓' : '✗';
    const detail = r.detail || '';
    bubble('sys', `${ok} ${a.action}${a.button ? '(' + a.button + ')' : ''} · ${detail}`);
  }

  // ---------------------------------------------------------------- 语音
  // 默认不出声。只有用户自己勾了「朗读」，才会用系统听筒读模型说的那句话；
  // 系统提示、错误、日志一律不读 —— 半夜突然说话比什么都吓人。
  let ttsReady = 'speechSynthesis' in window;
  function speak(text) {
    if (!state.speak || !ttsReady || !text) return;
    try {
      const u = new SpeechSynthesisUtterance(text);
      u.lang = 'zh-CN';
      u.rate = 1.05;
      u.pitch = 1.0;
      u.volume = 0.9;
      window.speechSynthesis.cancel();
      window.speechSynthesis.speak(u);
    } catch (_) { /* 静默失败，绝不弹框 */ }
  }

  // ---------------------------------------------------------------- SSE
  function connect() {
    const es = new EventSource('/api/events');
    es.onopen = () => setPill('mode', state.mode === 'auto' ? '自动巡逻' : '待机', state.mode === 'auto' ? 'on' : '');
    es.onerror = () => {
      es.close();
      setTimeout(connect, 1500);
    };
    es.onmessage = (raw) => {
      let ev;
      try { ev = JSON.parse(raw.data); } catch (_) { return; }
      handle(ev);
    };
  }

  function handle(ev) {
    switch (ev.kind) {
      case 'snapshot':
        state.snapshot = ev;
        applySnapshot(ev);
        break;
      case 'hello':
        setPill('driver', `身体 ${ev.driver || '—'}`, ev.driver && ev.driver !== 'none' ? 'on' : 'warn');
        setPill('camera', `眼 ${ev.camera || '—'}`, ev.camera === 'mock' ? '' : 'on');
        setPill('audio', `耳 ${ev.audio || '—'}`, ev.audio === 'mock' ? '' : 'on');
        setPill('model', `脑 ${ev.model || '—'}`, ev.model ? 'on' : 'warn');
        if (ev.rebuilt) toast('设置已生效，传感器/身体已重建');
        break;
      case 'decision':
        decisionCard(ev);
        break;
      case 'action':
        actionResult(ev);
        break;
      case 'utterance':
        if (ev.ignored) {
          bubble('sys', `（听到「${ev.text}」，但没有唤醒词，忽略）`);
        } else {
          bubble('heard', ev.text, `${ev.source === 'mic' ? '麦克风' : '文字'} · ${ev.duration ?? 0}s`);
        }
        break;
      case 'say':
        speak(ev.text);
        break;
      case 'mode':
        state.mode = ev.mode;
        updateModeButton();
        break;
      case 'estop':
        state.estop = !!ev.active;
        $('#estop-banner').hidden = !state.estop;
        break;
      case 'error':
        bubble('sys', `⚠️ ${ev.error || ev.where || '出错了'}`);
        break;
      default:
        break;
    }
  }

  function setPill(name, text, cls) {
    const el = document.getElementById(`pill-${name}`);
    if (!el) return;
    el.textContent = text;
    el.className = 'pill ' + (cls || '');
  }

  function updateModeButton() {
    const btn = $('#btn-auto');
    btn.classList.toggle('active', state.mode === 'auto');
    btn.textContent = state.mode === 'auto' ? '巡逻中…' : '自动巡逻';
    setPill('mode', state.mode === 'auto' ? '自动巡逻' : '待机', state.mode === 'auto' ? 'on' : '');
  }

  // ---------------------------------------------------------------- 状态刷新
  function applySnapshot(snap) {
    const tel = (snap.driver && snap.driver.telemetry) || {};
    const cam = snap.camera || {};
    const safety = snap.safety || {};

    $('#hud-front').textContent = tel.front_m != null ? `前方 ${Number(tel.front_m).toFixed(2)}m` : '前方 —';
    $('#hud-heading').textContent = tel.heading_deg != null ? `朝向 ${Math.round(tel.heading_deg)}°` : '朝向 —';
    $('#hud-battery').textContent = tel.battery != null ? `电量 ${Math.round(tel.battery)}%` : '电量 —';
    $('#hud-fps').textContent = `${cam.fps ?? 0} fps`;

    // 顶栏状态以快照为准（SSE 只负责增量事件，不负责首屏）
    if (snap.driver) {
      setPill('driver', `身体 ${snap.driver.name}`, snap.driver.name === 'none' ? 'warn' : 'on');
    }
    if (snap.camera) {
      setPill('camera', `眼 ${snap.camera.source}`, cam.error ? 'bad' : (cam.source === 'mock' ? '' : 'on'));
    }
    if (snap.audio) {
      setPill('audio', `耳 ${snap.audio.source === 'mock' ? 'mock' : (snap.audio.source + (snap.audio.error ? ' 出错' : ''))}`,
        snap.audio.error ? 'bad' : (snap.audio.source === 'mock' ? '' : 'on'));
    }
    if (snap.brain && snap.brain.model) {
      setPill('model', `脑 ${snap.brain.model}`, 'on');
    }

    state.estop = !!safety.estop;
    $('#estop-banner').hidden = !state.estop;
    state.mode = snap.mode || 'idle';
    updateModeButton();

    state.history.blocked = safety.blocked || 0;
    const cache = (snap.brain && snap.brain.cache) || {};
    state.history.cache = cache.hits || 0;
    $('#st-uptime').textContent = `${Math.round(snap.uptime_s || 0)}s`;
    renderStats();

    const mapWrap = $('#map');
    if (snap.sim) {
      mapWrap.src = `/api/map.png?t=${Math.floor(Date.now() / 1000)}`;
      mapWrap.style.display = '';
      $('#map-note').textContent = '';
    } else {
      mapWrap.style.display = 'none';
      $('#map-note').textContent = '（当前不是模拟身体）';
    }

    if (cam.error) setPill('camera', `眼 ${cam.source} 出错`, 'bad');
  }

  function renderStats() {
    $('#st-decisions').textContent = state.history.decisions;
    $('#st-latency').textContent = fmt.ms(state.history.latency);
    $('#st-tokens').textContent = state.history.tokens || 0;
    $('#st-blocked').textContent = state.history.blocked || 0;
    $('#st-cache').textContent = state.history.cache || 0;
  }

  async function poll() {
    try {
      const snap = await api('/api/state');
      state.snapshot = snap;
      applySnapshot(snap);
    } catch (_) { /* 服务重启中，忽略 */ }
  }

  // ---------------------------------------------------------------- 控制
  async function sendText(text) {
    const value = (text ?? $('#text').value).trim();
    if (!value) return;
    bubble('user', value, '你');
    $('#text').value = '';
    try {
      await api('/api/chat', { method: 'POST', body: JSON.stringify({ text: value }) });
    } catch (e) {
      toast(e.message, true);
    }
  }

  async function manual(action) {
    try {
      await api('/api/manual', {
        method: 'POST',
        body: JSON.stringify({ action, speed: 0.4, duration_ms: 550 }),
      });
    } catch (e) {
      toast(e.message, true);
    }
  }

  async function toggleAuto() {
    const next = state.mode === 'auto' ? 'idle' : 'auto';
    try {
      await api('/api/mode', { method: 'POST', body: JSON.stringify({ mode: next }) });
      state.mode = next;
      updateModeButton();
    } catch (e) {
      toast(e.message, true);
    }
  }

  async function estop() {
    try {
      if (state.estop) {
        await api('/api/estop/clear', { method: 'POST' });
        state.estop = false;
        toast('已解除急停');
      } else {
        await api('/api/estop', { method: 'POST', body: JSON.stringify({ reason: '网页急停' }) });
        state.estop = true;
        toast('已急停');
      }
      $('#estop-banner').hidden = !state.estop;
    } catch (e) {
      toast(e.message, true);
    }
  }

  // ---------------------------------------------------------------- 读懂这一刻
  async function understand() {
    const drawer = $('#understand-drawer');
    const body = $('#understand-body');
    drawer.hidden = false;
    body.innerHTML = '<div class="empty">正在看……（两次模型调用，约 2~4 秒）</div>';
    try {
      const text = (state.snapshot?.utterances || []).slice(-1)[0]?.text || $('#text').value || '';
      const data = await api('/api/understand', { method: 'POST', body: JSON.stringify({ text }) });
      if (!data.ok) throw new Error(data.error || '失败');
      body.innerHTML = kvBlocks(data);
    } catch (e) {
      body.innerHTML = `<div class="empty">⚠️ ${esc(e.message)}</div>`;
    }
  }

  function kvBlocks(data) {
    const v = data.vision || {};
    const vLabels = {
      summary: '一句话概括', scene: '场景', obstacles: '障碍物', people: '人物',
      objects: '主要物体', text: '画面文字', lighting: '光线', floor: '地面与路况', details: '细节',
    };
    const visionRows = Object.keys(v).map((k) => kvRow(vLabels[k] || k, v[k])).join('');
    let audioHtml = '<div class="empty">这次没有说话内容</div>';
    if (data.audio) {
      const aLabels = {
        speaker: '说话人', speech: '原话', summary: '概括', detail: '详细描述',
        emotion: '情绪', intent: '意图', keywords: '关键词', soundscape: '声音环境', lang: '语言',
      };
      const a = data.audio;
      const rows = Object.keys(aLabels).map((k) => {
        const value = Array.isArray(a[k]) ? a[k].join(' / ') : a[k];
        return kvRow(aLabels[k], value);
      }).join('');
      audioHtml = `<dl class="kv">${rows}</dl>`;
    }
    const meta = data.vision_meta || {};
    return `
      <div class="kv-block">
        <h3>解画 · 看见了什么 <span class="muted">${meta.cached ? '缓存命中' : ''} ${meta.ms ? meta.ms + 'ms' : ''}</span></h3>
        <dl class="kv">${visionRows}</dl>
      </div>
      <div class="kv-block">
        <h3>解音 · 听见了什么</h3>
        ${audioHtml}
      </div>`;
  }

  function kvRow(label, value) {
    const blank = value == null || value === '' || value === '未识别';
    return `<div class="kv-row"><dt>${esc(label)}</dt><dd class="${blank ? 'blank' : ''}">${esc(value ?? '')}</dd></div>`;
  }

  // ---------------------------------------------------------------- 设置
  const FIELD_DEFS = {
    brain: [
      { key: 'llm_base_url', label: '接口地址', hint: '任意 OpenAI 兼容服务。DeepSeek: https://api.deepseek.com；本机 Ollama: http://127.0.0.1:11434/v1' },
      { key: 'llm_api_key', label: 'API Key', type: 'password', hint: '只存在服务端，页面只显示掩码。Ollama 不用填。' },
      { key: 'llm_model', label: '模型名', hint: 'deepseek-flash（能看图）/ deepseek-v4-pro（纯文本）/ qwen2.5vl:7b' },
      { key: 'llm_vision', label: '能看图片', type: 'bool', hint: '关掉就是纯语音+状态决策，省一半 token' },
      { key: 'llm_thinking', label: '思考链', type: 'select', options: ['disabled', 'enabled'], hint: '机器人实时闭环务必 disabled，否则延迟 2~4 倍' },
      { key: 'llm_effort', label: '推理档位', type: 'select', options: ['low', 'high', 'max'] },
      { key: 'vision_detail', label: '图片精度', type: 'select', options: ['low', 'high', 'auto'], hint: 'low=512px，更快更便宜' },
      { key: 'decision_interval_ms', label: '思考间隔 (ms)', type: 'int', hint: '自动巡逻时每隔多久看一次' },
      { key: 'llm_max_tokens', label: '输出上限', type: 'int' },
      { key: 'llm_temperature', label: '温度', type: 'float' },
    ],
    senses: [
      { key: 'camera_source', label: '画面来源', type: 'select', options: ['mock', 'opencv', 'file'], hint: 'mock=模拟器；opencv=USB 摄像头；file=循环播视频' },
      { key: 'camera_index', label: '摄像头序号', type: 'int', hint: '0 一般是内置，1 是外接' },
      { key: 'camera_file', label: '视频文件路径', hint: 'camera_source=file 时用' },
      { key: 'camera_fps', label: '采集帧率', type: 'int' },
      { key: 'camera_width', label: '宽度', type: 'int' },
      { key: 'camera_height', label: '高度', type: 'int' },
      { key: 'audio_source', label: '声音来源', type: 'select', options: ['mock', 'mic', 'file'] },
      { key: 'audio_device', label: '麦克风设备', type: 'device', hint: '留空自动挑一个像麦克风的' },
      { key: 'audio_file', label: '音频文件路径' },
      { key: 'asr_provider', label: '识别器', type: 'select', options: ['mock', 'local', 'cloud'], hint: 'local=本机 faster-whisper（免费、离线）；cloud=任何 OpenAI 兼容 ASR' },
      { key: 'asr_model', label: 'whisper 模型', hint: 'tiny/base/small/medium/large-v3' },
      { key: 'cloud_asr_base_url', label: '云端 ASR 地址' },
      { key: 'cloud_asr_api_key', label: '云端 ASR Key', type: 'password' },
      { key: 'cloud_asr_model', label: '云端 ASR 模型' },
      { key: 'vad_threshold', label: 'VAD 阈值', type: 'float', hint: '环境吵就调大，老是切不干就调小' },
      { key: 'wake_required', label: '必须喊唤醒词', type: 'bool', hint: '「小解 / 解机 / 机器人」' },
    ],
    body: [
      { key: 'driver', label: '身体类型', type: 'select', options: ['mock', 'ir', 'button', 'serial', 'none'], hint: 'mock 模拟；ir 红外学习回放；button 舵机压遥控器；serial 串口直驱电机' },
      { key: 'driver_bridge_url', label: 'ESP32 桥地址', hint: 'ir/button 用，例如 ws://192.168.4.1:81' },
      { key: 'driver_serial_port', label: '串口', hint: 'serial 用，例如 COM3' },
      { key: 'driver_serial_baud', label: '波特率', type: 'int' },
      { key: 'ir_repeat_interval_ms', label: '红外重复间隔', type: 'int', hint: '玩具车一般是 100~150ms 一帧' },
      { key: 'press_hold_ms', label: '按压保持 (ms)', type: 'int' },
    ],
    safety: [
      { key: 'max_speed', label: '最高速度 (0~1)', type: 'float' },
      { key: 'max_duration_ms', label: '单次最长动作 (ms)', type: 'int' },
      { key: 'min_action_interval_ms', label: '动作最小间隔 (ms)', type: 'int' },
      { key: 'watchdog_ms', label: '看门狗 (ms)', type: 'int', hint: '超过这么久没有新决策就强制停' },
      { key: 'stale_frame_ms', label: '画面过期 (ms)', type: 'int', hint: '画面旧于这个值，自动模式下不许动' },
      { key: 'reflex_enabled', label: '防撞反射', type: 'bool' },
      { key: 'reflex_stop_m', label: '反射距离 (m)', type: 'float' },
      { key: 'estop_on_start', label: '开机即急停', type: 'bool' },
      { key: 'speak_reply', label: '朗读回复', type: 'bool' },
      { key: 'record_episodes', label: '记录数据', type: 'bool' },
      { key: 'tick_ms', label: '主循环周期 (ms)', type: 'int' },
    ],
  };

  const PRESETS = [
    { name: 'DeepSeek', base: 'https://api.deepseek.com', model: 'deepseek-flash', vision: true },
    { name: '本机 Ollama', base: 'http://127.0.0.1:11434/v1', model: 'qwen2.5vl:7b', vision: true },
    { name: 'OpenAI', base: 'https://api.openai.com/v1', model: 'gpt-4o-mini', vision: true },
    { name: '智谱 GLM', base: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4-flash', vision: false },
    { name: '通义千问', base: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-plus', vision: false },
  ];

  function fieldHtml(f, value) {
    const id = `f-${f.key}`;
    if (f.type === 'bool') {
      const checked = value ? 'checked' : '';
      return `<div class="field"><label for="${id}">${f.label}</label>
        <input type="checkbox" id="${id}" data-key="${f.key}" data-type="bool" ${checked} />
        ${f.hint ? `<small>${f.hint}</small>` : ''}</div>`;
    }
    if (f.type === 'select') {
      const opts = (f.options || []).map((o) =>
        `<option value="${esc(o)}" ${String(value) === String(o) ? 'selected' : ''}>${esc(o)}</option>`).join('');
      return `<div class="field"><label for="${id}">${f.label}</label>
        <select id="${id}" data-key="${f.key}" data-type="str">${opts}</select>
        ${f.hint ? `<small>${f.hint}</small>` : ''}</div>`;
    }
    if (f.type === 'device') {
      const devices = (state.meta?.audio_devices || []).filter((d) => d.kind === 'audio');
      const opts = ['<option value="">自动</option>'].concat(
        devices.map((d) => `<option value="${esc(d.name)}" ${d.name === value ? 'selected' : ''}>${esc(d.name)}</option>`)
      ).join('');
      return `<div class="field"><label for="${id}">${f.label}</label>
        <select id="${id}" data-key="${f.key}" data-type="str">${opts}</select>
        ${f.hint ? `<small>${f.hint}</small>` : ''}</div>`;
    }
    const type = f.type === 'password' ? 'password' : 'text';
    const inputmode = f.type === 'int' || f.type === 'float' ? 'decimal' : 'text';
    return `<div class="field"><label for="${id}">${f.label}</label>
      <input id="${id}" type="${type}" inputmode="${inputmode}" data-key="${f.key}"
        data-type="${f.type || 'str'}" value="${esc(value ?? '')}" />
      ${f.hint ? `<small>${f.hint}</small>` : ''}</div>`;
  }

  function renderTab(tab) {
    const body = $('#tab-body');
    const s = state.settings || {};
    if (tab === 'teach') return renderTeach(body);
    if (tab === 'data') return renderData(body);

    let html = '';
    if (tab === 'brain') {
      html += `<div class="field"><label>一键切换服务商</label>
        <div class="quick">${PRESETS.map((p, i) => `<button class="chip" data-preset="${i}">${p.name}</button>`).join('')}</div></div>`;
    }
    const defs = FIELD_DEFS[tab] || [];
    html += defs.map((f) => fieldHtml(f, s[f.key])).join('');

    if (tab === 'brain') {
      html += `<div class="quick">
        <button class="chip" id="btn-ping">测试连通性</button>
        <button class="chip" id="btn-ping-asr">看识别器状态</button>
      </div><div id="ping-out" class="muted"></div>`;
    }
    body.innerHTML = html;

    body.querySelectorAll('[data-preset]').forEach((btn) => {
      btn.onclick = () => {
        const p = PRESETS[Number(btn.dataset.preset)];
        const base = body.querySelector('[data-key="llm_base_url"]');
        const model = body.querySelector('[data-key="llm_model"]');
        const vision = body.querySelector('[data-key="llm_vision"]');
        if (base) base.value = p.base;
        if (model) model.value = p.model;
        if (vision) vision.checked = p.vision;
        toast(`已填入 ${p.name}，记得保存`);
      };
    });
    const ping = body.querySelector('#btn-ping');
    if (ping) ping.onclick = async () => {
      const out = body.querySelector('#ping-out');
      out.textContent = '测试中…';
      try {
        const r = await api('/api/llm/ping');
        out.textContent = r.ok ? `✓ ${r.ms}ms · ${r.model} · ${r.reply}` : `✗ ${r.error}`;
      } catch (e) { out.textContent = `✗ ${e.message}`; }
    };
  }

  function renderTeach(body) {
    const slots = ['forward', 'backward', 'turn_left', 'turn_right', 'stop'];
    const labels = { forward: '前进', backward: '后退', turn_left: '左转', turn_right: '右转', stop: '停止' };
    body.innerHTML = `
      <div class="field">
        <label>先看这里</label>
        <small>红外（ir）：把遥控器对准接收头，点「学习」后按下对应的键。<br />
        舵机（button）：点「学习」会让一个舵机动一下，你看它压的是哪个键，就把它命名成这个动作。<br />
        学完之后，大模型就能自己"按"这些键了。</small>
      </div>
      <div class="slots" id="slots"></div>
      <div class="field row">
        <div><label for="custom-slot">再学一个自定义键</label>
          <input id="custom-slot" placeholder="比如 ok / menu / 1" /></div>
        <button class="btn ghost" id="btn-learn-custom">学习</button>
      </div>
      <div id="teach-out" class="muted"></div>`;
    refreshSlots(slots, labels);

    body.querySelector('#btn-learn-custom').onclick = async () => {
      const slot = body.querySelector('#custom-slot').value.trim();
      if (!slot) return toast('先填个名字', true);
      await learn(slot);
    };
  }

  async function refreshSlots(slots, labels) {
    const wrap = $('#slots');
    if (!wrap) return;
    let codes = {};
    try {
      const r = await api('/api/ir/codes');
      codes = r.codes || {};
    } catch (_) { /* ignore */ }
    const all = Array.from(new Set([...slots, ...Object.keys(codes)]));
    wrap.innerHTML = all.map((slot) => {
      const rec = codes[slot];
      const desc = rec ? JSON.stringify(rec) : '未学习';
      return `<div class="slot">
        <strong>${esc(labels[slot] || slot)}</strong>
        <code>${esc(desc)}</code>
        <span>
          <button class="btn ghost" data-learn="${esc(slot)}">学习</button>
          ${rec ? `<button class="btn ghost" data-forget="${esc(slot)}">删除</button>` : ''}
        </span>
      </div>`;
    }).join('');
    wrap.querySelectorAll('[data-learn]').forEach((b) => { b.onclick = () => learn(b.dataset.learn); });
    wrap.querySelectorAll('[data-forget]').forEach((b) => {
      b.onclick = async () => {
        await api(`/api/ir/codes/${encodeURIComponent(b.dataset.forget)}`, { method: 'DELETE' });
        refreshSlots(slots, labels);
      };
    });
  }

  async function learn(slot) {
    const out = $('#teach-out');
    if (out) out.textContent = `正在等「${slot}」的信号……（红外：12 秒内按下遥控器）`;
    try {
      const r = await api('/api/ir/learn', { method: 'POST', body: JSON.stringify({ slot }) });
      if (out) out.textContent = r.ok ? `✓ ${slot} 学好了：${JSON.stringify(r.record)}` : `✗ ${r.error || '失败'}`;
      if (r.ok) toast(`${slot} 已绑定`);
      refreshSlots(['forward', 'backward', 'turn_left', 'turn_right', 'stop'], {});
    } catch (e) {
      if (out) out.textContent = `✗ ${e.message}`;
    }
  }

  async function renderData(body) {
    body.innerHTML = '<div class="empty">正在读片段列表…</div>';
    try {
      const r = await api('/api/episodes');
      const rows = (r.episodes || []).map((e) => `
        <div class="slot">
          <strong>${esc(e.id)}</strong>
          <code>${e.steps} 步 · ${e.frames} 帧</code>
          <span><button class="btn ghost" data-open="${esc(e.id)}">看</button></span>
        </div>`).join('') || '<div class="empty">还没有片段。去做点什么，它就会自动记下来。</div>';
      body.innerHTML = `<div class="field"><label>录制片段</label>
        <small>每一段都是「画面 → 决策 → 动作 → 结果」的完整流水，存在 data/episodes/。
        这些就是将来做 VLA 微调的原始数据。</small></div><div class="slots">${rows}</div>
        <div id="ep-out" class="muted"></div>`;
      body.querySelectorAll('[data-open]').forEach((b) => {
        b.onclick = async () => {
          const data = await api(`/api/episodes/${encodeURIComponent(b.dataset.open)}?limit=20`);
          body.querySelector('#ep-out').innerHTML = `<pre style="white-space:pre-wrap;font-size:11px">${
            esc(data.entries.map((e) => `${e.action?.action} ${e.action?.say || ''} ${e.result?.detail || ''}`).join('\n'))
          }</pre>`;
        };
      });
    } catch (e) {
      body.innerHTML = `<div class="empty">⚠️ ${esc(e.message)}</div>`;
    }
  }

  async function openSettings() {
    $('#drawer').hidden = false;
    if (!state.meta) {
      try { state.meta = await api('/api/settings'); } catch (e) { toast(e.message, true); return; }
    }
    state.settings = state.meta.settings;
    renderTab('brain');
  }

  async function saveSettings() {
    const patch = {};
    $$('#tab-body [data-key]').forEach((el) => {
      const key = el.dataset.key;
      const type = el.dataset.type;
      if (type === 'bool') patch[key] = el.checked;
      else if (type === 'int') patch[key] = parseInt(el.value || '0', 10) || 0;
      else if (type === 'float') patch[key] = parseFloat(el.value || '0') || 0;
      else patch[key] = el.value;
    });
    if (!Object.keys(patch).length) { toast('这个页面没有可保存的项'); return; }
    try {
      const r = await api('/api/settings', { method: 'POST', body: JSON.stringify({ settings: patch }) });
      state.settings = r.settings;
      state.meta.settings = r.settings;
      $('#save-hint').textContent = `已保存 ${Object.keys(r.changed || {}).length} 项` +
        (r.rebuilt ? '，传感器/身体已重建' : '');
      toast('设置已生效');
      poll();
    } catch (e) {
      toast(e.message, true);
    }
  }

  // ---------------------------------------------------------------- 事件绑定
  function bind() {
    $('#btn-send').onclick = () => sendText();
    $('#text').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') sendText();
    });
    $('#btn-auto').onclick = toggleAuto;
    $('#btn-estop').onclick = estop;
    $('#btn-settings').onclick = openSettings;
    $('#btn-close-settings').onclick = () => { $('#drawer').hidden = true; };
    $('#btn-save').onclick = saveSettings;
    $('#btn-understand').onclick = understand;
    $('#btn-close-understand').onclick = () => { $('#understand-drawer').hidden = true; };
    $('#chk-speak').onchange = (e) => {
      state.speak = e.target.checked;
      if (!state.speak && 'speechSynthesis' in window) {
        try { window.speechSynthesis.cancel(); } catch (_) { /* ignore */ }
      }
      toast(state.speak ? '会用系统音箱朗读，随时可以再关掉' : '已静音，只显示文字');
    };

    $$('#tabs .tab').forEach((tab) => {
      tab.onclick = () => {
        $$('#tabs .tab').forEach((t) => t.classList.remove('active'));
        tab.classList.add('active');
        renderTab(tab.dataset.tab);
      };
    });

    $$('[data-act]').forEach((b) => {
      b.onclick = () => manual(b.dataset.act === 'stop'
        ? 'stop'
        : b.dataset.act);
    });
    $$('[data-text]').forEach((b) => { b.onclick = () => sendText(b.dataset.text); });

    // 键盘：WASD 手动，空格急停
    document.addEventListener('keydown', (e) => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA') return;
      const map = { w: 'forward', s: 'backward', a: 'turn_left', d: 'turn_right', arrowup: 'forward', arrowdown: 'backward', arrowleft: 'turn_left', arrowright: 'turn_right' };
      if (e.key === ' ') { e.preventDefault(); estop(); return; }
      const act = map[e.key.toLowerCase()];
      if (act) { e.preventDefault(); manual(act); }
    });

    // 语音输入
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    const mic = $('#btn-mic');
    if (!SR) {
      mic.disabled = true;
      mic.title = '这个浏览器不支持语音识别（Chrome / Edge 可以）';
    } else {
      let recog = null;
      let listening = false;
      mic.onclick = () => {
        if (listening) { recog && recog.stop(); return; }
        recog = new SR();
        recog.lang = 'zh-CN';
        recog.interimResults = false;
        recog.maxAlternatives = 1;
        recog.onstart = () => { listening = true; mic.classList.add('active'); toast('在听……'); };
        recog.onend = () => { listening = false; mic.classList.remove('active'); };
        recog.onerror = (ev) => { toast(`语音识别出错：${ev.error}`, true); };
        recog.onresult = (ev) => {
          const text = ev.results[0][0].transcript;
          if (text) sendText(text);
        };
        recog.start();
      };
    }
  }

  // ---------------------------------------------------------------- 启动
  function startStream() {
    const img = $('#stream');
    img.onerror = () => {
      // MJPEG 断流（服务重启 / 网络抖）→ 1 秒后重连，别让画面永远黑着
      setTimeout(() => { img.src = `/api/stream?t=${Date.now()}`; }, 1000);
    };
    img.src = `/api/stream?t=${Date.now()}`;
  }

  bind();
  startStream();
  connect();
  poll();
  setInterval(poll, 2000);
  setTimeout(() => { setPill('mode', '待机', ''); }, 100);
})();
