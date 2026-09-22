/* HUB UI Kit — injected into dsh frontend by F:\llm_hub\dsh_patch\patch_dsh.py
   1) 侧栏底部（设置上方）手动更新按钮，走同源 /dsh-updater/api/*
   2) 侧栏箭头化收起 + 光标移到边缘弹出 + 移出自动收起
   3) 会话区全铺开（--dsh-conversation-column-width:100%）
   无外部依赖；SPA 重渲染自动重挂。 */
(function () {
  if (window.__hubUiKit) { return; }
  window.__hubUiKit = true;

  var API = '/dsh-updater/api';
  var st = { phase: 'idle', current: null, target: null, error: null };
  var root = null, mainEl = null, subEl = null, icoEl = null, btnEl = null;
  var peeking = false;
  var bound = (typeof WeakSet !== 'undefined') ? new WeakSet() : null;
  var fetching = false;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text != null) { n.textContent = text; }
    return n;
  }

  var CSS = ''
    /* --- 全铺开：会话列宽变量直接拉到 100% --- */
    + '.pI_x6G_centerCol [data-phase]{--dsh-conversation-column-width:100%!important}'
    /* --- 收起/展开按钮箭头化 --- */
    + 'button[aria-label="收起侧边栏"] svg,button[aria-label="打开侧边栏"] svg{display:none!important}'
    + 'button[aria-label="收起侧边栏"]::before{content:"\u2039";font-size:18px;line-height:1;display:block;font-weight:600}'
    + 'button[aria-label="打开侧边栏"]::before{content:"\u203a";font-size:18px;line-height:1;display:block;font-weight:600}'
    /* --- 折叠成细轨道：只留箭头，隐藏其它入口 --- */
    + '.hHd-Xa_collapsed .hHd-Xa_newSession,'
    + '.hHd-Xa_collapsed .hHd-Xa_regionArea,'
    + '.hHd-Xa_collapsed .hHd-Xa_settingsArea{display:none!important}'
    + '.hHd-Xa_collapsed .hHd-Xa_logoRow{padding:4px 0!important;justify-content:center!important}'
    + '.hHd-Xa_collapsed .hHd-Xa_footArea{padding-bottom:6px}'
    /* --- 更新按钮 --- */
    + '.hub-upd-row{padding:2px 8px 0}'
    + '.hub-upd-btn{display:flex;align-items:center;gap:8px;width:100%;padding:7px 10px;border:0;'
    + 'border-radius:8px;background:transparent;color:inherit;cursor:pointer;font-family:inherit;text-align:left}'
    + '.hub-upd-btn:hover{background:rgba(128,140,160,.14)}'
    + '.hub-upd-btn[disabled]{cursor:default;opacity:.7}'
    + '.hub-upd-ico{font-size:13px;width:16px;text-align:center;flex:none;opacity:.85}'
    + '.hub-upd-spin{animation:hubUpdSpin 1s linear infinite;display:inline-block}'
    + '@keyframes hubUpdSpin{to{transform:rotate(360deg)}}'
    + '.hub-upd-text{display:flex;flex-direction:column;min-width:0}'
    + '.hub-upd-main{font-size:13px;line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
    + '.hub-upd-sub{font-size:11px;line-height:1.35;opacity:.55;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
    + '.hub-upd-sub.hub-upd-new{opacity:.9;color:#4c8dff}'
    + '.hHd-Xa_collapsed .hub-upd-row{padding:2px 2px 0}'
    + '.hHd-Xa_collapsed .hub-upd-text{display:none}'
    + '.hHd-Xa_collapsed .hub-upd-btn{justify-content:center;padding:7px 4px}';

  function ensureStyle() {
    if (document.getElementById('hub-uikit-style')) { return; }
    var s = el('style');
    s.id = 'hub-uikit-style';
    s.textContent = CSS;
    (document.head || document.documentElement).appendChild(s);
  }

  /* ---------- 更新按钮 ---------- */

  function render() {
    if (!mainEl) { return; }
    var main = '检查更新', sub = '', ico = '\u21BB', spin = false, dis = false, isNew = false;
    if (st.phase === 'checking') { main = '正在检查…'; spin = true; dis = true; }
    else if (st.phase === 'uptodate') { sub = '已是最新 ' + (st.current || ''); }
    else if (st.phase === 'available') { main = '更新 dsh'; sub = (st.current || '?') + ' \u2192 ' + st.target; isNew = true; }
    else if (st.phase === 'updating') { main = '正在更新…'; sub = '下载安装中，勿关闭页面'; spin = true; dis = true; }
    else if (st.phase === 'restarting') { main = '正在重启…'; sub = '恢复后页面自动刷新'; spin = true; dis = true; }
    else if (st.phase === 'error') { sub = '检查失败，点击重试'; }
    else if (st.current) { sub = '当前 ' + st.current; }
    mainEl.textContent = main;
    subEl.textContent = sub;
    subEl.className = 'hub-upd-sub' + (isNew ? ' hub-upd-new' : '');
    icoEl.textContent = ico;
    icoEl.className = 'hub-upd-ico' + (spin ? ' hub-upd-spin' : '');
    btnEl.disabled = dis;
    btnEl.title = (st.phase === 'available')
      ? ('点击更新：' + st.current + ' \u2192 ' + st.target + '（自动安装+重启）')
      : '检查 dsh 新版本（npm dist-tags: latest/next）';
  }

  function fetchStatus(force) {
    if (fetching && !force) { return; }
    fetching = true;
    if (st.phase !== 'updating' && st.phase !== 'restarting') {
      st.phase = 'checking';
      render();
    }
    fetch(API + '/status')
      .then(function (r) { return r.json(); })
      .then(function (j) {
        fetching = false;
        if (!j.ok) { throw new Error(j.error || 'status'); }
        st.current = j.current;
        if (j.updateAvailable) { st.phase = 'available'; st.target = j.target; }
        else { st.phase = 'uptodate'; }
        render();
      })
      .catch(function (e) {
        fetching = false;
        st.phase = 'error';
        st.error = String((e && e.message) || e);
        render();
      });
  }

  function waitBack() {
    setTimeout(function poll() {
      fetch(API + '/status')
        .then(function (r) { return r.json(); })
        .then(function () { location.reload(); })
        .catch(function () { setTimeout(poll, 3000); });
    }, 5000);
  }

  function onUpdClick() {
    if (st.phase === 'available') {
      if (!confirm('更新 dsh：' + st.current + ' \u2192 ' + st.target + '\n约 1 分钟，dsh 会自动重启，页面随后自动刷新。')) { return; }
      st.phase = 'updating';
      render();
      fetch(API + '/update', { method: 'POST' })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (!j.ok) { throw new Error(j.error || 'update'); }
          if (j.noop) { st.phase = 'uptodate'; render(); return; }
          st.phase = 'restarting';
          render();
          waitBack();
        })
        .catch(function (e) {
          st.phase = 'error';
          st.error = String((e && e.message) || e);
          render();
        });
    } else {
      fetchStatus(true);
    }
  }

  function buildUpd() {
    root = el('div', 'hub-upd-row');
    root.id = 'hub-upd-row';
    btnEl = el('button', 'hub-upd-btn');
    btnEl.type = 'button';
    icoEl = el('span', 'hub-upd-ico', '\u21BB');
    var text = el('span', 'hub-upd-text');
    mainEl = el('span', 'hub-upd-main', '检查更新');
    subEl = el('span', 'hub-upd-sub', '');
    text.appendChild(mainEl);
    text.appendChild(subEl);
    btnEl.appendChild(icoEl);
    btnEl.appendChild(text);
    btnEl.addEventListener('click', function (e) {
      e.stopPropagation();
      onUpdClick();
    });
    root.appendChild(btnEl);
  }

  function mountUpd() {
    if (root && document.contains(root)) { return; }
    var fa = document.querySelector('.hHd-Xa_footerActions')
      || document.querySelector('[data-slot="sidebar.footer.action"]');
    if (!fa) { return; }
    if (!root) { buildUpd(); render(); }
    fa.appendChild(root);
    fetchStatus();
  }

  /* ---------- 侧栏：箭头收起 + 边缘弹出 + 移出自动收起 ---------- */

  function isCollapsed() {
    return !!document.querySelector('.hHd-Xa_collapsed');
  }

  function findBtn(label) {
    return document.querySelector('button[aria-label="' + label + '"]');
  }

  function bindSidebar(sb) {
    if (!sb || (bound && bound.has(sb))) { return; }
    if (bound) { bound.add(sb); }
    sb.addEventListener('mouseenter', function () {
      if (isCollapsed()) {
        var b = findBtn('\u6253\u5f00\u4fa7\u8fb9\u680f');
        if (b) { peeking = true; b.click(); }
      }
    });
    sb.addEventListener('mouseleave', function () {
      if (!peeking || isCollapsed()) { return; }
      setTimeout(function () {
        if (peeking && !isCollapsed() && !sb.matches(':hover')) {
          var b = findBtn('\u6536\u8d77\u4fa7\u8fb9\u680f');
          if (b) { b.click(); }
          peeking = false;
        }
      }, 300);
    });
    sb.addEventListener('click', function (e) {
      var t = (e.target && e.target.closest)
        ? e.target.closest('button[aria-label="\u6253\u5f00\u4fa7\u8fb9\u680f"]') : null;
      if (t && e.isTrusted) { peeking = false; }
    }, true);
  }

  function slimRail() {
    var frame = document.querySelector('.pI_x6G_frame');
    if (!frame) { return; }
    var collapsed = isCollapsed();
    var want = collapsed ? '30px' : '280px';
    var nativeW = collapsed ? '56px' : '280px';
    var parts = (frame.style.gridTemplateColumns || '').split(/\s+/).filter(Boolean);
    if (!parts.length) { return; }
    if (parts[0] === nativeW && parts[0] !== want) {
      parts[0] = want;
      frame.style.gridTemplateColumns = parts.join(' ');
    }
    var handle = document.querySelector('.pI_x6G_handle[data-side="sidebar"]');
    if (handle && (parts[0] === want)) { handle.style.left = want; }
  }

  function spread() {
    var nodes = document.querySelectorAll('[style*="dsh-conversation-column-width"]');
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].style.getPropertyValue('--dsh-conversation-column-width') !== '100%') {
        nodes[i].style.setProperty('--dsh-conversation-column-width', '100%');
      }
    }
  }

  function tick() {
    ensureStyle();
    var sb = document.querySelector('.hHd-Xa_root');
    if (sb) {
      mountUpd();
      bindSidebar(sb);
      slimRail();
    }
    spread();
  }

  setInterval(tick, 900);
  tick();
})();
