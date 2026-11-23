const select = (selector) => document.querySelector(selector);
const selectAll = (selector) => document.querySelectorAll(selector);
const formatBytes = (bytes) => {
  if (bytes == null) return '—';
  if (bytes >= 1e12) return (bytes / 1e12).toFixed(2) + ' TB';
  if (bytes >= 1e9) return (bytes / 1e9).toFixed(1) + ' GB';
  return (bytes / 1e6).toFixed(0) + ' MB';
};
// 所有写入都遵循“值没变就不碰 DOM”：采集每 2 秒推一次，无条件赋值会让
// 即使数值相同的文本节点 / 样式属性也重绘一遍，看起来像面板一直在刷新。
function setValue(element, value) {
  if (!element || !element.firstChild) return;
  const next = value == null ? '—' : String(value);
  if (element.firstChild.nodeValue !== next) element.firstChild.nodeValue = next;
}
function setText(element, value) {
  if (!element) return;
  const next = value == null ? '—' : String(value);
  if (element.textContent !== next) element.textContent = next;
}
function setHidden(element, hidden) {
  if (element && element.hidden !== hidden) element.hidden = hidden;
}
function bar(element, value) {
  if (!element) return;
  const next = Math.max(0, Math.min(100, value || 0)) + '%';
  if (element.style.getPropertyValue('--fill') !== next) element.style.setProperty('--fill', next);
}
function dateText(seconds) {
  if (!seconds) return '重置时间不可用';
  return new Date(seconds * 1000).toLocaleString('zh-CN', {month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit'}) + ' 重置';
}
const TITLE_K = 8;
// 标题截取标记：中日韩等全角文字每个算一字，连续西文字母数字算一个词，空格标点不计
const TITLE_TOKENS = new RegExp('[\\u2E80-\\u2EFF\\u3040-\\u30FF\\u3130-\\u318F\\u3400-\\u4DBF\\u4E00-\\u9FFF\\uAC00-\\uD7AF\\uF900-\\uFAFF]|[0-9A-Za-z]+|\\s+|[^\\s]', 'g');
const TITLE_WEIGHTED = new RegExp('^[\\u2E80-\\u2EFF\\u3040-\\u30FF\\u3130-\\u318F\\u3400-\\u4DBF\\u4E00-\\u9FFF\\uAC00-\\uD7AF\\uF900-\\uFAFF0-9A-Za-z]');
function shortTitle(text, k = TITLE_K) {
  let count = 0, out = '';
  for (const token of String(text).match(TITLE_TOKENS) || []) {
    if (count === k && TITLE_WEIGHTED.test(token)) return out.trimEnd() + '…';
    out += token;
    if (TITLE_WEIGHTED.test(token)) count++;
  }
  return out;
}
const SOURCE_LABELS = {codex: 'Codex', claude: 'Claude Code', zcode: 'ZCode', workbuddy: 'WB', scnet: 'SCNet'};
function render(data) {
  const gpu = select('.metric.gpu');
  const cpu = select('.metric.cpu');
  const disk = select('.metric.disk');
  setText(gpu.querySelector('.quiet'), data.gpu && data.gpu[4]);
  setText(cpu.querySelector('.quiet'), data.cpu_model || '—');
  setText(disk.querySelector('.quiet'), data.system_drive || '—');
  if (data.gpu) {
    setValue(gpu.querySelector('.value'), data.gpu[0].toFixed(0));
    setValue(gpu.querySelector('.temp'), data.gpu[1].toFixed(0));
    setText(gpu.querySelector('.meta'), '显存 ' + (data.gpu[2] / 1024).toFixed(1) + ' / ' + (data.gpu[3] / 1024).toFixed(1) + ' GB');
    bar(gpu.querySelector('.track span'), data.gpu[0]);
  } else {
    setValue(gpu.querySelector('.value'), null);
    setValue(gpu.querySelector('.temp'), null);
    setText(gpu.querySelector('.meta'), '显卡数据不可用');
    bar(gpu.querySelector('.track span'), 0);
  }
  setValue(cpu.querySelector('.value'), data.cpu);
  setValue(cpu.querySelector('.temp'), data.cpu_temp);
  setText(cpu.querySelector('.meta'), '内存 ' + formatBytes(data.memory[0]) + ' / ' + formatBytes(data.memory[1]));
  bar(cpu.querySelector('.track span'), data.cpu);
  setValue(disk.querySelector('.value'), (data.disk[0] / 1e9).toFixed(0));
  setValue(disk.querySelector('.temp'), data.disk[2].toFixed(0));
  setText(disk.querySelector('.meta'), '总计 ' + formatBytes(data.disk[1]) + ' · 回收站 ' + formatBytes(data.recycle));
  bar(disk.querySelector('.track span'), data.disk[2]);
  const speeds = selectAll('.network > .speeds .speed');
  for (let i = 0; i < speeds.length; i++) setValue(speeds[i].querySelector('.value'), data.network ? data.network[i].toFixed(1) : null);
  const vpnSpeeds = selectAll('.vpn .speed');
  for (let i = 0; i < 2; i++) {
    setText(speeds[i].querySelector('.day strong'), data.network_total ? formatBytes(data.network_total[i]) : '—');
    setValue(vpnSpeeds[i].querySelector('.value'), data.vpn ? data.vpn[i].toFixed(1) : null);
    setText(vpnSpeeds[i].querySelector('.day strong'), data.vpn_total ? formatBytes(data.vpn_total[i]) : '—');
  }
  const proxy = data.proxy || {};
  setText(select('.vpn .status'), proxy.state || '不可用');
  const tun = select('.tun input');
  setHidden(select('.tun'), typeof proxy.tun !== 'boolean');
  if (!tun.disabled && typeof proxy.tun === 'boolean') tun.checked = proxy.tun;
  setText(select('.egress span:first-child'), proxy.place || '—');
  setText(select('.egress .ip'), proxy.ip || '—');
  const limits = data.codex && ((data.codex.rateLimitsByLimitId || {}).codex || data.codex.rateLimits);
  // 每日预算由后端按周重置时刻分配、持久化，并扣除当天已用量。
  const isPro = !!(limits && String(limits.planType || '').includes('pro'));
  const weeklyWindow = limits && ([limits.primary, limits.secondary].find(w => w && w.windowDurationMins >= 10080) || null);
  const windows = selectAll('.window');
  for (let i = 0; i < windows.length; i++) {
    const windowData = isPro ? weeklyWindow : limits && (i === 0 ? limits.primary : limits.secondary);
    if (isPro && i === 0) {
      const daily = data.daily_budget;
      setText(windows[i].querySelector('.label'), '今日预算剩余');
      if (!daily) {
        setValue(windows[i].querySelector('.value'), null);
        bar(windows[i].querySelector('.track span'), null);
        setText(windows[i].querySelector('.meta'), data.quota_state || '此周期无额度数据');
        continue;
      }
      const remain = daily.remaining;
      if (remain == null) {
        setValue(windows[i].querySelector('.value'), null);
        bar(windows[i].querySelector('.track span'), 0);
        setText(windows[i].querySelector('.meta'), '等待首次更新 · ' + dateText(daily.resetsAt).replace('重置', '更新'));
        continue;
      }
      setValue(windows[i].querySelector('.value'), Math.round(remain * 10) / 10);
      bar(windows[i].querySelector('.track span'), daily.budget > 0 ? remain / daily.budget * 100 : 0);
      setText(windows[i].querySelector('.meta'), data.quota_state ||
        '预算 ' + (Math.round(daily.budget * 10) / 10) + '% · ' + dateText(daily.resetsAt).replace('重置', '更新'));
      continue;
    }
    const remaining = windowData ? Math.max(0, 100 - windowData.usedPercent) : null;
    setValue(windows[i].querySelector('.value'), remaining);
    bar(windows[i].querySelector('.track span'), remaining);
    const reset = windowData ? dateText(windowData.resetsAt) : '此周期无额度数据';
    setText(windows[i].querySelector('.meta'), data.quota_state || reset);
    if (windowData && windowData.windowDurationMins) setText(windows[i].querySelector('.label'), windowData.windowDurationMins >= 10080 ? '每周' : windowData.windowDurationMins / 60 + ' 小时');
  }
  const credits = data.codex && data.codex.rateLimitResetCredits;
  setText(select('.credit-head strong'), credits ? credits.availableCount + ' 张' : '—');
  // 每张可用卡一行：序号 + 到期时间；超过两行时在卡片区域内滚动
  const cardList = select('.cards');
  const available = credits && credits.credits
    ? credits.credits.filter(c => c.status === 'available' && c.expiresAt).sort((a, b) => a.expiresAt - b.expiresAt)
    : [];
  // 卡片内容没变就不碰 DOM（此前每 2 秒无条件 replaceChildren，属于无谓重绘）
  const cardSig = JSON.stringify(available.map(c => [c.expiresAt])) + '|' + (credits ? credits.availableCount : 'x') + '|' + !!credits;
  if (cardSig !== window.__lastCardSig) {
    window.__lastCardSig = cardSig;
  cardList.textContent = '';
  if (available.length) {
    available.forEach((card, index) => {
      const item = document.createElement('li');
      const label = document.createElement('span');
      label.textContent = '#' + (index + 1);
      const expires = document.createElement('em');
      expires.textContent = new Date(card.expiresAt * 1000).toLocaleString('zh-CN',
        {month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit'}) + ' 到期';
      item.append(label, expires);
      cardList.append(item);
    });
  } else {
    const item = document.createElement('li');
    item.className = 'cards-empty';
    item.textContent = credits && credits.availableCount === 0 ? '暂无可用重置卡'
      : credits ? '到期时间不可用' : '等待数据';
    cardList.append(item);
  }
  }  // 关闭“卡片内容变化才重建”的守卫
  const deepseek = data.deepseek || {};
  const dsValue = select('.deepseek .value');
  if (deepseek.state) {
    setValue(dsValue, null);
    setText(dsValue.querySelector('small'), ' CNY');
    setText(select('.deepseek .meta'), deepseek.state);
  } else {
    setValue(dsValue, deepseek.total);
    setText(dsValue.querySelector('small'), ' ' + (deepseek.currency || 'CNY'));
    const granted = Number(deepseek.granted || 0);
    setText(select('.deepseek .meta'), granted > 0 ? '其中赠送 ' + granted.toFixed(2) : '余额全部为充值');
  }
  const list = select('.tasks');
  const scrollPosition = list.scrollTop;
  const NAMES = {idle:'待命', read:'已读', writing:'写作', researching:'研究', executing:'执行', syncing:'同步', error:'异常', interactive:'待回复', unknown:'未确认', interrupted:'已中断'};
  // 按 source:id 复用 <li> 节点、原地更新文字/状态，而非每 2 秒整列销毁重建：
  // 否则 idle/error 行上的 softpulse 呼吸动画会被反复拉回起点（看起来就是不停闪烁），
  // 且处理中会话步骤文字变化时也只更新那一行，不会波及其它行的动画。
  const rows = window.__taskRows || (window.__taskRows = {});
  const desired = new Map();
  if (data.sessions) {
    for (const task of data.sessions) desired.set(task.source + ':' + task.id, task);
  }
  // 移除已不在列表中的行
  for (const key of Object.keys(rows)) {
    if (!desired.has(key)) { rows[key].remove(); delete rows[key]; }
  }
  // 原地更新：值没变就完全不碰 DOM，避免每 2 秒无谓的属性写入与重排。
  for (const [key, task] of desired) {
    let row = rows[key];
    if (!row) {
      row = document.createElement('li');
      row.className = 'task';
      const source = document.createElement('span');
      source.className = 'task-source ' + (SOURCE_LABELS[task.source] ? task.source : 'codex');
      source.textContent = SOURCE_LABELS[task.source] || 'Codex';
      const name = document.createElement('button');
      name.type = 'button';
      name.className = 'task-name';
      const step = document.createElement('span');
      step.className = 'task-step';
      const state = document.createElement('span');
      state.className = 'task-state';
      const read = document.createElement('button');
      read.type = 'button';
      read.className = 'task-read';
      read.addEventListener('click', event => {
        event.stopPropagation();
        if (window.bridge) window.bridge.mark_session_seen(row.dataset.source, row.dataset.id, Number(row.dataset.stamp), ok => {
          if (!ok) alert('会话已更新或无法保存操作，请重试');
        });
      });
      row.append(source, name, step, state, read);
      row.dataset.id = task.id;                    // key = source:id，创建后不会再变
      row.dataset.source = task.source || 'codex';
      row.dataset.name = task.name || '';
      row.addEventListener('click', () => {
        const now = Date.now();
        // 跳转在后台线程执行，外部应用响应可能要一两秒。1s 内的重复点击直接吞掉，
        // 避免用户因「没反应」连点，开出多个窗口 / 连投多条深链把目标应用拖卡。
        if (now - Number(row.dataset.clickAt || 0) < 1000) return;
        row.dataset.clickAt = String(now);
        row.classList.add('opening');
        setTimeout(() => row.classList.remove('opening'), 700);
        if (row.dataset.id && window.bridge) {
          window.bridge.open_session(row.dataset.source || 'codex', row.dataset.id, row.dataset.name || '');
        }
      });
      rows[key] = row;
    }
    const state = task.read ? 'read' : task.status;
    if (row.dataset.state !== state) row.dataset.state = state;
    // 标题可能被后端改写；点击时要用最新的完整标题（Codex 靠标题兜底找会话行）
    if (row.dataset.name !== (task.name || '')) row.dataset.name = task.name || '';
    const isUnread = !!task.unread;
    if (row.dataset.stamp !== String(task.stamp || 0)) row.dataset.stamp = String(task.stamp || 0);
    const canMarkUnread = isUnread && ['error', 'unknown', 'interrupted'].includes(task.status)
      && (task.source !== 'codex' || task.status === 'interrupted');
    const idle = task.status === 'idle';
    const actionButton = row.children[4];
    setHidden(actionButton, !(task.stamp > 0 && (idle || canMarkUnread)));
    const readAction = task.read ? '移除已读条目' : idle ? '移除待命条目' : '标为已读';
    setText(actionButton, idle && !task.read ? '×' : '✓');
    if (actionButton.title !== readAction) actionButton.title = readAction;
    const readLabel = readAction + '：' + task.name;
    if (actionButton.getAttribute('aria-label') !== readLabel) actionButton.setAttribute('aria-label', readLabel);
    if (row.classList.contains('unread') !== isUnread) row.classList.toggle('unread', isUnread);
    const nameEl = row.children[1], stepEl = row.children[2], stateEl = row.children[3];
    const newName = shortTitle(task.name);
    setText(nameEl, newName);
    if (nameEl.title !== task.name) nameEl.title = task.name;
    const newStep = task.step || '';
    if (stepEl.textContent !== newStep) { stepEl.textContent = newStep; stepEl.title = newStep; }
    const newState = NAMES[state] || '待命';
    if (stateEl.textContent !== newState) stateEl.textContent = newState;
  }
  // 仅在成员/顺序真的变化时才整体重排一次，顺序不变时一个节点都不动。
  // 切勿逐行 insertBefore：列表里的空白文本节点会让锚点永远对不上，等于每帧把每行重插一遍。
  const ordered = [];
  for (const key of desired.keys()) ordered.push(rows[key]);
  const current = Array.from(list.children);
  const same = current.length === ordered.length && current.every((el, i) => el === ordered[i]);
  if (!same) {
    list.textContent = '';
    list.append(...ordered);
  }
  list.scrollTop = scrollPosition;
  const unread = data.sessions && data.sessions.filter(task => task.unread).length;
  const running = data.sessions && data.sessions.filter(task => ['writing', 'researching', 'executing', 'syncing'].includes(task.status)).length;
  setText(select('#task-count'), data.sessions
    ? (running ? running + ' 项处理中 · ' : '') + unread + ' 项未查看' : '不可用');
  setHidden(select('.empty'), !!(data.sessions && data.sessions.length));
  setText(select('.empty'), data.sessions ? '暂无处理中或未查看的会话' : '会话状态暂不可用');
}
const dsKeyInput = select('.ds-key');
select('.controls > button').addEventListener('click', event => {
  const menu = select('.controls-menu');
  menu.hidden = !menu.hidden;
  event.currentTarget.setAttribute('aria-expanded', String(!menu.hidden));
  // 展开时把已存的 DeepSeek Key 灌进输入框（无则留空，提示用户填入）
  if (!menu.hidden && window.bridge && typeof window.bridge.get_deepseek_key === 'function') {
    window.bridge.get_deepseek_key(key => { dsKeyInput.value = key || ''; });
  }
});
// 点“⋯”菜单之外的任意区域自动收起菜单：只收一次，点菜单内部或按钮本身不收。
document.addEventListener('click', event => {
  const controls = select('.controls');
  const menu = select('.controls-menu');
  const btn = select('.controls > button');
  if (!menu || menu.hidden) return;
  if (controls && controls.contains(event.target)) return;  // 点在按钮或菜单内不收
  menu.hidden = true;
  if (btn) btn.setAttribute('aria-expanded', 'false');
});
// 左下角刷新按钮：点击即让后端立即重新采集全部数据并推回前端重绘（全局刷新）。
select('.refresh').addEventListener('click', () => {
  const btn = select('.refresh');
  btn.classList.add('spinning');
  setTimeout(() => btn.classList.remove('spinning'), 900);
  if (window.bridge && typeof window.bridge.refresh === 'function') {
    window.bridge.refresh();
  } else {
    // 兜底：QWebChannel 尚未就绪时直接重载页面（同样会触发后端重新采集）
    location.reload();
  }
});
const brightness = select('.brightness input');
const brightnessOutput = select('.brightness output');
// 统一入口：给定亮度值就同步滑块、百分比文字、渐变进度与遮罩透明度（拖动实时改 + 启动恢复都走它）
function applyBrightness(value) {
  const v = Math.max(20, Math.min(100, Number(value) || 68));
  brightness.value = v;
  if (brightnessOutput) brightnessOutput.value = v + '%';
  brightness.style.setProperty('--level', (v - 20) * 1.25 + '%');
  select('.dimmer').style.opacity = (100 - v) / 400;
}
brightness.addEventListener('input', () => applyBrightness(brightness.value));
// 拖动结束（松手）才落盘，避免每拖一格都向后端写一次文件
brightness.addEventListener('change', () => {
  if (window.bridge && typeof window.bridge.set_brightness === 'function') {
    window.bridge.set_brightness(Number(brightness.value), () => {});
  }
});
new QWebChannel(qt.webChannelTransport, channel => {
  const bridge = channel.objects.bridge;
  window.bridge = bridge;
  bridge.update.connect(message => render(JSON.parse(message)));
  // 启动/刷新时恢复上次保存的预览亮度（brightness.json），不要每次都回到 HTML 默认 68
  if (typeof bridge.get_brightness === 'function') {
    bridge.get_brightness(v => { if (v) applyBrightness(v); });
  }
  const tun = select('.tun input');
  let tunPrevious = tun.checked;
  bridge.tun_finished.connect(success => {
    if (!success) { tun.checked = tunPrevious; alert('TUN 设置失败，请检查 Clash'); }
    tun.disabled = false;
  });
  tun.addEventListener('change', () => {
    tunPrevious = !tun.checked;
    tun.disabled = true;
    bridge.request_tun(tun.checked);
  });
  const startup = select('.controls-menu input');
  bridge.startup_enabled(value => { startup.checked = value; });
  startup.addEventListener('change', () => {
    bridge.set_startup(startup.checked, success => {
      if (!success) {
        startup.checked = !startup.checked;
        alert('开机启动设置失败');
      }
    });
  });
  select('.hide-panel').addEventListener('click', () => bridge.hide_panel());
  // “⋯”菜单里填入 DeepSeek Key 后保存：写回后端配置并立即刷新余额显示
  const dsSaveBtn = select('.ds-key-save');
  dsSaveBtn.addEventListener('click', event => {
    event.stopPropagation();
    bridge.set_deepseek_key((select('.ds-key').value || '').trim(), ok => {
      dsSaveBtn.textContent = ok ? '已保存' : '保存失败';
      setTimeout(() => { dsSaveBtn.textContent = '保存'; }, 1200);
    });
  });
});
