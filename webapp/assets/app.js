/* ═══════════════════════════════════════════════════════════
   QQ 群抽奖 · 界面逻辑（无任何外部依赖）
   ═══════════════════════════════════════════════════════════ */
(() => {
  'use strict';

  const TOKEN = '__LOTTERY_TOKEN__';
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const state = {
    config: null,
    stats: null,      // {meta, summary, top}
    draw: null,       // 最近一次开奖结果
    blacklist: [],
    whitelist: [],
    excludeRoles: [],
    blTab: 'blacklist',
    rolling: false,
    bot: { running: false, logged_in: false, bot: null, found: false },
    botPollTimer: null,
    sendDisabled: false,
    groups: [],
  };

  /* ─────────────────────────── 基础工具 ─────────────────────────── */

  async function api(path, { method = 'GET', body = null } = {}) {
    const options = { method, headers: { 'X-Lottery-Token': TOKEN } };
    if (body !== null) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(`/api/${path}`, options);
    } catch (err) {
      throw new Error('无法连接本地服务，请确认程序窗口仍然打开');
    }
    let payload = null;
    try { payload = await res.json(); } catch (_) { /* 忽略非 JSON 响应 */ }
    if (!res.ok || (payload && payload.ok === false)) {
      const err = new Error((payload && payload.message) || `请求失败（HTTP ${res.status}）`);
      err.code = payload && payload.code;
      err.data = payload && payload.data;
      throw err;
    }
    return payload ? payload.data : null;
  }

  function toast(message, kind = 'info', ms = 3200) {
    const el = document.createElement('div');
    el.className = `toast ${kind}`;
    const icon = kind === 'ok' ? '✅' : kind === 'err' ? '⚠️' : 'ℹ️';
    el.innerHTML = `<span>${icon}</span><span></span>`;
    el.lastElementChild.textContent = message;
    $('#toasts').appendChild(el);
    setTimeout(() => {
      el.classList.add('hide');
      setTimeout(() => el.remove(), 220);
    }, ms);
  }

  let busyDepth = 0;
  function busy(on, text = '处理中…') {
    busyDepth = Math.max(0, busyDepth + (on ? 1 : -1));
    const el = $('#busy');
    $('#busy-text').textContent = text;
    el.hidden = busyDepth === 0;
  }

  function status(kind, text) {
    const chip = $('#status-chip');
    chip.className = `chip ${kind || ''}`;
    $('span:last-child', chip).textContent = text;
  }

  function fmtNum(n) {
    if (n === null || n === undefined || Number.isNaN(n)) return '—';
    return Number(n).toLocaleString('zh-CN');
  }

  function initial(name) {
    const text = (name || '?').trim();
    return text ? Array.from(text)[0].toUpperCase() : '?';
  }

  function escapeHtml(text) {
    return String(text ?? '').replace(/[&<>"']/g, (ch) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
    ));
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      const area = document.createElement('textarea');
      area.value = text;
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      const ok = document.execCommand('copy');
      area.remove();
      return ok;
    }
  }

  /* ─────────────────────────── 配置绑定 ─────────────────────────── */

  const els = {};

  function cacheEls() {
    Object.assign(els, {
      sourceSwitch: $('#source-switch'),
      filePath: $('#file-path'),
      fileFormat: $('#file-format'),
      fileHint: $('#file-hint'),
      groupId: $('#group-id'),
      onebotUrl: $('#onebot-url'),
      onebotToken: $('#onebot-token'),
      days: $('#days'),
      daysVal: $('#days-val'),
      daysField: $('#days-field'),
      windowAuto: $('#window-auto'),
      winnersInput: $('#winner-count'),
      minMessages: $('#min-messages'),
      weightCap: $('#weight-cap'),
      capVal: $('#cap-val'),
      weighting: $('#weighting'),
      postToGroup: $('#post-to-group'),
      seed: $('#seed'),
      dataSize: $('#data-size'),
      blacklistCount: $('#blacklist-count'),
      chart: $('#chart'),
      tableBody: $('#table-body'),
      tableFilter: $('#table-filter'),
      summaryMetrics: $('#summary-metrics'),
      summaryNotes: $('#summary-notes'),
      activityMetrics: $('#activity-metrics'),
      activityNotes: $('#activity-notes'),
      activityWindow: $('#activity-window'),
      winners: $('#winners'),
      heroEmpty: $('#hero-empty'),
      heroResult: $('#hero-result'),
      resultMeta: $('#result-meta'),
      summaryCard: $('#summary-card'),
      summarySteps: $('#summary-steps'),
      historyList: $('#history-list'),
      historyPath: $('#history-path'),
      // 机器人面板
      poolSwitch: $('#pool-switch'),
      poolHint: $('#pool-hint'),
      archiveBox: $('#archive-box'),
      archiveTotal: $('#archive-total'),
      archiveSpan: $('#archive-span'),
      archiveInterval: $('#archive-interval'),
      archiveHint: $('#archive-hint'),
      botStatus: $('#bot-status'),
      botTitle: $('#bot-title'),
      botSub: $('#bot-sub'),
      botHint: $('#bot-hint'),
      botGroup: $('#bot-group'),
      botGroupField: $('#bot-group-field'),
      fairnessBanner: $('#fairness-banner'),
      fairnessTitle: $('#fairness-title'),
      fairnessSub: $('#fairness-sub'),
      qrImg: $('#qr-img'),
      qrTip: $('#qr-tip'),
      qrBody: $('#qr-body'),
      qrDone: $('#qr-done'),
      qrDoneTitle: $('#qr-done-title'),
      qrDoneSub: $('#qr-done-sub'),
    });
  }

  function paintRange(input, out, formatter) {
    const min = Number(input.min || 0);
    const max = Number(input.max || 100);
    const value = Number(input.value);
    const pct = max > min ? ((value - min) / (max - min)) * 100 : 0;
    input.style.setProperty('--fill', `${pct}%`);
    if (out) out.textContent = formatter(value);
  }

  function applyConfig(cfg) {
    state.config = cfg;
    state.blacklist = cfg.blacklist || [];
    state.whitelist = cfg.whitelist || [];
    state.excludeRoles = cfg.exclude_roles || [];

    els.sourceSwitch.querySelectorAll('.seg').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.source === cfg.source);
    });
    showSourceGroups(cfg.source);

    els.filePath.value = cfg.file.path || '';
    els.fileFormat.value = cfg.file.format || 'auto';
    els.fileHint.textContent = cfg.data_file_exists
      ? `文件已就绪，约 ${(cfg.data_file_size / 1024).toFixed(0)} KB`
      : '文件不存在，请确认路径（相对路径基于程序目录）';

    els.groupId.value = cfg.group_id || '';
    els.onebotUrl.value = cfg.onebot.base_url || '';
    els.onebotToken.value = cfg.onebot.access_token || '';

    els.days.value = cfg.days;
    paintRange(els.days, els.daysVal, (v) => (v >= 1 ? `${v} 天` : `${(v * 24).toFixed(0)} 小时`));
    els.windowAuto.checked = (cfg.window_mode || 'days') === 'auto';
    els.daysField.hidden = els.windowAuto.checked;   // auto 模式下不需要手动调天数
    applyPool(cfg.pool === 'all_members' ? 'all_members' : 'active');
    els.winnersInput.value = cfg.winners;
    els.minMessages.value = cfg.min_messages;
    els.weightCap.value = cfg.weight_cap;
    paintRange(els.weightCap, els.capVal, (v) => (Number(v) === 0 ? '不限制' : `${v} 条`));
    els.weighting.checked = !!cfg.weighting;
    els.postToGroup.checked = !!cfg.post_to_group;
    els.seed.value = cfg.seed === null || cfg.seed === undefined ? '' : cfg.seed;

    els.blacklistCount.textContent = String(state.blacklist.length);
    renderBlacklist();
    updateDataSize();
    state.sendDisabled = !!cfg.send_disabled;
    updateSendLockNotice();
  }

  function showSourceGroups(source) {
    $$('[data-when]').forEach((group) => {
      group.hidden = group.dataset.when !== source;
    });
  }

  function updateDataSize() {
    if (!state.stats) { els.dataSize.textContent = '未读取'; return; }
    const meta = state.stats.meta || {};
    els.dataSize.textContent = `${fmtNum(meta.count)} 条${meta.cached ? '（缓存）' : ''}`;
  }

  function collectConfig() {
    return {
      source: els.sourceSwitch.querySelector('.seg.active')?.dataset.source || 'file',
      group_id: els.groupId.value.trim(),
      pool: currentPool(),
      days: Number(els.days.value),
      window_mode: els.windowAuto.checked ? 'auto' : 'days',
      winners: Math.max(1, Number(els.winnersInput.value) || 1),
      min_messages: Math.max(1, Number(els.minMessages.value) || 1),
      weight_cap: Math.max(0, Number(els.weightCap.value) || 0),
      weighting: els.weighting.checked,
      post_to_group: els.postToGroup.checked,
      seed: els.seed.value.trim() === '' ? null : Number(els.seed.value),
      blacklist: state.blacklist,
      whitelist: state.whitelist,
      exclude_roles: state.excludeRoles,
      file: { path: els.filePath.value.trim(), format: els.fileFormat.value },
      onebot: {
        base_url: els.onebotUrl.value.trim(),
        access_token: els.onebotToken.value.trim(),
      },
    };
  }

  /* ─────────────────────────── 活跃度视图 ─────────────────────────── */

  function renderFairness(coverage, summary) {
    const box = els.fairnessBanner;
    if (!coverage) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    const complete = !!coverage.complete;
    box.classList.toggle('ok', complete);
    $('.mark', box).textContent = complete ? '✅' : '⚠️';

    if (complete) {
      els.fairnessTitle.textContent = '数据完整：整个统计窗口都有消息';
      els.fairnessSub.textContent =
        `窗口 ${summary.window} 已被消息完整覆盖，候选范围与"有发言就算"的规则一致。` +
        '抽取采用无放回的加权随机，随机种子会记录在历史里，可复现核对。';
    } else {
      const missing = coverage.missing_hours != null ? coverage.missing_hours : '—';
      els.fairnessTitle.textContent = `数据覆盖不足：仅 ${coverage.percent ?? 0}% 的统计窗口有数据`;
      els.fairnessSub.textContent =
        (coverage.warning || '机器人上线时间还不够久，窗口开头的发言没有被采集到。') +
        `（缺口约 ${missing} 小时）抽取本身是公平的，但候选范围偏窄 —— ` +
        '想让"近三天"名副其实，请保持机器人连续在线。';
    }
  }

  function renderSummary(summary) {
    const items = [      { k: '统计窗口', v: summary.days_label, cls: '' },
      { k: '窗口内消息', v: fmtNum(summary.messages_in_window), cls: '' },
      { k: '发过言的人', v: fmtNum(summary.speakers), cls: '' },
      { k: '符合条件', v: fmtNum(summary.candidates), cls: 'accent' },
      { k: '抽奖方式', v: summary.weighting ? '按条数加权' : '每人等概率', cls: '' },
      { k: '中奖人数', v: fmtNum(summary.winners), cls: 'brand' },
    ];
    if (summary.weighting) {
      items.push({ k: '权重上限', v: summary.weight_cap ? `${summary.weight_cap} 条` : '不限制', cls: '' });
    }
    els.summaryMetrics.innerHTML = items
      .map((i) => `<div class="metric ${i.cls}"><div class="k">${i.k}</div><div class="v">${escapeHtml(i.v)}</div></div>`)
      .join('');

    const notes = [];
    // 存档读写异常必须让人看到——静默失败会让人误以为历史一直在攒
    if (summary.archive_note) notes.push({ kind: 'warn', text: summary.archive_note });
    else if (summary.archive_used > 0) {
      notes.push({
        kind: '',
        text: `其中 ${fmtNum(summary.archive_used)} 条来自本地存档（这部分机器人缓存里已经没有了）。`,
      });
    }
    (summary.notes || []).forEach((n) => notes.push({ kind: '', text: n }));
    (summary.excluded || []).forEach((e) => {
      notes.push({
        kind: 'warn',
        text: `已排除「${e.reason}」共 ${e.count} 人：${e.people.join('、')}${e.count > e.people.length ? ' 等' : ''}`,
      });
    });
    els.summaryNotes.innerHTML = notes
      .map((n) => `<div class="note ${n.kind}"><span class="ico">${n.kind === 'warn' ? '🚫' : 'ℹ️'}</span><span>${escapeHtml(n.text)}</span></div>`)
      .join('');

    renderFairness(summary.coverage, summary);
  }

  function renderActivity() {
    const stats = state.stats;
    if (!stats) return;
    const { summary, top, meta } = stats;

    els.activityWindow.textContent = `${summary.window} · ${summary.days_label}`;
    els.activityMetrics.innerHTML = [
      { k: '读取消息', v: fmtNum(summary.messages_total), cls: '' },
      { k: '窗口内消息', v: fmtNum(summary.messages_in_window), cls: '' },
      { k: '候选人数', v: fmtNum(summary.candidates), cls: 'accent' },
      { k: '数据覆盖', v: `${summary.coverage ? summary.coverage.percent : '—'}%`, cls: summary.coverage && summary.coverage.complete ? 'accent' : '' },
      { k: '数据来源', v: summary.source_label, cls: '' },
      { k: '读取时间', v: `${meta.elapsed ?? 0} 秒`, cls: '' },
    ].map((i) => `<div class="metric ${i.cls}"><div class="k">${i.k}</div><div class="v">${escapeHtml(i.v)}</div></div>`).join('');

    const notes = [];
    notes.push({ kind: '', text: `统计窗口：${summary.window}（${summary.window_reference}）` });
    (summary.notes || []).forEach((n) => notes.push({ kind: '', text: n }));
    (summary.excluded || []).forEach((e) => notes.push({
      kind: 'warn',
      text: `已排除「${e.reason}」共 ${e.count} 人：${e.people.join('、')}${e.count > e.people.length ? ' 等' : ''}`,
    }));
    els.activityNotes.innerHTML = notes
      .map((n) => `<div class="note ${n.kind}"><span class="ico">${n.kind === 'warn' ? '🚫' : 'ℹ️'}</span><span>${escapeHtml(n.text)}</span></div>`)
      .join('');

    renderTable(top);
    renderChart(top);
    updateDataSize();
  }

  function renderTable(rows, filter = '') {
    const keyword = filter.trim().toLowerCase();
    const visible = keyword
      ? rows.filter((r) => r.nickname.toLowerCase().includes(keyword) || r.user_id.includes(keyword))
      : rows;

    if (!visible.length) {
      els.tableBody.innerHTML = `<tr><td colspan="6"><div class="empty">${rows.length ? '没有匹配的群友' : '暂无符合条件的群友'}</div></td></tr>`;
      return;
    }
    const maxCount = Math.max(...rows.map((r) => r.count), 1);
    els.tableBody.innerHTML = visible.map((r) => {
      const roleBadge = r.role === 'owner' ? '<span class="badge owner">群主</span>'
        : r.role === 'admin' ? '<span class="badge">管理员</span>' : '';
      const width = Math.round((r.count / maxCount) * 58) + 2;
      return `<tr data-uid="${escapeHtml(r.user_id)}">
        <td class="idx">${r.rank}</td>
        <td>
          <div class="who">
            <span class="avatar">${escapeHtml(initial(r.nickname))}</span>
            <div>
              <div class="nick">${escapeHtml(r.nickname)}${roleBadge}</div>
              <div class="uid">${escapeHtml(r.user_id)}</div>
            </div>
          </div>
        </td>
        <td class="num"><div class="bar-cell"><span class="bar" style="width:${width}px"></span><span>${fmtNum(r.count)}</span></div></td>
        <td class="num">${escapeHtml(r.chance_pct)}</td>
        <td class="num">${escapeHtml(r.last_speak)}</td>
        <td class="act"><span class="row-actions"><button class="link-btn" data-act="exclude">排除</button></span></td>
      </tr>`;
    }).join('');
  }

  function renderChart(rows) {
    const canvas = els.chart;
    const limit = Math.min(rows.length, 15);
    const data = rows.slice(0, limit);
    const cssWidth = canvas.parentElement.clientWidth || 640;
    const rowH = 26;
    const cssHeight = Math.max(70, data.length * rowH + 12);
    const dpr = window.devicePixelRatio || 1;

    canvas.width = Math.round(cssWidth * dpr);
    canvas.height = Math.round(cssHeight * dpr);
    canvas.style.height = `${cssHeight}px`;
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssWidth, cssHeight);

    if (!data.length) {
      ctx.fillStyle = getVar('--text-3');
      ctx.font = '13px "Microsoft YaHei UI", system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('暂无数据', cssWidth / 2, cssHeight / 2);
      return;
    }

    const labelW = Math.min(150, Math.max(80, cssWidth * 0.24));
    const valueW = 96;
    const barMax = Math.max(cssWidth - labelW - valueW - 24, 40);
    const maxCount = Math.max(...data.map((r) => r.count), 1);
    const brand = getVar('--brand');
    const text2 = getVar('--text-2');
    const text3 = getVar('--text-3');

    data.forEach((row, i) => {
      const y = i * rowH + 6;
      const barW = Math.max(2, (row.count / maxCount) * barMax);

      ctx.fillStyle = text2;
      ctx.font = '12.5px "Microsoft YaHei UI", system-ui, sans-serif';
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';
      ctx.fillText(clip(row.nickname, 12), labelW - 12, y + 9);

      const trackX = labelW;
      const grad = ctx.createLinearGradient(trackX, 0, trackX + barW, 0);
      grad.addColorStop(0, brand);
      grad.addColorStop(1, getVar('--accent'));
      ctx.fillStyle = grad;
      roundRect(ctx, trackX, y, barW, 18, 6);
      ctx.fill();

      ctx.fillStyle = text3;
      ctx.textAlign = 'left';
      ctx.font = '12px "Microsoft YaHei UI", system-ui, sans-serif';
      ctx.fillText(`${row.count} 条 · ${row.chance_pct}`, trackX + barW + 10, y + 9);
    });
  }

  function clip(text, max) {
    const chars = Array.from(String(text));
    return chars.length > max ? `${chars.slice(0, max - 1).join('')}…` : String(text);
  }

  function roundRect(ctx, x, y, w, h, r) {
    const radius = Math.min(r, h / 2, w / 2);
    ctx.beginPath();
    ctx.moveTo(x + radius, y);
    ctx.arcTo(x + w, y, x + w, y + h, radius);
    ctx.arcTo(x + w, y + h, x, y + h, radius);
    ctx.arcTo(x, y + h, x, y, radius);
    ctx.arcTo(x, y, x + w, y, radius);
    ctx.closePath();
  }

  function getVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#4f6ef7';
  }

  /* ─────────────────────────── 开奖 ─────────────────────────── */

  function medal(rank) {
    return rank === 1 ? '🥇' : rank === 2 ? '🥈' : rank === 3 ? '🥉' : `${rank}`;
  }

  function winnerHtml(win, { rolling = false, allMembers = false } = {}) {
    const why = rolling
      ? '抽取中…'
      : allMembers
        ? '全体成员等概率<br>中奖概率 ' + escapeHtml(win.probability_pct)
        : `近 <b>${win.count}</b> 条发言<br>中奖概率 ${escapeHtml(win.probability_pct)}`;
    return `<div class="winner ${win.rank === 1 ? 'top1' : ''} ${rolling ? 'rolling' : ''}">
      <span class="medal">${rolling ? '🎲' : medal(win.rank)}</span>
      <div class="who">
        <div class="name">${escapeHtml(win.nickname)}</div>
        <div class="uid">${escapeHtml(win.user_id)}</div>
      </div>
      <div class="why">${why}</div>
    </div>`;
  }

  async function runDraw({ animate = true } = {}) {
    if (state.rolling) return;
    const cfg = collectConfig();

    if (cfg.source === 'onebot' && !cfg.group_id) {
      toast('请先填写群号', 'err');
      return;
    }
    if (cfg.source === 'file' && !cfg.file.path) {
      toast('请先指定聊天记录文件', 'err');
      return;
    }

    state.rolling = true;
    status('busy', '正在开奖');
    $('#btn-draw').disabled = true;
    $('.btn-label', $('#btn-draw')).textContent = '抽奖中…';
    $('.spinner', $('#btn-draw')).hidden = false;

    // 先切换视图并播放滚动动画，让等待过程可见
    showTab('result');
    els.heroEmpty.hidden = true;
    els.heroResult.hidden = false;
    const placeholder = Array.from({ length: Math.min(cfg.winners, 8) }, (_, i) => ({
      rank: i + 1, nickname: '？？？', user_id: '——', count: 0, probability_pct: '—',
    }));
    els.winners.innerHTML = placeholder.map((w) => winnerHtml(w, { rolling: true })).join('');
    $('#result-title').textContent = '正在抽取…';
    els.resultMeta.innerHTML = '';

    const rollTimer = animate
      ? setInterval(() => {
          $$('.winner.rolling .name', els.winners).forEach((el) => {
            const pool = state.stats?.top || [];
            if (pool.length) {
              const pick = pool[Math.floor(Math.random() * pool.length)];
              el.textContent = pick.nickname;
              const uidEl = el.closest('.winner')?.querySelector('.uid');
              if (uidEl) uidEl.textContent = pick.user_id;
            }
          });
        }, 70)
      : null;

    try {
      const started = Date.now();
      const data = await api('draw', { method: 'POST', body: cfg });
      if (rollTimer) {
        const remain = Math.max(0, 900 - (Date.now() - started));
        await new Promise((r) => setTimeout(r, remain));
      }
      state.draw = data;
      renderDraw(data);
      loadHistory();
      if (cfg.post_to_group) await postToGroup(true);
    } catch (err) {
      if (rollTimer) clearInterval(rollTimer);
      els.heroResult.hidden = true;
      els.heroEmpty.hidden = false;
      status('err', '开奖失败');
      toast(err.message, 'err', 5200);
      if (err.data && err.data.summary) {
        renderSummary(err.data.summary);
        els.summaryCard.hidden = false;
      }
    } finally {
      if (rollTimer) clearInterval(rollTimer);
      state.rolling = false;
      $('#btn-draw').disabled = false;
      $('.btn-label', $('#btn-draw')).textContent = '开始抽奖';
      $('.spinner', $('#btn-draw')).hidden = true;
      if (state.draw) status('ok', `已开奖 · ${state.draw.winners.length} 人中奖`);
    }
  }

  function renderDraw(data) {
    els.heroEmpty.hidden = true;
    els.heroResult.hidden = false;
    $('#result-title').textContent = `🎉 ${data.winners.length} 位群友中奖`;
    els.resultMeta.innerHTML = `
      开奖时间 <b>${escapeHtml(data.drawn_at)}</b><br>
      统计窗口 <b>${escapeHtml(data.summary.days_label)}</b> · ${fmtNum(data.summary.candidates)} 人参与<br>
      随机种子 <b>${escapeHtml(String(data.seed))}</b>`;
    els.winners.innerHTML = data.winners.map((w, i) => winnerHtml(w).replace('<div class="winner', `<div style="animation-delay:${i * 70}ms" class="winner`)).join('');

    renderSummary(data.summary);
    els.summaryCard.hidden = false;
    if (data.history_saved_to) {
      const note = document.createElement('div');
      note.className = 'note';
      note.innerHTML = `<span class="ico">💾</span><span>已记入历史：${escapeHtml(data.history_saved_to)}</span>`;
      els.summaryNotes.prepend(note);
    }
  }

  async function postToGroup(silent = false) {
    if (!state.draw) { toast('还没有开奖结果', 'err'); return; }
    try {
      busy(true, '正在发送到群…');
      await api('post', { method: 'POST', body: {} });
      toast('开奖结果已发送到群', 'ok');
    } catch (err) {
      if (!silent) toast(err.message, 'err', 5200);
      else toast(`自动发群失败：${err.message}`, 'err', 5200);
    } finally {
      busy(false);
    }
  }

  /* ─────────────────────────── 导出与海报 ─────────────────────────── */

  async function doExport(kind) {
    if (!state.draw) { toast('还没有开奖结果可导出', 'err'); return; }
    if (kind === 'poster') return showPoster();
    try {
      busy(true, '正在导出…');
      const data = await api('export', { method: 'POST', body: { kind } });
      const link = document.createElement('a');
      link.href = `/api/export/download?name=${encodeURIComponent(data.path)}`;
      link.download = data.file;
      document.body.appendChild(link);
      link.click();
      link.remove();
      toast(`已导出 ${data.file}`, 'ok');
    } catch (err) {
      toast(err.message, 'err');
    } finally {
      busy(false);
    }
  }

  function showPoster() {
    if (!state.draw) { toast('还没有开奖结果', 'err'); return; }
    const canvas = $('#poster-canvas');
    drawPoster(canvas, state.draw);
    $('#modal-poster').hidden = false;
    window.__posterName = `中奖海报-${state.draw.drawn_at.replace(/[:\s-]/g, '')}.png`;
  }

  function drawPoster(canvas, data) {
    const W = 720;
    const pad = 44;
    const winners = data.winners;
    const rowH = 78;
    const headH = 236;
    const footH = 74;
    const H = headH + winners.length * (rowH + 10) + footH;
    const dpr = 2;

    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = `${W}px`;
    canvas.style.height = `${H}px`;
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    // 背景
    const bg = ctx.createLinearGradient(0, 0, W * 0.6, H);
    bg.addColorStop(0, '#1d3350');
    bg.addColorStop(0.55, '#122036');
    bg.addColorStop(1, '#0b1421');
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, W, H);

    // 光晕
    const glow = ctx.createRadialGradient(W * 0.18, 40, 10, W * 0.18, 40, 420);
    glow.addColorStop(0, 'rgba(94,234,212,.22)');
    glow.addColorStop(1, 'rgba(94,234,212,0)');
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, W, H * 0.7);

    // 顶部徽标
    ctx.font = '600 13px "Microsoft YaHei UI", system-ui, sans-serif';
    const badgeText = 'QQ 群抽奖 · 开奖结果';
    const badgeW = ctx.measureText(badgeText).width + 28;
    ctx.fillStyle = 'rgba(94,234,212,.14)';
    roundRect(ctx, pad, pad, badgeW, 30, 15);
    ctx.fill();
    ctx.strokeStyle = 'rgba(94,234,212,.38)';
    ctx.lineWidth = 1;
    roundRect(ctx, pad, pad, badgeW, 30, 15);
    ctx.stroke();
    ctx.fillStyle = '#5eead4';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.fillText(badgeText, pad + 14, pad + 16);

    // 标题
    ctx.fillStyle = '#eef4ff';
    ctx.font = '700 34px "Microsoft YaHei UI", system-ui, sans-serif';
    ctx.fillText('🎉 恭喜以下群友中奖', pad, pad + 76);

    // 说明
    ctx.font = '13.5px "Microsoft YaHei UI", system-ui, sans-serif';
    ctx.fillStyle = '#93a8c6';
    const s = data.summary;
    const lines = [
      `参与范围：近 ${s.days} 天内发过言的群友，共 ${s.candidates} 人参与`,
      `统计窗口：${s.window_start} ~ ${s.window_end}`,
      `${s.weighting ? '按发言条数加权' : '每人等概率'} · 开奖时间 ${data.drawn_at}`,
    ];
    lines.forEach((line, i) => ctx.fillText(line, pad, pad + 112 + i * 24));

    // 中奖行
    let y = headH;
    winners.forEach((win, i) => {
      const isTop = win.rank === 1;
      const box = ctx.createLinearGradient(pad, y, W - pad, y + rowH);
      if (isTop) {
        box.addColorStop(0, 'rgba(251,191,36,.20)');
        box.addColorStop(1, 'rgba(251,191,36,.05)');
      } else {
        box.addColorStop(0, 'rgba(94,234,212,.16)');
        box.addColorStop(1, 'rgba(94,234,212,.04)');
      }
      ctx.fillStyle = box;
      roundRect(ctx, pad, y, W - pad * 2, rowH, 16);
      ctx.fill();
      ctx.strokeStyle = isTop ? 'rgba(251,191,36,.34)' : 'rgba(94,234,212,.24)';
      ctx.lineWidth = 1;
      roundRect(ctx, pad, y, W - pad * 2, rowH, 16);
      ctx.stroke();

      // 名次
      ctx.fillStyle = isTop ? '#fbbf24' : '#5eead4';
      ctx.font = '700 26px "Microsoft YaHei UI", system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(String(win.rank), pad + 36, y + rowH / 2);

      // 昵称
      ctx.textAlign = 'left';
      ctx.fillStyle = '#f2f7ff';
      ctx.font = '600 22px "Microsoft YaHei UI", system-ui, sans-serif';
      ctx.fillText(clip(win.nickname, 14), pad + 74, y + 30);

      // QQ 号
      ctx.fillStyle = '#93a8c6';
      ctx.font = '13px "Microsoft YaHei UI", system-ui, sans-serif';
      ctx.fillText(String(win.user_id), pad + 74, y + 55);

      // 数据
      ctx.textAlign = 'right';
      ctx.fillStyle = '#a7f3d0';
      ctx.font = '13.5px "Microsoft YaHei UI", system-ui, sans-serif';
      ctx.fillText(`${win.count} 条发言`, W - pad - 22, y + 30);
      ctx.fillStyle = '#8ea4c0';
      ctx.fillText(`中奖概率 ${win.probability_pct}`, W - pad - 22, y + 52);

      y += rowH + 10;
    });

    // 页脚
    ctx.strokeStyle = 'rgba(255,255,255,.14)';
    ctx.setLineDash([5, 5]);
    ctx.beginPath();
    ctx.moveTo(pad, y + 8);
    ctx.lineTo(W - pad, y + 8);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.textAlign = 'left';
    ctx.fillStyle = '#8ea4c0';
    ctx.font = '12.5px "Microsoft YaHei UI", system-ui, sans-serif';
    ctx.fillText(`随机种子 ${data.seed}（可复现本次结果）`, pad, y + 38);
    ctx.textAlign = 'right';
    ctx.fillText(`共 ${winners.length} 位中奖者`, W - pad, y + 38);
  }

  /* ─────────────────────────── 历史 ─────────────────────────── */

  async function loadHistory() {
    try {
      const data = await api('history?limit=50');
      els.historyPath.textContent = data.exists ? `${data.path}（共 ${data.total} 次）` : '尚未产生记录';
      if (!data.items.length) {
        els.historyList.innerHTML = '<div class="empty">还没有历史开奖记录</div>';
        return;
      }
      els.historyList.innerHTML = data.items.map((item) => `
        <div class="history-item">
          <div class="when">${escapeHtml(item.drawn_at)}
            <small>${item.source === 'onebot' ? '机器人接口' : '离线记录'} · 近 ${item.days} 天</small>
          </div>
          <div class="names">${(item.winners || []).map((w) => `<span>${escapeHtml(w.nickname)} · ${escapeHtml(w.user_id)}</span>`).join('')}</div>
          <div class="meta">${fmtNum(item.candidate_count)} 人参与<br>种子 ${escapeHtml(String(item.seed))}</div>
        </div>`).join('');
    } catch (err) {
      els.historyList.innerHTML = `<div class="empty">${escapeHtml(err.message)}</div>`;
    }
  }

  /* ─────────────────────────── 排除名单 ─────────────────────────── */

  function renderBlacklist() {
    const isBlack = state.blTab === 'blacklist';
    const list = isBlack ? state.blacklist : state.whitelist;
    const box = $('#bl-chips');
    box.innerHTML = list.length
      ? list.map((uid) => `<span class="chip-item">${escapeHtml(uid)}<button data-uid="${escapeHtml(uid)}" title="移除">✕</button></span>`).join('')
      : `<span class="hint">${isBlack ? '还没有排除任何人' : '白名单为空时表示不限制'}</span>`;
    $('#bl-hint').textContent = isBlack
      ? '黑名单中的 QQ 号永远不会参与抽奖，即使他在窗口内发过言。'
      : '白名单非空时，只有名单内、且窗口内发过言的群友可以参与抽奖。';
    if (isBlack) els.blacklistCount.textContent = String(state.blacklist.length);
  }

  function openBlacklist() {
    $('#modal-blacklist').hidden = false;
    $('#ex-owner').checked = state.excludeRoles.includes('owner');
    $('#ex-admin').checked = state.excludeRoles.includes('admin');
    renderBlacklist();
  }

  function addToBlacklist(uid) {
    const clean = String(uid).trim().replace(/\D/g, '');
    if (!clean) { toast('请输入有效的 QQ 号', 'err'); return; }
    const list = state.blTab === 'blacklist' ? state.blacklist : state.whitelist;
    if (list.includes(clean)) { toast('该 QQ 号已在名单中'); return; }
    list.push(clean);
    renderBlacklist();
  }

  async function loadMembers() {
    try {
      busy(true, '正在读取群成员…');
      const rows = await api('members');
      const box = $('#members-list');
      box.innerHTML = rows.map((m) => `<div class="member" data-uid="${escapeHtml(m.user_id)}">
        <span class="avatar">${escapeHtml(initial(m.nickname))}</span>
        <span>${escapeHtml(m.nickname)}</span>
        ${m.role === 'owner' ? '<span class="badge owner">群主</span>' : m.role === 'admin' ? '<span class="badge">管理员</span>' : ''}
        <span class="uid">${escapeHtml(m.user_id)}</span>
      </div>`).join('');
      $('#members-box').hidden = false;
      toast(`已读取 ${rows.length} 位群成员`, 'ok');
    } catch (err) {
      toast(err.message, 'err', 5000);
    } finally {
      busy(false);
    }
  }

  /* ─────────────────────────── 加载与事件 ─────────────────────────── */

  /* ─────────────────────────── 消息存档 ─────────────────────────── */

  async function refreshArchive({ notify = false } = {}) {
    try {
      const data = await api('archive/status');
      renderArchive(data);
      if (notify) toast('存档状态已刷新', 'ok', 2000);
      return data;
    } catch (err) {
      els.archiveHint.textContent = `读取失败：${err.message}`;
      return null;
    }
  }

  function renderArchive(data) {
    if (!data) return;
    els.archiveTotal.textContent = data.enabled ? `${fmtNum(data.total)} 条` : '未启用';
    els.archiveSpan.textContent = data.total ? data.span_label : '—';
    els.archiveInterval.textContent = data.auto_sync ? `每 ${data.interval_label}` : '仅手动';

    const lines = [];
    if (!data.enabled) {
      lines.push('存档未启用：历史只会保留机器人缓存能覆盖的部分。');
    } else if (!data.total) {
      lines.push('存档还是空的，点「立即同步」开始积累历史。');
    } else {
      lines.push(`已保存 ${fmtNum(data.total)} 条消息，跨度 ${data.span_label}（${data.earliest} ~ ${data.latest}）。`);
      if (data.auto_sync) {
        lines.push(`每 ${data.interval_label}自动同步一次；工具运行期间历史会持续累积，不会被机器人缓存挤掉。`);
      }
      lines.push('注意：只能累积「工具在线期间」的消息，关机期间的空洞补不回来。');
    }
    if (data.last_sync_result) lines.push(data.last_sync_result);
    els.archiveHint.innerHTML = lines.map((t) => escapeHtml(t)).join('<br>');
    els.archiveBox.dataset.state = data.enabled ? (data.total ? 'ok' : 'empty') : 'off';
  }

  async function syncArchive() {
    const btn = $('#btn-archive-sync');
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = '同步中…';
    try {
      busy(true, '正在把机器人消息写入本地存档…');
      const data = await api('archive/sync', { method: 'POST', body: {} });
      toast(data?.message || '同步完成', 'ok', 5000);
      await refreshArchive();
      state.stats = null;
      updateDataSize();
    } catch (err) {
      toast(err.message, 'err', 6000);
      await refreshArchive();
    } finally {
      busy(false);
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  /* ─────────────────────────── 抽奖范围切换 ─────────────────────────── */

  function currentPool() {
    return state.config?.pool === 'all_members' ? 'all_members' : 'active';
  }

  function applyPool(pool) {
    $$('.seg', els.poolSwitch).forEach((b) => b.classList.toggle('active', b.dataset.pool === pool));
    $$('[data-pool-when]').forEach((el) => {
      el.hidden = el.dataset.poolWhen !== pool;
    });
    const isAll = pool === 'all_members';
    $('#btn-all-members').hidden = !isAll;
    $('#btn-draw').hidden = isAll;
    $('.btn-label', $('#btn-run-all')).textContent = isAll ? '一键抽取全群' : '一键开跑';

    if (isAll) {
      els.poolHint.innerHTML =
        '从<strong>群里所有成员</strong>中等概率抽取，<strong>不看发言多少</strong>，潜水的也有机会。' +
        '候选名单来自群成员列表，需要机器人在线。';
    } else {
      els.poolHint.innerHTML =
        '从<strong>统计窗口内发过言的群友</strong>里抽取，默认<strong>话越多概率越高</strong>。' +
        '适合奖励活跃群友。';
    }
    // 天数滑杆的显隐（自动适配开启时隐藏）
    if (!isAll) els.daysField.hidden = els.windowAuto.checked;
  }

  async function drawAllMembers() {
    if (state.rolling) return;
    const cfg = collectConfig();
    if (cfg.source !== 'onebot') {
      toast('全群抽取需要机器人数据源：请先在左侧切到「机器人接口」', 'err', 6000);
      return;
    }
    if (!cfg.group_id) {
      toast('请先选择群号（机器人登录后会自动列出）', 'err');
      return;
    }

    const winners = Math.max(1, Number(els.winnersInput.value) || 1);
    state.rolling = true;
    const btn = $('#btn-all-members');
    btn.disabled = true;
    $('.btn-label', btn).textContent = '抽取中…';
    $('.spinner', btn).hidden = false;
    showTab('result');
    status('busy', '读取群成员');
    els.heroEmpty.hidden = true;
    els.heroResult.hidden = false;
    $('#result-title').textContent = '正在读取群成员…';
    els.resultMeta.innerHTML = '';
    els.winners.innerHTML = '';
    renderSteps([{ name: '读取成员', ok: true, message: '正在从机器人获取群成员列表…' }]);

    try {
      const data = await api('draw-all-members', {
        method: 'POST',
        body: { winners, group_id: cfg.group_id },
      });
      state.draw = data;
      renderAllMembersDraw(data);
      loadHistory();
      status('ok', `已开奖 · ${data.winners.length} 人中奖`);
      toast('开奖完成，尚未发送到群 —— 需要发的时候自己点「发送到群」', 'info', 6000);
    } catch (err) {
      els.heroResult.hidden = true;
      els.heroEmpty.hidden = false;
      status('err', '抽取失败');
      toast(err.message, 'err', 6500);
    } finally {
      state.rolling = false;
      btn.disabled = false;
      $('.btn-label', btn).textContent = '抽取全群成员';
      $('.spinner', btn).hidden = true;
    }
  }

  function renderAllMembersDraw(data) {
    els.heroEmpty.hidden = true;
    els.heroResult.hidden = false;
    $('#result-title').textContent = `🎉 ${data.winners.length} 位群友中奖`;
    const chance = (100 / Math.max(data.candidate_count, 1)).toFixed(3);
    els.resultMeta.innerHTML = `
      开奖时间 <b>${escapeHtml(data.drawn_at)}</b><br>
      范围 <b>全群成员</b> · ${fmtNum(data.candidate_count)} 人参与 · 每人 <b>${chance}%</b><br>
      随机种子 <b>${escapeHtml(String(data.seed))}</b>`;
    els.winners.innerHTML = data.winners
      .map((w, i) => winnerHtml({ ...w, count: 0 }, { allMembers: true })
        .replace('<div class="winner', `<div style="animation-delay:${i * 70}ms" class="winner`))
      .join('');

    els.summaryCard.hidden = false;
    els.summaryMetrics.innerHTML = [
      { k: '抽奖范围', v: '全群成员', cls: 'accent' },
      { k: '群成员总数', v: fmtNum(data.member_total), cls: '' },
      { k: '实际参与', v: fmtNum(data.candidate_count), cls: 'accent' },
      { k: '中奖人数', v: fmtNum(data.winners.length), cls: 'brand' },
      { k: '每人概率', v: `${chance}%`, cls: '' },
      { k: '数据来源', v: '群成员列表', cls: '' },
    ].map((i) => `<div class="metric ${i.cls}"><div class="k">${i.k}</div><div class="v">${escapeHtml(i.v)}</div></div>`).join('');

    const notes = [];
    const ex = data.excluded_summary || {};
    const exText = Object.entries(ex).filter(([, n]) => n > 0).map(([r, n]) => `${r} ${n} 人`).join('、');
    notes.push({ kind: '', text: '候选池来自群成员列表，与发言多少无关；每人权重都是 1，概率完全相等。' });
    if (exText) notes.push({ kind: 'warn', text: `已排除：${exText}` });
    els.summaryNotes.innerHTML = notes
      .map((n) => `<div class="note ${n.kind}"><span class="ico">${n.kind === 'warn' ? '🚫' : 'ℹ️'}</span><span>${escapeHtml(n.text)}</span></div>`)
      .join('');

    els.fairnessBanner.hidden = true;   // 全群模式不涉及时间窗口，无需覆盖警告
    renderSteps([
      { name: '读取成员', ok: true, message: `共 ${data.member_total} 人` },
      { name: '筛选候选', ok: true, message: `${data.candidate_count} 人参与` },
      { name: '开奖', ok: true, message: `${data.winners.length} 位群友中奖` },
    ]);
  }

  /* ─────────────────────────── 机器人管理 ─────────────────────────── */

  async function refreshBotState({ silent = true } = {}) {
    try {
      const previous = { ...state.bot };
      const data = await api('bot/state');
      state.bot = data || {};
      renderBot();
      if (state.bot.logged_in) {
        await loadGroups();
        fillGroupSelect();
        if (!previous.logged_in) {
          // 刚登录成功
          onBotLoggedIn();
        }
      }
      return state.bot;
    } catch (err) {
      renderBot(err);
      if (!silent) toast(err.message, 'err', 5000);
      return null;
    }
  }

  function renderBot(error = null) {
    const bot = state.bot || {};
    const box = els.botStatus;
    const title = els.botTitle;
    const sub = els.botSub;
    box.classList.remove('ok', 'warn', 'err');

    if (error) {
      box.classList.add('warn');
      title.textContent = '无法读取机器人状态';
      sub.textContent = error.message;
      els.botHint.textContent = '本地服务可能已停止，重新打开程序窗口即可。';
      return;
    }

    if (!bot.found) {
      box.classList.add('err');
      title.textContent = '未找到 NapCat';
      sub.textContent = '请先按 docs\\NapCat配置指南.md 安装';
      els.botHint.textContent = '装好后点「刷新」重新检测。';
      return;
    }
    if (!bot.running) {
      box.classList.add('warn');
      title.textContent = '机器人未运行';
      sub.textContent = '点「启动机器人」即可，会自动登录';
      els.botHint.textContent = bot.autostart_configured
        ? '已配置开机自启，正常情况下开机就会自动运行。'
        : '还没配置开机自启，可在 docs\\NapCat配置指南.md 的 8.1 节开启。';
      return;
    }
    if (bot.logged_in && bot.bot) {
      box.classList.add('ok');
      title.textContent = `已登录：${bot.bot.nickname}`;
      sub.textContent = `QQ ${bot.bot.user_id} · NapCat 运行中`;
      els.botHint.textContent = '机器人已就绪，可以直接开抽。';
      return;
    }
    box.classList.add('warn');
    title.textContent = 'NapCat 在运行，但未登录';
    sub.textContent = bot.qr_available ? '二维码已生成，点「扫码登录」' : '稍等几秒生成二维码后点「扫码登录」';
    els.botHint.textContent = '首次使用需要扫码；登录成功一次之后就会自动登录。';
  }

  async function loadGroups() {
    if (state.groups.length) return state.groups;
    try {
      const groups = await api('bot/groups');
      state.groups = Array.isArray(groups) ? groups : [];
    } catch (_) {
      state.groups = [];
    }
    return state.groups;
  }

  function fillGroupSelect() {
    const groups = state.groups;
    const field = els.botGroupField;
    if (!groups.length) {
      field.hidden = true;
      return;
    }
    field.hidden = false;
    const current = (els.groupId.value || '').trim();
    els.botGroup.innerHTML = groups
      .map((g) => `<option value="${escapeHtml(g.group_id)}">${escapeHtml(g.group_name)}（${g.member_count} 人）</option>`)
      .join('');
    // 优先保留用户已填的群号；否则把人最多的群带上
    const match = groups.find((g) => g.group_id === current);
    els.botGroup.value = match ? match.group_id : groups[0].group_id;
  }

  async function startBot() {
    try {
      busy(true, '正在启动机器人…');
      const data = await api('bot/start', { method: 'POST', body: {} });
      toast(data?.message || '机器人已启动', 'ok', 4000);
    } catch (err) {
      toast(err.message, 'err', 6000);
    } finally {
      busy(false);
      await refreshBotState({ silent: false });
    }
  }

  async function openQrLogin() {
    $('#modal-qr').hidden = false;
    els.qrImg.removeAttribute('src');
    els.qrBody.hidden = false;
    els.qrDone.hidden = true;
    await refreshQr({ load: true });
    state.qrPolling = true;
  }

  async function refreshQr({ load = false } = {}) {
    const bot = state.bot || {};
    if (bot.logged_in) {
      showQrDone(bot.bot);
      return;
    }
    if (bot.qr_image) {
      els.qrImg.src = bot.qr_image;
      els.qrTip.textContent = '用手机 QQ 扫码并确认登录。二维码有效期约 2~5 分钟，过期可点「换一张二维码」。';
      return;
    }
    if (!load) return;
    try {
      const data = await api('bot/qr');
      els.qrImg.src = data.image;
      els.qrTip.textContent = '用手机 QQ 扫码并确认登录。二维码有效期约 2~5 分钟，过期可点「换一张二维码」。';
    } catch (err) {
      els.qrTip.textContent = err.message;
    }
  }

  function showQrDone(bot) {
    els.qrBody.hidden = true;
    els.qrDone.hidden = false;
    els.qrDoneTitle.textContent = bot ? `登录成功：${bot.nickname}` : '登录成功';
    els.qrDoneSub.textContent = bot ? `QQ ${bot.user_id}` : '';
  }

  function onBotLoggedIn() {
    if (state.botNotified) return;
    state.botNotified = true;
    toast('机器人已登录，可以开抽了', 'ok', 4200);
    if (!$('#modal-qr').hidden) showQrDone(state.bot.bot);
  }

  function closeQrModal() {
    $('#modal-qr').hidden = true;
    state.qrPolling = false;
    state.botNotified = false;
  }

  function startBotPolling() {
    // 轮询要一直跑：一来 QR 登录状态需要刷新，
    // 二来后端会在 bot/state 里按间隔触发「自动存档」，停了就不攒历史了。
    if (state.botPollTimer) return;
    let tick = 0;
    state.botPollTimer = setInterval(async () => {
      tick += 1;
      await refreshBotState();
      if (state.qrPolling && !$('#modal-qr').hidden) await refreshQr();
      if (tick % 10 === 0) refreshArchive();
    }, 3000);
  }

  function renderSteps(steps) {
    if (!steps || !steps.length) {
      els.summarySteps.hidden = true;
      els.summarySteps.innerHTML = '';
      return;
    }
    els.summarySteps.hidden = false;
    els.summarySteps.innerHTML = steps
      .map((s) => `<div class="step ${s.ok ? '' : 'fail'}">
        <span class="mark">${s.ok ? '✓' : '✕'}</span>
        <span class="name">${escapeHtml(s.name)}</span>
        <span class="desc">${escapeHtml(s.message)}</span>
      </div>`)
      .join('');
  }

  function updateSendLockNotice() {
    const btn = $('#btn-post');
    if (!btn) return;
    if (state.sendDisabled) {
      btn.disabled = true;
      btn.title = '当前环境禁止发送消息（安全锁已开启）';
    } else {
      btn.disabled = false;
      btn.title = '把开奖结果发送到群里';
    }
  }

  async function runAll() {
    if (state.rolling) return;
    const cfg = collectConfig();
    if (cfg.source !== 'onebot') {
      const go = confirm('当前数据源不是机器人接口。\n\n一键开跑需要机器人接口。是否切换为机器人接口并继续？');
      if (!go) return;
      state.config.source = 'onebot';
      document.querySelectorAll('#source-switch .seg').forEach((b) => {
        b.classList.toggle('active', b.dataset.source === 'onebot');
      });
      showSourceGroups('onebot');
    }
    const body = {
      group_id: (els.botGroup.value || els.groupId.value || '').trim(),
      winners: Number(els.winnersInput.value) || 1,
      days: Number(els.days.value),
      window_mode: els.windowAuto.checked ? 'auto' : 'days',
      pool: currentPool(),
    };

    state.rolling = true;
    $('#btn-run-all').disabled = true;
    $('.btn-label', $('#btn-run-all')).textContent = '执行中…';
    $('.spinner', $('#btn-run-all')).hidden = false;
    showTab('result');
    status('busy', '一键开跑');
    els.heroEmpty.hidden = true;
    els.heroResult.hidden = false;
    $('#result-title').textContent = '正在准备…';
    els.resultMeta.innerHTML = '';
    els.winners.innerHTML = '';
    renderSteps([{ name: '开始', ok: true, message: '正在连接机器人、读取群消息…' }]);

    try {
      const data = await api('bot/run-all', { method: 'POST', body });
      state.draw = data;
      renderDraw(data);
      if (data.summary) {
        els.summaryCard.hidden = false;
        renderSteps(data.steps);
      }
      if (data.group_id) {
        els.groupId.value = data.group_id;
        const cfgNow = await api('config');
        applyConfig(cfgNow);
      }
      state.stats = null;
      await loadStats();
      loadHistory();
      status('ok', `已开奖 · ${data.winners.length} 人中奖`);
      if (!data.posted) {
        toast('开奖完成，尚未发送到群 —— 需要发的时候自己点「发送到群」', 'info', 6000);
      }
    } catch (err) {
      els.heroResult.hidden = true;
      els.heroEmpty.hidden = false;
      status('err', '一键开跑失败');
      toast(err.message, 'err', 6500);
      if (err.data && err.data.steps) {
        showTab('result');
        els.summaryCard.hidden = false;
        renderSteps(err.data.steps);
      }
    } finally {
      state.rolling = false;
      $('#btn-run-all').disabled = false;
      $('.btn-label', $('#btn-run-all')).textContent = '一键开跑';
      $('.spinner', $('#btn-run-all')).hidden = true;
    }
  }

  async function loadConfig() {
    const cfg = await api('config');
    applyConfig(cfg);
    return cfg;
  }

  async function loadStats(reload = false) {
    busy(true, reload ? '正在重新读取消息…' : '正在统计活跃度…');
    status('busy', '读取数据');
    try {
      state.stats = await api('stats', { method: 'POST', body: { reload } });
      renderActivity();
      status('ok', `${fmtNum(state.stats.summary.candidates)} 位候选群友`);
      return true;
    } catch (err) {
      status('err', '数据读取失败');
      toast(err.message, 'err', 5200);
      return false;
    } finally {
      busy(false);
    }
  }

  function showTab(name) {
    $$('.tab[data-tab]').forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
    $$('.panel').forEach((p) => p.classList.toggle('active', p.dataset.panel === name));
    if (name === 'history') loadHistory();
    if (name === 'activity' && state.stats) renderChart(state.stats.top);
  }

  async function saveConfig() {
    try {
      busy(true, '正在保存设置…');
      const cfg = collectConfig();
      const fresh = await api('config/save', { method: 'POST', body: cfg });
      applyConfig({ ...state.config, ...cfg, data_file_exists: state.config.data_file_exists, data_file_size: state.config.data_file_size });
      toast('设置已保存到 config.json', 'ok');
      // 设置变了，缓存的统计数据不再准确
      state.stats = null;
      updateDataSize();
      return fresh;
    } catch (err) {
      toast(err.message, 'err', 5200);
      return null;
    } finally {
      busy(false);
    }
  }

  function bindEvents() {
    // 抽奖范围切换
    els.poolSwitch.addEventListener('click', async (e) => {
      const btn = e.target.closest('.seg');
      if (!btn) return;
      const pool = btn.dataset.pool;
      if (state.config) state.config.pool = pool;
      applyPool(pool);
      try {
        await api('config/save', { method: 'POST', body: { pool } });
        toast(pool === 'all_members'
          ? '已切换为「全群成员」：每人等概率，不看发言多少'
          : '已切换为「活跃群友」：按发言条数加权', 'info', 4000);
      } catch (err) {
        toast(`保存失败：${err.message}`, 'err');
      }
    });
    $('#btn-all-members').addEventListener('click', drawAllMembers);

    // 消息存档
    $('#btn-archive-sync').addEventListener('click', syncArchive);
    $('#btn-archive-open').addEventListener('click', async () => {
      const data = await refreshArchive();
      if (!data) return;
      toast(
        data.exists ? `存档位置：${data.path}（${data.size_label}）` : '存档还没生成，先点「立即同步」',
        'info',
        6000,
      );
    });

    // 机器人面板
    $('#btn-bot-start').addEventListener('click', startBot);
    $('#btn-bot-login').addEventListener('click', openQrLogin);
    $('#btn-bot-refresh').addEventListener('click', async () => {
      state.groups = [];
      await refreshBotState({ silent: false });
      toast('状态已刷新', 'ok', 2000);
    });
    els.botGroup.addEventListener('change', () => {
      els.groupId.value = els.botGroup.value;
    });
    $('#btn-qr-refresh').addEventListener('click', async () => {
      await refreshBotState();
      await refreshQr({ load: true });
      toast('二维码已刷新，请重新扫码', 'info');
    });
    $('#btn-qr-finish').addEventListener('click', closeQrModal);
    $('#btn-run-all').addEventListener('click', runAll);

    // 来源切换
    els.sourceSwitch.addEventListener('click', (e) => {
      const btn = e.target.closest('.seg');
      if (!btn) return;
      $$('.seg', els.sourceSwitch).forEach((b) => b.classList.toggle('active', b === btn));
      showSourceGroups(btn.dataset.source);
    });

    // 滑杆
    els.days.addEventListener('input', () => paintRange(els.days, els.daysVal, (v) => (v >= 1 ? `${v} 天` : `${(v * 24).toFixed(0)} 小时`)));
    els.windowAuto.addEventListener('change', () => {
      els.daysField.hidden = els.windowAuto.checked;
      if (els.windowAuto.checked) {
        toast('已切换为「自动适配窗口」：以缓存能完整覆盖的时段为准，保证数据完整', 'info', 5200);
      } else {
        toast('已切换为「指定天数」：窗口可能超出缓存范围，请留意覆盖提示', 'info', 5200);
      }
    });
    els.weightCap.addEventListener('input', () => paintRange(els.weightCap, els.capVal, (v) => (Number(v) === 0 ? '不限制' : `${v} 条`)));

    // 标签页
    $$('.tab[data-tab]').forEach((tab) => tab.addEventListener('click', () => showTab(tab.dataset.tab)));

    // 主按钮
    $('#btn-draw').addEventListener('click', () => runDraw());
    $('#btn-hero-draw').addEventListener('click', () => runDraw());
    $('#btn-refresh').addEventListener('click', () => loadStats(true));
    $('#btn-save').addEventListener('click', saveConfig);
    $('#btn-hero-stats').addEventListener('click', async () => {
      showTab('activity');
      if (!state.stats) await loadStats();
    });
    $('#btn-test').addEventListener('click', async () => {
      try {
        busy(true, '正在检测连接…');
        const data = await api('config/test', { method: 'POST', body: {} });
        toast(data?.message || '连接正常', 'ok');
      } catch (err) {
        toast(err.message, 'err', 5200);
      } finally {
        busy(false);
      }
    });
    $('#btn-members').addEventListener('click', loadMembers);
    $('#btn-blacklist').addEventListener('click', openBlacklist);
    $('#btn-seed-rand').addEventListener('click', () => { els.seed.value = ''; });
    $('#theme-toggle').addEventListener('click', () => {
      const now = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = now;
      localStorage.setItem('lottery-theme', now);
      if (state.stats) renderChart(state.stats.top);
    });

    // 导出与文案
    $('#btn-export-csv').addEventListener('click', () => doExport('csv'));
    $('#btn-export-json').addEventListener('click', () => doExport('json'));
    $('#btn-export-poster').addEventListener('click', () => doExport('poster'));
    $('#btn-post').addEventListener('click', () => postToGroup());
    $('#btn-copy').addEventListener('click', async () => {
      if (!state.draw) { toast('还没有开奖结果', 'err'); return; }
      const ok = await copyText(state.draw.group_message);
      toast(ok ? '开奖文案已复制，可直接粘贴到群里' : '复制失败，请手动选择文本', ok ? 'ok' : 'err');
    });
    $('#btn-poster-download').addEventListener('click', () => {
      const canvas = $('#poster-canvas');
      canvas.toBlob((blob) => {
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = window.__posterName || '中奖海报.png';
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 2000);
        toast('海报已保存', 'ok');
      }, 'image/png');
    });

    // 表格：搜索与排除
    els.tableFilter.addEventListener('input', () => {
      if (state.stats) renderTable(state.stats.top, els.tableFilter.value);
    });
    els.tableBody.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-act="exclude"]');
      if (!btn) return;
      const uid = btn.closest('tr')?.dataset.uid;
      if (uid) {
        state.blTab = 'blacklist';
        addToBlacklist(uid);
        toast(`已把 ${uid} 加入黑名单（记得点保存名单）`, 'info', 4200);
        openBlacklist();
      }
    });

    // 排除名单弹窗
    $$('[data-close]').forEach((btn) => btn.addEventListener('click', () => {
      const modal = btn.closest('.modal');
      if (modal.id === 'modal-qr') closeQrModal(); else modal.hidden = true;
    }));
    $$('.modal').forEach((modal) => modal.addEventListener('click', (e) => {
      if (e.target !== modal) return;
      if (modal.id === 'modal-qr') closeQrModal(); else modal.hidden = true;
    }));
    $$('[data-bl-tab]').forEach((tab) => tab.addEventListener('click', () => {
      state.blTab = tab.dataset.blTab;
      $$('[data-bl-tab]').forEach((t) => t.classList.toggle('active', t === tab));
      renderBlacklist();
    }));
    $('#bl-chips').addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-uid]');
      if (!btn) return;
      const list = state.blTab === 'blacklist' ? state.blacklist : state.whitelist;
      const idx = list.indexOf(btn.dataset.uid);
      if (idx >= 0) list.splice(idx, 1);
      renderBlacklist();
    });
    $('#bl-add').addEventListener('click', () => addToBlacklist($('#bl-input').value));
    $('#bl-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); addToBlacklist($('#bl-input').value); $('#bl-input').value = ''; }
    });
    $('#members-list').addEventListener('click', (e) => {
      const row = e.target.closest('.member');
      if (!row) return;
      state.blTab = 'blacklist';
      $$('[data-bl-tab]').forEach((t) => t.classList.toggle('active', t.dataset.blTab === 'blacklist'));
      addToBlacklist(row.dataset.uid);
      renderBlacklist();
    });
    $('#bl-save').addEventListener('click', async () => {
      const roles = [];
      if ($('#ex-owner').checked) roles.push('owner');
      if ($('#ex-admin').checked) roles.push('admin');
      state.excludeRoles = roles;
      $('#modal-blacklist').hidden = true;
      await saveConfig();
    });

    // 键盘：空格 = 开奖（不在输入框里时）
    document.addEventListener('keydown', (e) => {
      const tag = (e.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'select' || tag === 'textarea') return;
      if (e.code === 'Space') { e.preventDefault(); runDraw(); }
      if (e.key === 'Escape') $$('.modal').forEach((m) => { m.hidden = true; });
    });

    window.addEventListener('resize', () => {
      if (state.stats) renderChart(state.stats.top);
    });
  }

  async function init() {
    cacheEls();
    const savedTheme = localStorage.getItem('lottery-theme');
    if (savedTheme) document.documentElement.dataset.theme = savedTheme;
    bindEvents();
    try {
      await loadConfig();
      status('ok', '就绪');
    } catch (err) {
      status('err', '配置读取失败');
      toast(err.message, 'err', 6000);
    }
    loadHistory();

    // 机器人状态：先查一次，再按需轮询（未登录或扫码弹窗打开时）
    await refreshBotState();
    startBotPolling();
    refreshArchive();

    // 支持 #tab=activity / #auto 直接进入某个视图（也方便截图与自动化）
    const hash = location.hash.replace(/^#/, '');
    const params = new URLSearchParams(hash);
    const wanted = params.get('tab');
    const theme = params.get('theme');
    if (theme === 'dark' || theme === 'light') {
      document.documentElement.dataset.theme = theme;
      localStorage.setItem('lottery-theme', theme);
    }
    if (wanted === 'activity' || wanted === 'history') {
      showTab(wanted);
      if (wanted === 'activity') await loadStats();
    }
    if (params.has('auto') || hash === 'auto') {
      await loadStats();
    }
    // #draw=3 打开即开奖（用于演示与自动化截图）
    const autoDraw = Number(params.get('draw') || 0);
    if (autoDraw > 0) {
      els.winnersInput.value = String(autoDraw);
      if (els.seed.value.trim() === '') els.seed.value = '2024';
      await runDraw({ animate: false });
    }
    const modal = params.get('modal');
    if (modal === 'blacklist') openBlacklist();
    if (modal === 'poster' && state.draw) showPoster();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
