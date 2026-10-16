/* 大屏总后台前端：原生 JS，无构建、无外部依赖（路演现场断网也能用） */
(function () {
  'use strict';
  var C = { safe: '#047857', warn: '#F59E0B', warnInk: '#3B2300', warnText: '#92400E', danger: '#C62839',
    dangerDeep: '#8E1B2A', primary: '#5C6BC0', ink: '#3949AB', dim: '#5A64A8', mid: '#C5CAE9' };
  var STATE = {
    NORMAL: { color: C.safe, ink: '#fff', bg: '#fff', text: C.ink, shadow: C.primary, badge: '√ SAFE' },
    WARNING: { color: C.warn, ink: C.warnInk, bg: '#FFF4D6', text: C.warnText, shadow: C.warn, badge: '▲ WARNING' },
    CRITICAL: { color: C.danger, ink: '#fff', bg: '#FFE3E7', text: C.dangerDeep, shadow: C.dangerDeep, badge: '■ CRITICAL' }
  };
  var LEVEL_TAG = { NOTICE: '提醒', WARNING: '▲ 警告', CRITICAL: '■ 紧急', INFO: '信息' };
  var PO_TEXT = { pending: '断电指令已下发，等待下位机确认', acked: '下位机已确认断电（ACK），继电器已断开',
    failed_no_link: '串口未连接 —— 请立即人工切断加热电源', failed_no_ack: '下位机未应答 —— 请立即人工切断加热电源' };

  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); };
  var token = '';
  try { token = localStorage.getItem('labguard_token') || ''; } catch (e) {}
  // 支持 ?token=XXXX 直接登录（小屏设置页给出的链接），读取后从地址栏去掉
  var qs = new URLSearchParams(location.search);
  if (qs.get('token')) {
    token = qs.get('token').trim();
    try { localStorage.setItem('labguard_token', token); } catch (e) {}
    qs.delete('token');
    history.replaceState(null, '', location.pathname + (qs.toString() ? '?' + qs : '') + location.hash);
  }
  // ?still=1：画面改为每秒刷新一张图片（不支持 MJPEG 的浏览器 / 截图用）
  var still = qs.get('still') === '1';
  var imagePoll = null, reconnectTimer = null;
  var snap = null, ws = null, heatHist = [], evCache = [], evFilter = 'all', chatHist = [];

  // ---------------- 通用 ----------------
  function toast(msg) {
    var t = $('toast'); t.textContent = msg; t.classList.remove('hidden');
    clearTimeout(toast._t); toast._t = setTimeout(function () { t.classList.add('hidden'); }, 4200);
  }
  function api(method, path, body) {
    var opt = { method: method, headers: { 'X-Token': token } };
    if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    return fetch(path, opt).then(function (r) {
      if (r.status === 401) { showLogin('口令错误或已变更'); throw new Error('口令错误'); }
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) { var d = j.detail; throw new Error(typeof d === 'string' ? d : JSON.stringify(d || j)); }
        return j;
      });
    });
  }
  function fmt(v, d, unit) { return v == null ? '--' : Number(v).toFixed(d) + (unit || ''); }
  function hms(s) { s = Math.max(0, Math.round(s || 0)); var h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
    return [h, m, x].map(function (n) { return (n < 10 ? '0' : '') + n; }).join(':'); }

  // ---------------- 登录 ----------------
  function showLogin(msg) { $('login').classList.remove('hidden'); $('loginMsg').textContent = msg || ''; }
  $('loginBtn').onclick = function () {
    token = $('tokenInput').value.trim();
    try { localStorage.setItem('labguard_token', token); } catch (e) {}
    $('login').classList.add('hidden'); start();
  };
  $('tokenInput').onkeydown = function (e) { if (e.key === 'Enter') $('loginBtn').click(); };

  // ---------------- 页签 ----------------
  document.querySelectorAll('.tabs [data-tab]').forEach(function (b) {
    b.onclick = function () {
      document.querySelectorAll('.tabs [data-tab]').forEach(function (x) { x.classList.toggle('on', x === b); });
      ['ov', 'params', 'agent', 'wm', 'env', 'review'].forEach(function (t) {
        $('tab-' + t).classList.toggle('hidden', t !== b.dataset.tab); });
      if (b.dataset.tab === 'params') loadParams();
      if (b.dataset.tab === 'agent') loadAgent();
      if (b.dataset.tab === 'review') loadReview();
      if (b.dataset.tab === 'env') loadDemo();
    };
  });

  // ---------------- 实时推送 ----------------
  function start() {
    clearInterval(imagePoll); imagePoll = null;
    clearTimeout(reconnectTimer); reconnectTimer = null;
    if (ws) { var old = ws; ws = null; old.close(); }
    var t = encodeURIComponent(token);
    if (still) {
      var poll = function () {
        $('imgPano').src = '/api/frame/pano.jpg?token=' + t + '&_=' + Date.now();
        $('imgDesk').src = '/api/frame/desk.jpg?token=' + t + '&_=' + Date.now();
      };
      poll(); imagePoll = setInterval(poll, 1000);
    } else {
      $('imgPano').src = '/stream/pano.mjpg?token=' + t;
      $('imgDesk').src = '/stream/desk.mjpg?token=' + t;
    }
    connectWs();
    var tab = (location.hash.match(/tab=(\w+)/) || [])[1];
    var btn = tab && document.querySelector('.tabs [data-tab="' + tab + '"]');
    if (btn) btn.click();
  }
  function connectWs() {
    var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    var socket = new WebSocket(proto + location.host + '/ws?token=' + encodeURIComponent(token));
    ws = socket;
    socket.onmessage = function (m) {
      if (ws !== socket) return;
      try { render(JSON.parse(m.data)); } catch (e) { console.error(e); }
    };
    socket.onclose = function (e) {
      if (ws !== socket) return;
      ws = null;
      if (e.code === 4401) { clearInterval(imagePoll); imagePoll = null; showLogin('口令错误'); return; }
      $('heroState').textContent = '与边缘节点断开，重连中…';
      reconnectTimer = setTimeout(function () { reconnectTimer = null; connectWs(); }, 2000);
    };
  }

  // ---------------- 渲染 ----------------
  function render(s) {
    snap = s;
    var st = STATE[s.state] || STATE.NORMAL, hero = $('hero');
    hero.style.background = st.bg; hero.style.borderColor = st.color; hero.style.boxShadow = '6px 6px 0 ' + st.shadow;
    hero.classList.toggle('blink', s.state === 'CRITICAL');
    var badge = $('heroBadge'); badge.style.background = st.color; badge.style.color = st.ink; badge.textContent = st.badge;
    $('heroState').textContent = s.state_text; $('heroState').style.color = st.text;
    $('heroUptime').textContent = '运行 ' + hms(s.uptime_s);
    var reason;
    if (s.state === 'CRITICAL' && s.latched) reason = (s.latch_reason || '高危事件') + ' · ' + ((s.telemetry.source !== 'hardware' && !s.link.mcu) ? '演示模式：软件已模拟断电锁定' : (PO_TEXT[s.power_off_state] || '断电处理中')) + ' · 恢复供电需现场按复位键';
    else if (s.active_events.length) reason = s.active_events.slice(0, 3).map(function (e) { return e.title; }).join(' · ');
    else reason = (s.vision.person_present ? '人员在岗' : '等待人员') + ' · ' + (s.link.mcu ? '下位机在线' : '下位机未接入（演示数据）') + ' · 无活动告警';
    $('heroReason').textContent = reason; $('heroReason').style.color = st.text;
    $('linkInfo').textContent = s.link.mcu_text + ' · 遥测来源：' + ({ hardware: '实测', ipc: '演示注入', simulated: '自主模拟' }[s.telemetry.source] || '无');

    renderCams(s); renderHeat(s); renderTiles(s); renderChain(s); renderPresence(s); renderLog(s);
    renderEvents(s.recent_events || [], s.stats);
    renderWorld(s); renderEnv(s); renderAgentStatus(s);
  }

  function chip(el, text, fg, bg, border) { el.textContent = text; el.style.color = fg; el.style.background = bg; el.style.borderColor = border || fg; }
  function renderCams(s) {
    var v = s.vision;
    if (!v.ok) chip($('chipPano'), '视觉离线', '#fff', C.danger);
    else if (v.person_present == null) chip($('chipPano'), '画面无效', C.dim, '#fff');
    else if (v.unattended) chip($('chipPano'), '▲ 无人值守', C.warnInk, C.warn);
    else if (v.violations.length) chip($('chipPano'), '■ ' + v.violations[0], '#fff', C.danger);
    else chip($('chipPano'), '√ 人员在岗', C.safe, '#fff');
    var n = v.chem_items.length;
    if (v.falling) chip($('chipDesk'), '▲ 器皿近边 · ' + n + '件', C.warnInk, C.warn);
    else chip($('chipDesk'), n ? '器皿 × ' + n : '台面空闲', n ? C.safe : C.primary, '#fff');
    $('fpsPano').textContent = '● LIVE · ' + (v.fps || '');
    $('mediaLabel').textContent = v.media || '';
  }

  function sparkPath(vals, x0, x1, lo, hi, h, pad) {
    if (!vals.length) return '';
    return vals.map(function (v, i) {
      var x = x0 + (vals.length === 1 ? 0 : i * (x1 - x0) / (vals.length - 1));
      var y = pad + (h - 2 * pad) * (1 - (v - lo) / (hi - lo || 1));
      return x.toFixed(1) + ',' + Math.max(1, Math.min(h - 1, y)).toFixed(1);
    }).join(' ');
  }
  function renderHeat(s) {
    var t = s.telemetry.obj_temp, wm = s.world_model, thr = s.rules.dry_burn_c, card = $('heatCard');
    $('heatThr').textContent = '干烧阈值 ' + thr + '℃';
    $('heatVal').textContent = t == null ? '--.-' : t.toFixed(1);
    heatHist = (wm.history || []).map(function (p) { return p[1]; });
    var lv = 'ok', tag = '√ 常温', eta = '未在加热 · 无干烧风险', etaColor = C.dim;
    if (t == null) { tag = '未接入'; eta = '未接入热源测温（使用环境温度兜底规则）'; }
    else if (t >= thr) { lv = 'bad'; tag = '■ 干烧'; eta = '已超过干烧阈值'; etaColor = C.dangerDeep; }
    else if (t >= s.rules.heating_on_c) {
      lv = wm.eta_s != null && wm.eta_s <= s.rules.predict_eta_warn_s ? 'warn' : 'ok';
      tag = '▲ ' + (wm.phase_text || '加热中');
      if (wm.eta_s != null) { eta = '已越过沸点仍在升温（液体可能已烧干）· 约 ' + Math.round(wm.eta_s) + ' 秒后达到 ' + thr + '℃'; etaColor = lv === 'warn' ? C.warnText : C.ink; }
      else if (wm.eta_boil_s != null) eta = '预测：约 ' + Math.round(wm.eta_boil_s) + ' 秒后沸腾';
      else if (wm.phase === 'boiling') eta = '沸腾已持续 ' + Math.round(wm.plateau_s) + ' 秒 · 风险 ' + Math.round(wm.risk * 100) + '%';
      else if (t >= 90 && t < 104) eta = '接近沸点';
      else if (wm.t_inf != null && wm.t_inf < 90) eta = '按当前趋势将稳定在约 ' + Math.round(wm.t_inf) + '℃';
      else eta = '预测：' + (wm.phase_text || '采样中');
    }
    card.className = 'card heat lv-' + lv; card.style.boxShadow = '6px 6px 0 ' + ({ ok: C.primary, warn: C.warn, bad: C.danger })[lv];
    var tg = $('heatTag'); tg.textContent = tag;
    tg.style.background = { ok: '#DDF7EC', warn: C.warn, bad: C.danger }[lv]; tg.style.color = { ok: C.safe, warn: C.warnInk, bad: '#fff' }[lv];
    if (t == null) { tg.style.background = '#E8EAF6'; tg.style.color = C.ink; }
    $('heatEta').textContent = eta; $('heatEta').style.color = etaColor;
    var fc = (wm.forecast || []).map(function (p) { return p[1]; });
    var all = heatHist.concat(fc).concat([thr]);
    var lo = Math.min.apply(null, all.concat([20])) - 5, hi = Math.max.apply(null, all) + 5;
    var yThr = 3 + 42 * (1 - (thr - lo) / (hi - lo));
    $('heatSpark').innerHTML = '<line x1="0" x2="320" y1="' + yThr + '" y2="' + yThr + '" stroke="' + C.danger + '" stroke-dasharray="4 4"/>' +
      '<polyline fill="none" stroke="' + C.primary + '" stroke-width="2" points="' + sparkPath(heatHist, 0, 230, lo, hi, 48, 3) + '"/>' +
      (fc.length && heatHist.length ? '<polyline fill="none" stroke="' + C.primary + '" stroke-width="2" stroke-dasharray="3 3" points="' + sparkPath(fc, 230, 320, lo, hi, 48, 3) + '"/>' : '');
  }

  function tile(id, val, lv, hint) {
    var el = $(id); el.className = 'tile lv-' + lv;
    el.querySelector('.v').textContent = val; el.querySelector('.hint').textContent = hint;
  }
  function renderTiles(s) {
    var t = s.telemetry, r = s.rules;
    tile('tTemp', fmt(t.temp, 1, '℃'), t.temp > r.ambient_high_c ? 'warn' : 'ok', t.temp > r.ambient_high_c ? '▲ 过高' : '上限 ' + r.ambient_high_c + '℃');
    tile('tHumi', fmt(t.humi, 0, '%'), t.humi > r.humidity_high ? 'warn' : 'ok', t.humi > r.humidity_high ? '▲ 高湿' : '上限 ' + r.humidity_high + '%');
    tile('tSmoke', t.smoke == null ? '--' : t.smoke, t.smoke >= r.smoke_raw ? 'bad' : 'ok', t.smoke == null ? '未接入\n或预热中' : (t.smoke >= r.smoke_raw ? '■ 超标' : '阈值 ' + r.smoke_raw));
  }

  function renderChain(s) {
    var v = s.vision, wm = s.world_model, warn = s.state === 'WARNING', crit = s.state === 'CRITICAL';
    var steps = [
      ['摄像头', 'YOLO11 识别', v.ok ? (v.fps || '运行中') : '视觉离线', v.ok ? 'ok' : 'bad'],
      ['热源', '温度预测', wm.eta_s != null ? '干烧倒计时 ' + Math.round(wm.eta_s) + 's' : (wm.phase_text || '—'), wm.risk > 0.5 ? 'warn' : 'ok'],
      ['告警', '安全等级', crit ? '紧急' : warn ? '警告' : '正常', crit ? 'bad' : warn ? 'warn' : 'ok'],
      ['电源', '加热继电器', s.latched ? '已断电锁定' : (s.link.mcu ? (s.link.relay_on ? '供电中' : '继电器断开') : '未接入'), s.latched ? 'bad' : (s.link.mcu ? 'ok' : 'warn')]
    ];
    $('chainSteps').innerHTML = steps.map(function (x) {
      return '<div class="step lv-' + x[3] + '"><span class="n">' + x[0] + '</span><span>' + x[1] + '</span><span class="s">' + esc(x[2]) + '</span></div>';
    }).join('');
    $('chainLink').textContent = s.link.mcu ? '下位机在线 · 心跳 1s' : '下位机未接入';
  }

  function renderPresence(s) {
    var v = s.vision;
    $('presence').textContent = v.person_present == null ? '画面无效' : (v.unattended ? '无人值守' : '人员在岗');
    var mute = s.mute_until ? Math.max(0, Math.round(s.mute_until - Date.now() / 1000)) : 0;
    $('btnMute').textContent = mute ? '静音中 ' + mute + 's' : '静音语音 60s';
    var icon = { ok: '√', bad: '■', unknown: '?' };
    $('ppeList').innerHTML = v.ppe.map(function (p) {
      var lv = p.status === 'bad' ? 'bad' : (p.status === 'ok' ? 'ok' : 'ok');
      return '<div class="lv-' + lv + '" style="' + (p.status === 'unknown' ? 'color:' + C.dim : '') + '">' + icon[p.status] + ' ' + esc(p.name) + '</div>';
    }).join('');
  }

  function renderLog(s) {
    var colors = { CRITICAL: C.danger, WARNING: C.warnText, NOTICE: C.primary, INFO: C.dim };
    $('logBox').innerHTML = (s.recent_events || []).map(function (e) {
      return '<div><span class="t">' + esc(e.time_str) + '</span><span style="color:' + colors[e.level] + '">[' + (LEVEL_TAG[e.level] || e.level).replace(/[▲■ ]/g, '') + ']</span><span class="m">' + esc(e.title + '：' + e.message) + '</span></div>';
    }).join('') || '<div class="muted">暂无事件</div>';
    $('fpsInfo').textContent = s.vision.fps || '—';
  }

  // ---------------- 事件流 ----------------
  function evCard(e) {
    var img = e.snapshot ? '<img class="thumb" loading="lazy" alt="事件截图" src="/api/events/' + e.uid + '/snapshot.jpg?token=' + encodeURIComponent(token) + '">' : '';
    var note = e.agent_note ? '<div class="note">处理建议：' + esc(e.agent_note) + '</div>' : '';
    var foot;
    if (e.review === 1) foot = '<div class="done" style="color:' + C.safe + '">√ 已复核：判断正确</div>';
    else if (e.review === 0) foot = '<div class="done" style="color:' + C.danger + '">× 已复核：误报</div>';
    else foot = '<div class="row"><button class="btn sm ok" data-rev="1" data-uid="' + e.uid + '">√ 正确</button><button class="btn sm" style="color:' + C.danger + ';border-color:' + C.danger + '" data-rev="0" data-uid="' + e.uid + '">× 误报</button>' +
      (e.agent_note ? '' : '<button class="btn sm" data-ai="' + e.uid + '">生成建议</button>') + '</div>';
    return '<div class="ev ' + e.level + '"><div class="h"><span class="tag">' + (LEVEL_TAG[e.level] || e.level) + '</span><span class="title">' + esc(e.title) +
      '</span><span class="time">' + esc(e.time_str) + '</span></div><div class="msg">' + esc(e.message) + '</div>' + img + note + foot + '</div>';
  }
  function renderEvents(list, st) {
    var key = JSON.stringify(list.map(function (e) { return [e.uid, e.review, !!e.agent_note, !!e.snapshot]; })) + evFilter;
    $('sTotal').textContent = st.total; $('sPending').textContent = st.total - st.reviewed;
    $('sPrec').textContent = st.reviewed ? Math.round(st.correct * 100 / st.reviewed) + '%' : '—';
    if (key === renderEvents._k) return; renderEvents._k = key; evCache = list;
    var shown = list.filter(function (e) {
      return evFilter === 'all' || (evFilter === 'pending' ? e.review == null : e.level === evFilter); });
    $('evList').innerHTML = shown.map(evCard).join('') || '<div class="muted">暂无事件</div>';
  }
  document.addEventListener('click', function (e) {
    var b = e.target.closest('button'); if (!b) return;
    if (b.dataset.rev) { api('POST', '/api/events/' + b.dataset.uid + '/review', { correct: b.dataset.rev === '1' }).then(function () { renderEvents._k = ''; if (!$('tab-review').classList.contains('hidden')) loadReview(); }).catch(function (x) { toast(x.message); }); }
    if (b.dataset.ai) { b.disabled = true; b.textContent = '生成中…'; api('POST', '/api/events/' + b.dataset.ai + '/agent').then(function () { renderEvents._k = ''; }).catch(function (x) { toast(x.message); }); }
    if (b.dataset.f) { evFilter = b.dataset.f; document.querySelectorAll('#filters .btn').forEach(function (x) { x.classList.toggle('on', x === b); }); renderEvents._k = ''; }
  });

  // ---------------- 操作 ----------------
  $('btnSnap').onclick = function () { api('POST', '/api/manual_snapshot').then(function () { toast('已抓拍并记录'); }).catch(function (x) { toast(x.message); }); };
  $('btnMute').onclick = function () { api('POST', '/api/mute', { seconds: 60 }).then(function () { toast('语音提醒已静音（告警与断电不受影响）'); }).catch(function (x) { toast(x.message); }); };
  $('btnCut').onclick = function () {
    if (!confirm('确认立即切断加热电源？\n\n断电后软件无法恢复供电，必须由人员到现场按控制盒复位键。')) return;
    api('POST', '/api/power/cut', { confirm: true }).then(function (r) { toast(r.msg + '\n' + r.note); }).catch(function (x) { toast(x.message); });
  };

  // ---------------- 参数页 ----------------
  function loadParams() {
    api('GET', '/api/config').then(function (c) {
      $('cfgForm').innerHTML = Object.keys(c).map(function (k) {
        var x = c[k];
        return '<label class="f">' + esc(x.label) + '（' + x.min + '–' + x.max + '）<input type="number" step="any" data-k="' + k + '" value="' + esc(x.value) + '"></label>';
      }).join('');
    });
    api('GET', '/api/models').then(function (m) {
      $('modelFile').innerHTML = m.files.map(function (f) { return '<option>' + esc(f) + '</option>'; }).join('');
      $('modelInfo').textContent = Object.keys(m.loaded).map(function (k) { return k + '：' + m.loaded[k].split(/[\\/]/).pop(); }).join(' · ');
      $('tglPpe').classList.toggle('on', m.enable_ppe); $('tglChem').classList.toggle('on', m.enable_chem);
    });
    api('GET', '/api/media').then(function (m) {
      $('mediaFile').innerHTML = m.files.map(function (f) { return '<option>' + esc(f) + '</option>'; }).join('') || '<option value="">（未找到视频）</option>';
    });
  }
  $('cfgSave').onclick = function () {
    var data = {};
    document.querySelectorAll('#cfgForm input').forEach(function (i) { data[i.dataset.k] = Number(i.value); });
    api('PATCH', '/api/config', data).then(function (r) { $('cfgMsg').textContent = '已保存 ' + Object.keys(r.changed).length + ' 项'; })
      .catch(function (x) { $('cfgMsg').textContent = '保存失败：' + x.message; });
  };
  function toggleModel(slot, btn) { var on = !btn.classList.contains('on');
    api('POST', '/api/model', { slot: slot, enabled: on }).then(function () { btn.classList.toggle('on', on); }).catch(function (x) { toast(x.message); }); }
  $('tglPpe').onclick = function () { toggleModel('ppe', $('tglPpe')); };
  $('tglChem').onclick = function () { toggleModel('chem', $('tglChem')); };
  $('modelApply').onclick = function () {
    toast('模型加载中…');
    api('POST', '/api/model', { slot: $('modelSlot').value, name: $('modelFile').value }).then(function (r) { toast(r.msg); loadParams(); }).catch(function (x) { toast(x.message); });
  };
  function cam(body) { return api('POST', '/api/camera', body).then(function (r) { toast('画面源：' + r.media + (r.paused ? '（已暂停）' : '')); }).catch(function (x) { toast(x.message); }); }
  $('srcCam').onclick = function () { cam({ mode: 'camera' }); };
  $('srcPause').onclick = function () { cam({ mode: 'pause' }); };
  $('srcVideo').onclick = function () { cam({ mode: 'video', name: $('mediaFile').value }); };
  $('srcUrl').onclick = function () { cam({ mode: 'url', pano: $('urlPano').value || 0, desk: $('urlDesk').value || 0 }); };

  // ---------------- Agent ----------------
  function loadAgent() {
    api('GET', '/api/agent').then(function (a) {
      $('rUrl').value = a.remote.base_url; $('rModel').value = a.remote.model; $('rVModel').value = a.remote.vision_model;
      $('lUrl').value = a.local.base_url; $('lModel').value = a.local.model; $('lVModel').value = a.local.vision_model;
      $('rKey').placeholder = a.remote.has_key ? '已配置（不回显）' : '留空 = 用环境变量 DASHSCOPE_API_KEY';
      $('autoReview').checked = a.auto_review; $('sendImg').checked = a.send_images_remote;
      markRoute(a.route);
    }).catch(function (x) { $('agentMsg').textContent = x.message; });
  }
  function markRoute(r) { document.querySelectorAll('#routeBtns .btn').forEach(function (b) { b.classList.toggle('on', b.dataset.route === r); }); }
  document.querySelectorAll('#routeBtns .btn').forEach(function (b) { b.onclick = function () { markRoute(b.dataset.route); }; });
  $('agentSave').onclick = function () {
    var on = document.querySelector('#routeBtns .btn.on');
    api('POST', '/api/agent/config', {
      route: on ? on.dataset.route : 'auto', auto_review: $('autoReview').checked, send_images_remote: $('sendImg').checked,
      remote: { base_url: $('rUrl').value, model: $('rModel').value, vision_model: $('rVModel').value, api_key: $('rKey').value },
      local: { base_url: $('lUrl').value, model: $('lModel').value, vision_model: $('lVModel').value }
    }).then(function () { $('rKey').value = ''; $('agentMsg').textContent = '线路设置已保存'; loadAgent(); }).catch(function (x) { $('agentMsg').textContent = x.message; });
  };
  function testRoute(r) {
    $('agentMsg').textContent = '测试中…';
    api('POST', '/api/agent/test', { route: r }).then(function (t) {
      $('agentMsg').textContent = t.ok ? (r === 'remote' ? '远程' : '本地') + '在线 · ' + t.model + ' · ' + t.latency_ms + 'ms' : '不可用：' + t.error;
    }).catch(function (x) { $('agentMsg').textContent = x.message; });
  }
  $('testRemote').onclick = function () { testRoute('remote'); };
  $('testLocal').onclick = function () { testRoute('local'); };
  function bubble(text, me, sub) {
    var d = document.createElement('div'); d.className = 'bubble' + (me ? ' me' : '');
    d.textContent = text; if (sub) { var s = document.createElement('small'); s.textContent = sub; d.appendChild(s); }
    $('chat').appendChild(d); $('chat').scrollTop = 1e9; return d;
  }
  function ask() {
    var q = $('ask').value.trim(); if (!q) return; $('ask').value = '';
    bubble(q, true); var wait = bubble('思考中…');
    api('POST', '/api/agent/ask', { question: q, history: chatHist }).then(function (r) {
      wait.remove(); bubble(r.text, false, r.provider_label + (r.tools && r.tools.length ? ' · 调用工具：' + r.tools.join('、') : ''));
      chatHist.push({ role: 'user', content: q }, { role: 'assistant', content: r.text }); chatHist = chatHist.slice(-8);
    }).catch(function (x) { wait.textContent = '失败：' + x.message; });
  }
  $('askBtn').onclick = ask;
  $('ask').onkeydown = function (e) { if (e.key === 'Enter') ask(); };
  $('reportBtn').onclick = function () {
    var wait = bubble('正在生成实验安全报告…');
    api('POST', '/api/agent/report').then(function (r) { wait.remove(); bubble(r.text, false, '实验安全报告 · ' + r.provider_label); })
      .catch(function (x) { wait.textContent = '失败：' + x.message; });
  };
  function renderAgentStatus(s) {
    var a = s.agent; if (!a) return;
    var names = { auto: '自动', remote: '远程 API', local: '本地主机', off: '仅规则' };
    $('agentRouteNow').textContent = '线路：' + names[a.route] + (a.last_provider ? ' · 上次使用 ' + (a.last_provider === 'remote' ? '远程' : '本地') : '');
    $('agentStat').textContent = '已生成建议 ' + a.reviews_done + ' 条' + (a.busy ? ' · 工作中' : '') + ' · 手册 ' + a.manual_entries + ' 条';
    $('agentLog').innerHTML = (a.log || []).slice().reverse().map(function (l) { return '<div><span class="m">' + esc(l) + '</span></div>'; }).join('') || '<div class="muted">暂无</div>';
  }

  // ---------------- 世界模型 ----------------
  var wmHist = [];
  function renderWorld(s) {
    var wm = s.world_model, t = s.telemetry.obj_temp, thr = s.rules.dry_burn_c;
    wmHist = (wm.history || []).map(function (p) { return p[1]; });
    if (!$('tab-wm').classList.contains('hidden')) {
      var fc = (wm.forecast || []).map(function (p) { return p[1]; });
      var all = wmHist.concat(fc).concat([thr]);
      var lo = Math.min.apply(null, all.concat([20])) - 5, hi = Math.max.apply(null, all) + 5;
      var W = 900, H = 300, split = 600, y = function (v) { return 10 + (H - 20) * (1 - (v - lo) / (hi - lo)); };
      var grid = '';
      for (var g = Math.ceil(lo / 20) * 20; g < hi; g += 20) grid += '<line x1="0" x2="900" y1="' + y(g) + '" y2="' + y(g) + '" stroke="#E8EAF6"/><text x="4" y="' + (y(g) - 3) + '" font-size="11" fill="' + C.dim + '">' + g + '℃</text>';
      $('wmChart').innerHTML = grid + '<line x1="0" x2="900" y1="' + y(thr) + '" y2="' + y(thr) + '" stroke="' + C.danger + '" stroke-dasharray="6 5" stroke-width="2"/>' +
        '<text x="896" y="' + (y(thr) - 5) + '" text-anchor="end" font-size="12" fill="' + C.danger + '">干烧阈值 ' + thr + '℃</text>' +
        '<line x1="' + split + '" x2="' + split + '" y1="0" y2="300" stroke="' + C.mid + '"/><text x="' + (split + 6) + '" y="16" font-size="11" fill="' + C.dim + '">现在 → 未来 120s</text>' +
        '<polyline fill="none" stroke="' + C.primary + '" stroke-width="2.5" points="' + sparkPath(wmHist, 0, split, lo, hi, H, 10) + '"/>' +
        (fc.length && wmHist.length ? '<polyline fill="none" stroke="' + C.primary + '" stroke-width="2.5" stroke-dasharray="6 5" points="' + sparkPath(fc, split, W, lo, hi, H, 10) + '"/>' : '') +
        (t == null ? '<text x="450" y="150" text-anchor="middle" font-size="16" fill="' + C.dim + '">未接入热源测温（可在「环境控制」页注入演示场景）</text>' : '');
      var kv = [['阶段', wm.phase_text], ['当前温度', fmt(wm.temp, 1, '℃')], ['升温速率', fmt(wm.slope, 2, '℃/s')],
        ['渐近温度 T∞', fmt(wm.t_inf, 0, '℃')], ['时间常数 τ', fmt(wm.tau, 0, 's')], ['沸腾 ETA', wm.eta_boil_s == null ? '—' : Math.round(wm.eta_boil_s) + ' 秒'],
        ['干烧 ETA', wm.eta_s == null ? '未越过沸点 / 未加热' : Math.round(wm.eta_s) + ' 秒'],
        ['沸腾平台', Math.round(wm.plateau_s || 0) + ' 秒'], ['模型', wm.model === 'first_order' ? '一阶热平衡' : (wm.model ? '线性外推' : '—')]];
      $('wmKv').innerHTML = kv.map(function (x) { return '<span>' + x[0] + '</span><span>' + esc(x[1]) + '</span>'; }).join('');
      $('wmRisk').style.width = Math.round((wm.risk || 0) * 100) + '%';
      $('wmRisk').style.background = wm.risk > 0.6 ? C.danger : wm.risk > 0.3 ? C.warn : C.primary;
      $('wmRiskTxt').textContent = Math.round((wm.risk || 0) * 100) + '%' + (s.vision.unattended && wm.risk > 0 ? '（无人值守 ×1.5）' : '');
      var j = s.jepa || {};
      $('jepaState').textContent = j.online ? '在线 · ' + (j.model || 'V-JEPA 2') + ' · 异常度 ' + fmt(j.score, 1) : '离线（电脑端未运行 hub/jepa_worker.py）';
      var hs = (j.history || []).map(function (p) { return p[1]; });
      hs = hs.map(function (v) { return Math.max(0, v); });
      var jhi = Math.max.apply(null, hs.concat([j.threshold || 0, 1])) * 1.2;
      $('jepaChart').innerHTML = hs.length ? '<polyline fill="none" stroke="' + C.primary + '" stroke-width="2" points="' + sparkPath(hs, 0, 600, 0, jhi, 160, 8) + '"/>' +
        (j.threshold ? '<line x1="0" x2="600" y1="' + (8 + 144 * (1 - j.threshold / jhi)) + '" y2="' + (8 + 144 * (1 - j.threshold / jhi)) + '" stroke="' + C.danger + '" stroke-dasharray="5 4"/>' : '')
        : '<text x="300" y="85" text-anchor="middle" font-size="14" fill="' + C.dim + '">等待电脑端 V-JEPA 2 上报异常度</text>';
    }
  }

  // ---------------- 环境控制 / 演示 ----------------
  var envLoaded = false;
  function renderEnv(s) {
    var e = s.env; if (!e) return;
    $('envSrc').textContent = (e.source === 'hardware' ? '下位机实测' : '仿真（未接入下位机）') + ' · ' + (e.mode === 'manual' ? '手动 ' + Math.round(e.manual_left_s / 60) + ' 分钟后恢复自动' : '自动');
    $('fanBar').style.width = e.fan_pwm + '%'; $('fanTxt').textContent = e.fan_pwm ? '运行 ' + e.fan_pwm + '%' : '停止';
    $('humTxt').textContent = e.humid_on ? '● 加湿中' : '○ 关闭'; $('humTxt').style.color = e.humid_on ? C.primary : C.dim;
    $('envReason').textContent = '控制依据：' + e.reason;
    if (!envLoaded) { envLoaded = true; $('eT').value = e.t_set; $('eHL').value = e.h_low; $('eHH').value = e.h_high; }
    var d = s.demo; if (d) {
      $('demoState').textContent = d.scenario ? '注入中：' + d.scenario_name + ' · ' + d.elapsed_s + 's' : '未注入';
      $('demoAvail').textContent = d.available ? '当前可以注入演示数据。' : '不可用：' + d.reason;
    }
  }
  $('envSave').onclick = function () {
    api('POST', '/api/env/setpoint', { t_set: Number($('eT').value), h_low: Number($('eHL').value), h_high: Number($('eHH').value) })
      .then(function (r) { $('envMsg').textContent = r.msg; }).catch(function (x) { $('envMsg').textContent = x.message; });
  };
  $('mFan').oninput = function () { $('mFanV').textContent = $('mFan').value + '%'; };
  $('mApply').onclick = function () { api('POST', '/api/env/manual', { fan_pwm: Number($('mFan').value), humid: $('mHum').checked }).then(function (r) { toast(r.msg); }).catch(function (x) { toast(x.message); }); };
  $('mAuto').onclick = function () { api('POST', '/api/env/manual', { fan_pwm: null }).then(function (r) { toast(r.msg); }).catch(function (x) { toast(x.message); }); };
  function loadDemo() {
    api('GET', '/api/demo').then(function (d) {
      $('demoBtns').innerHTML = Object.keys(d.scenarios).map(function (k) { return '<button class="btn" data-sc="' + k + '">' + esc(d.scenarios[k]) + '</button>'; }).join('');
      document.querySelectorAll('#demoBtns .btn').forEach(function (b) {
        b.onclick = function () { api('POST', '/api/demo/start', { scenario: b.dataset.sc }).then(function (r) { toast('开始注入：' + r.scenario_name); }).catch(function (x) { toast(x.message); }); };
      });
    });
  }
  $('demoCustom').onclick = function () {
    var num = function (id) { var v = $(id).value; return v === '' ? null : Number(v); };
    api('POST', '/api/demo/start', { scenario: 'custom', custom: { temp: num('cT'), humi: num('cH'), obj_temp: num('cO'), smoke: num('cS') } })
      .then(function () { toast('已注入自定义数值'); }).catch(function (x) { toast(x.message); });
  };
  $('demoStop').onclick = function () { api('POST', '/api/demo/stop').then(function () { toast('演示注入已停止'); }); };

  // ---------------- 复核页 ----------------
  var revRows = [];
  function loadReview() {
    api('GET', '/api/events?n=300&min_level=NOTICE').then(function (list) {
      revRows = list;
      var done = list.filter(function (e) { return e.review != null; }), ok = done.filter(function (e) { return e.review === 1; });
      $('revSum').textContent = '共 ' + list.length + ' 条 · 已复核 ' + done.length + ' · 准确率 ' + (done.length ? Math.round(ok.length * 100 / done.length) + '%' : '—');
      $('revTable').innerHTML = '<tr><th>时间</th><th>等级</th><th>事件</th><th>处理建议</th><th>截图</th><th>复核</th></tr>' + list.map(function (e) {
        var rev = e.review === 1 ? '<span style="color:' + C.safe + '">√ 正确</span>' : e.review === 0 ? '<span style="color:' + C.danger + '">× 误报</span>' :
          '<button class="btn sm ok" data-rev="1" data-uid="' + e.uid + '">√</button> <button class="btn sm" data-rev="0" data-uid="' + e.uid + '">×</button>';
        return '<tr><td>' + esc(e.time_str) + '</td><td>' + (LEVEL_TAG[e.level] || e.level) + '</td><td>' + esc(e.title) + '<div class="muted">' + esc(e.message) + '</div></td><td style="white-space:pre-line;max-width:320px" class="muted">' +
          esc(e.agent_note || '') + '</td><td>' + (e.snapshot ? '<a target="_blank" rel="noopener" href="/api/events/' + e.uid + '/snapshot.jpg?token=' + encodeURIComponent(token) + '">查看</a>' : '—') + '</td><td>' + rev + '</td></tr>';
      }).join('');
    }).catch(function (x) { toast(x.message); });
  }
  $('revRefresh').onclick = loadReview;
  $('revExport').onclick = function () {
    var head = ['时间', '等级', '事件', '说明', '处理建议', '复核'];
    var q = function (v) { return '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"'; };
    var csv = '﻿' + [head.join(',')].concat(revRows.map(function (e) {
      return [e.time_str, e.level, e.title, e.message, e.agent_note, e.review === 1 ? '正确' : e.review === 0 ? '误报' : ''].map(q).join(','); })).join('\n');
    var a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
    a.download = '事件复核_' + new Date().toISOString().slice(0, 10) + '.csv'; a.click();
  };

  // ---------------- 启动 ----------------
  if (!token) showLogin(); else start();
})();
