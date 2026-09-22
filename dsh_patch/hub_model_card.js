/* HUB Model Card — injected into dsh frontend by F:\llm_hub\dsh_patch\patch_dsh.py
   Mounts a model-switch button next to the "标准模式" dropdown inside the dsh panel.
   Talks to the local LLM Key Hub (127.0.0.1:8787) pin API. No external deps. */
(function () {
  if (window.__hubModelCard) { return; }
  window.__hubModelCard = true;

  var HUB = 'https://hub.zeroxcore.tech';
  var HUB_HEADERS = { Authorization: 'Basic ' + btoa('hubadmin:f844c52d260ac865') };
  var FALLBACK_AFTER = (typeof window.__hubMcFallbackAfter === 'number') ? window.__hubMcFallbackAfter : 20;
  var state = { model: 'auto', options: [], loaded: false };
  var root = null, btn = null, labelEl = null, panel = null, toastTimer = null;
  var failCount = 0, fallbackMounted = false;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text != null) { n.textContent = text; }
    return n;
  }

  var CSS = ''
    + '.hub-mc-root{position:relative;display:inline-block;margin-left:6px;vertical-align:middle;z-index:60}'
    + '.hub-mc-root.hub-mc-fallback{position:fixed;left:50%;top:118px;transform:translateX(-50%);z-index:99998;margin-left:0}'
    + '.hub-mc-btn{display:inline-flex;align-items:center;gap:5px;padding:4px 11px;border-radius:7px;'
    + 'border:1px solid rgba(255,255,255,.16);background:rgba(255,255,255,.07);color:#d7dbe4;'
    + 'font-size:12px;line-height:1.5;cursor:pointer;user-select:none;white-space:nowrap;font-family:inherit}'
    + '.hub-mc-btn:hover{background:rgba(255,255,255,.13)}'
    + '.hub-mc-caret{font-size:9px;opacity:.65}'
    + '.hub-mc-panel{position:absolute;top:calc(100% + 6px);left:0;width:264px;max-height:330px;overflow-y:auto;'
    + 'background:#171a22;border:1px solid rgba(255,255,255,.14);border-radius:10px;padding:6px;'
    + 'box-shadow:0 10px 32px rgba(0,0,0,.55);z-index:99999;font-size:12px;color:#d7dbe4}'
    + '.hub-mc-hint{padding:6px 8px;color:#8b93a7;font-size:11px;line-height:1.5}'
    + '.hub-mc-item{display:flex;align-items:center;gap:7px;padding:6px 8px;border-radius:6px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
    + '.hub-mc-item:hover{background:rgba(255,255,255,.08)}'
    + '.hub-mc-item .hub-mc-check{width:14px;color:#4c8dff;font-weight:700;flex:none}'
    + '.hub-mc-group{padding:7px 8px 2px;color:#8b93a7;font-size:11px}'
    + '.hub-mc-sep{height:1px;background:rgba(255,255,255,.1);margin:5px 4px}'
    + '.hub-mc-toast{position:fixed;right:18px;bottom:18px;background:#232836;border:1px solid rgba(255,255,255,.16);'
    + 'color:#e7eaf2;padding:9px 14px;border-radius:9px;font-size:12px;z-index:100000;box-shadow:0 8px 24px rgba(0,0,0,.5)}';

  function ensureStyle() {
    if (document.getElementById('hub-mc-style')) { return; }
    var st = el('style'); st.id = 'hub-mc-style'; st.textContent = CSS;
    (document.head || document.documentElement).appendChild(st);
  }

  function toast(msg) {
    var old = document.querySelector('.hub-mc-toast');
    if (old) { old.remove(); }
    var t = el('div', 'hub-mc-toast', msg);
    document.body.appendChild(t);
    if (toastTimer) { clearTimeout(toastTimer); }
    toastTimer = setTimeout(function () { t.remove(); }, 2400);
  }

  function curLabel() {
    return (state.model && state.model !== 'auto') ? state.model : 'Auto · 号池轮询';
  }

  function itemHtml(val, label) {
    var it = el('div', 'hub-mc-item');
    it.setAttribute('data-m', val);
    var ck = el('span', 'hub-mc-check', (state.model === val) ? '✓' : '');
    it.appendChild(ck);
    it.appendChild(el('span', null, label));
    it.addEventListener('click', function (e) { e.stopPropagation(); pick(val); });
    return it;
  }

  function renderPanel() {
    if (!panel) { return; }
    panel.innerHTML = '';
    panel.appendChild(el('div', 'hub-mc-hint',
      'Auto = 号池内自动轮询；指定模型 = 锁定单模型，立即生效无需重启。候选来自渠道管理号池中已勾选的模型。'));
    panel.appendChild(itemHtml('auto', 'Auto · 号池轮询'));
    if (state.options.length) { panel.appendChild(el('div', 'hub-mc-sep')); }
    state.options.forEach(function (g) {
      panel.appendChild(el('div', 'hub-mc-group', g.provider_name + '（' + g.models.length + '）'));
      g.models.forEach(function (m) { panel.appendChild(itemHtml(m, m)); });
    });
  }

  function loadModel() {
    return fetch(HUB + '/api/harness/model', { method: 'GET', headers: HUB_HEADERS })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        state.model = j.model || 'auto';
        state.options = j.options || [];
        state.loaded = true;
        if (labelEl) { labelEl.textContent = curLabel(); }
        renderPanel();
      })
      .catch(function () {
        if (labelEl && !state.loaded) { labelEl.textContent = 'Hub 未连接'; }
      });
  }

  function pick(m) {
    fetch(HUB + '/api/harness/model', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: HUB_HEADERS.Authorization },
      body: JSON.stringify({ model: m })
    })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j && j.ok) {
          state.model = j.model;
          if (labelEl) { labelEl.textContent = curLabel(); }
          renderPanel();
          toast(m === 'auto' ? '已切回号池轮询' : ('已锁定模型：' + m));
        } else {
          toast('切换失败：' + ((j && j.detail) || '未知错误'));
        }
      })
      .catch(function () { toast('切换失败：Hub 未连接'); });
    closePanel();
  }

  function openPanel() {
    if (!panel) { return; }
    panel.style.display = 'block';
    loadModel();
  }

  function closePanel() {
    if (panel) { panel.style.display = 'none'; }
  }

  function buildRoot() {
    ensureStyle();
    root = el('span', 'hub-mc-root');
    root.id = 'hub-mc-root';
    btn = el('button', 'hub-mc-btn');
    btn.type = 'button';
    btn.title = '点击切换模型（Auto = 号池轮询，指定模型 = 锁定）';
    labelEl = el('span', null, state.loaded ? curLabel() : '加载中…');
    btn.appendChild(labelEl);
    btn.appendChild(el('span', 'hub-mc-caret', '▾'));
    panel = el('div', 'hub-mc-panel');
    panel.style.display = 'none';
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      if (panel.style.display === 'none') { openPanel(); } else { closePanel(); }
    });
    panel.addEventListener('click', function (e) { e.stopPropagation(); });
    root.appendChild(btn);
    root.appendChild(panel);
    document.addEventListener('click', function () { closePanel(); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { closePanel(); } });
  }

  function visible(node) {
    return !!(node && node.getBoundingClientRect && node.getBoundingClientRect().width > 0);
  }

  // Find the "标准模式" dropdown: deepest element whose text matches, then climb
  // to its clickable container (max 5 levels) and use that as the dock anchor.
  function findAnchor() {
    var leaves = document.querySelectorAll('span, div, button, a, p, label');
    var best = null;
    for (var i = 0; i < leaves.length; i++) {
      var n = leaves[i];
      var kids = n.querySelectorAll('*');
      var hasSameKid = false;
      for (var k = 0; k < kids.length; k++) {
        if (/标准模式|標準模式|standard/i.test(kids[k].textContent || '')) { hasSameKid = true; break; }
      }
      if (hasSameKid) { continue; }
      var t = (n.textContent || '').replace(/\s+/g, '');
      if (/标准模式|標準模式/i.test(t) || /^standard(mode)?$/i.test(t)) {
        if (visible(n) && t.length <= 12) { best = n; break; }
      }
    }
    if (!best) { return null; }
    var node = best;
    for (var up = 0; up < 5 && node.parentElement; up++) {
      var p = node.parentElement;
      var tag = p.tagName;
      if (tag === 'BUTTON' || p.getAttribute('role') === 'button') { node = p; continue; }
      // stop climbing when the parent contains a sibling dropdown (the chip row)
      if (/选择工作区|選擇工作區|workspace/i.test(p.textContent || '') && p.children.length >= 2) { break; }
      node = p;
    }
    return node;
  }

  function mountFallback() {
    if (fallbackMounted || !root) { return; }
    fallbackMounted = true;
    root.classList.add('hub-mc-fallback');
    document.body.appendChild(root);
  }

  function tick() {
    if (!root) { buildRoot(); loadModel(); }
    if (document.contains(root)) {
      // keep fallback class only if still in fallback mode
      failCount = 0;
      return;
    }
    if (fallbackMounted) {
      // fallback root got wiped (SPA re-render); re-mount it
      fallbackMounted = false;
      mountFallback();
      return;
    }
    var anchor = findAnchor();
    if (anchor && anchor.parentElement) {
      root.classList.remove('hub-mc-fallback');
      anchor.parentElement.insertBefore(root, anchor.nextSibling);
      failCount = 0;
    } else {
      failCount++;
      if (failCount >= FALLBACK_AFTER) { mountFallback(); }
    }
  }

  setInterval(tick, 900);
  tick();
})();
