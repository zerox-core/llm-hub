/* HUB Session Zone — injected into dsh frontend by F:\llm_hub\dsh_patch\patch_dsh.py
   会话区（playground 时间分区）：
   1) 侧栏顶部「会话区」栏：＋新会话 → POST /zone/api/create → 自动点原生"新建会话"
   2) 会话区工作区行内注入 🗑 → 弹窗「是否删除本会话的相关文件？」
      删除文件=连 cwd 文件夹+会话存储一起删；仅删会话=只删会话与记忆数据
   3) 行内 📌 归属（方案 A 纯标记，2026-09-22 拍板）：把会话分区标记归属到某个
      项目工作区，行上显示「→ 项目名」；不改 cwd、不搬存储、不动文件
   4) 列表显示美化（2026-09-22 R14 拍板）：机器命名（YYYY.MMDD-HH.mm）只保留在
      后端文件夹；侧栏里每个分区显示为一条会话样式行——主文案 = 其内会话标题，
      右侧小字 = 分区时间（MM-DD HH:mm）；分区的子级会话行隐藏，点分区行直接
      进入会话。工作区选择菜单里的机器名同样替换为「会话 MM-DD HH:mm」。
   5) Codex 风格（2026-09-23 R24 拍板）：面板里不再暴露文件夹体系——
      a) 侧栏「工作区」原生分区头隐藏；
      b) 非会话区（项目）工作区文件夹行及其子会话行整体隐藏；
      c) 会话区行的文件夹图标隐藏（只留会话标题 + 时间）；
      d) 主面板（hero/会话页）里的工作区选择 chip 隐藏，新会话直达消息输入框。
      会话标题 = 首条发送内容（由 cordis.patch.yml 摘掉 LLM 标题提供方后，
      dsh 原生确定性回退自动取首条消息开头若干字）。
   命名规则：YYYY.MMDD-HH.mm，同分钟冲突自动 (1) (2) 后缀（服务端处理）。
   无外部依赖；SPA 重渲染自动重挂（tick 模式同 hub_ui_kit）。 */
(function () {
  if (window.__hubSessionZone) { return; }
  window.__hubSessionZone = true;

  var API = '/zone/api';
  var LQ = '“'; // “
  var RQ = '”'; // ”
  var bar = null, countEl = null, addBtn = null;
  var zoneTitles = [];       // 会话区工作区标题缓存
  var zoneByTitle = {};      // title -> {id, path, sessionIds, project}
  var projectTitles = [];    // 项目工作区标题缓存（隐藏主面板工作区 chip 用）
  var fetching = false, tickN = 0, creating = false;
  var modal = null;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text != null) { n.textContent = text; }
    return n;
  }

  var CSS = ''
    + '.hub-zone-bar{display:flex;align-items:center;gap:6px;margin:2px 8px 6px;padding:6px 8px;'
    + 'border:1px solid rgba(128,140,160,.22);border-radius:8px}'
    + '.hub-zone-label{font-size:12px;font-weight:600;opacity:.8;flex:none}'
    + '.hub-zone-count{font-size:11px;opacity:.5;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
    + '.hub-zone-add{flex:none;border:0;border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer;'
    + 'background:rgba(76,141,255,.16);color:inherit;font-family:inherit}'
    + '.hub-zone-add:hover{background:rgba(76,141,255,.3)}'
    + '.hub-zone-add[disabled]{opacity:.5;cursor:default}'
    + '.hub-zone-del,.hub-zone-asg{flex:none;border:0;background:transparent;cursor:pointer;font-size:12px;'
    + 'opacity:.45;padding:2px 4px;border-radius:5px;color:inherit}'
    + '.hub-zone-del:hover{opacity:1;background:rgba(255,90,90,.18)}'
    + '.hub-zone-asg:hover{opacity:1;background:rgba(76,141,255,.2)}'
    + '.hub-zone-tag{font-size:10px;opacity:.55;margin-left:6px;white-space:nowrap;'
    + 'overflow:hidden;text-overflow:ellipsis;max-width:120px;vertical-align:middle}'
    + '.hub-zone-time{font-size:10px;opacity:.5;margin-left:6px;white-space:nowrap;flex:none}'
    + '.hub-zone-mask{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:9999;'
    + 'display:flex;align-items:center;justify-content:center}'
    + '.hub-zone-modal{width:380px;max-width:92vw;background:#262b36;color:#e8eaf0;border-radius:12px;'
    + 'padding:18px 18px 14px;box-shadow:0 12px 40px rgba(0,0,0,.5);font-size:13px;line-height:1.6}'
    + '.hub-zone-modal h3{margin:0 0 8px;font-size:15px}'
    + '.hub-zone-modal .hub-zone-path{word-break:break-all;font-size:11px;opacity:.55;margin:6px 0 0}'
    + '.hub-zone-modal .hub-zone-err{color:#ff8a8a;font-size:12px;margin-top:8px;min-height:1em}'
    + '.hub-zone-modal .hub-zone-btns{display:flex;gap:8px;margin-top:14px;justify-content:flex-end}'
    + '.hub-zone-modal button{border:0;border-radius:7px;padding:7px 12px;font-size:12px;cursor:pointer;'
    + 'font-family:inherit;background:rgba(128,140,160,.18);color:inherit}'
    + '.hub-zone-modal button:hover{background:rgba(128,140,160,.32)}'
    + '.hub-zone-modal button.hub-zone-danger{background:rgba(220,70,70,.85);color:#fff}'
    + '.hub-zone-modal button.hub-zone-danger:hover{background:rgba(230,90,90,1)}'
    + '.hub-zone-modal button[disabled]{opacity:.5;cursor:default}'
    + '.hub-zone-plist{max-height:260px;overflow:auto;margin-top:8px;display:flex;flex-direction:column;gap:4px}'
    + '.hub-zone-pitem{text-align:left;display:flex;flex-direction:column;gap:2px;padding:8px 10px}'
    + '.hub-zone-pitem .hub-zone-ppath{font-size:10px;opacity:.5;word-break:break-all}'
    + '.hub-zone-toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);z-index:9999;'
    + 'background:#262b36;color:#e8eaf0;border:1px solid rgba(128,140,160,.3);border-radius:8px;'
    + 'padding:8px 14px;font-size:12px;box-shadow:0 6px 24px rgba(0,0,0,.4)}';

  function ensureStyle() {
    if (document.getElementById('hub-zone-style')) { return; }
    var s = el('style');
    s.id = 'hub-zone-style';
    s.textContent = CSS;
    (document.head || document.documentElement).appendChild(s);
  }

  function toast(msg) {
    var t = el('div', 'hub-zone-toast', msg);
    document.body.appendChild(t);
    setTimeout(function () { if (t.parentNode) { t.parentNode.removeChild(t); } }, 2600);
  }

  /* ---------- 数据 ---------- */

  function refreshList(done) {
    if (fetching) { return; }
    fetching = true;
    fetch(API + '/list')
      .then(function (r) { return r.json(); })
      .then(function (j) {
        fetching = false;
        if (!j.ok) { throw new Error(j.error || 'list'); }
        zoneTitles = [];
        zoneByTitle = {};
        (j.workspaces || []).forEach(function (w) {
          zoneTitles.push(w.title);
          zoneByTitle[w.title] = w;
        });
        if (countEl) {
          countEl.textContent = zoneTitles.length ? (zoneTitles.length + ' 个分区') : '按时间自动分区';
        }
        if (done) { done(true); }
      })
      .catch(function () {
        fetching = false;
        if (countEl) { countEl.textContent = '会话区服务未就绪'; }
        if (done) { done(false); }
      });
    fetch(API + '/projects')
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j && j.ok) {
          projectTitles = (j.projects || []).map(function (p) { return p.title; });
        }
      })
      .catch(function () {});
  }

  /* ---------- 顶部栏 ---------- */

  function buildBar() {
    bar = el('div', 'hub-zone-bar');
    bar.id = 'hub-zone-bar';
    bar.appendChild(el('span', 'hub-zone-label', '会话区'));
    countEl = el('span', 'hub-zone-count', '…');
    bar.appendChild(countEl);
    addBtn = el('button', 'hub-zone-add', '＋ 新会话');
    addBtn.type = 'button';
    addBtn.title = '在会话区新建会话（自动按时间建分区文件夹）';
    addBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      onCreate();
    });
    bar.appendChild(addBtn);
  }

  function mountBar() {
    if (bar && document.contains(bar)) { return; }
    var region = document.querySelector('.hHd-Xa_regionArea');
    if (!region) { return; }
    if (!bar) { buildBar(); }
    region.insertBefore(bar, region.firstChild);
    refreshList();
  }

  /* ---------- 创建：建分区 → 点原生"新建会话" ---------- */

  function clickNativeNewSession(title, triesLeft) {
    var b = document.querySelector(
      'button[aria-label="在' + LQ + title + RQ + '中新建会话"]');
    if (b) {
      b.click();
      toast('已进入新会话 ' + zoneFriendly(title));
      setTimeout(function () { refreshList(); }, 1500);
      return;
    }
    if (triesLeft <= 0) {
      toast(zoneFriendly(title) + ' 已建好，请手动点它的「新建会话」');
      return;
    }
    setTimeout(function () { clickNativeNewSession(title, triesLeft - 1); }, 800);
  }

  function onCreate() {
    if (creating) { return; }
    creating = true;
    addBtn.disabled = true;
    addBtn.textContent = '创建中…';
    fetch(API + '/create', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) { throw new Error(j.error || 'create'); }
        var title = j.workspace.title;
        refreshList();
        // 等原生工作区列表经 WS 刷新出新行，然后点它的"新建会话"
        setTimeout(function () { clickNativeNewSession(title, 12); }, 500);
      })
      .catch(function (e) {
        toast('创建失败：' + String((e && e.message) || e));
      })
      .then(function () {
        creating = false;
        addBtn.disabled = false;
        addBtn.textContent = '＋ 新会话';
      });
  }

  /* ---------- 弹窗基础设施 ---------- */

  function closeModal() {
    if (modal && modal.parentNode) { modal.parentNode.removeChild(modal); }
    modal = null;
  }

  function openModal(buildBox) {
    closeModal();
    modal = el('div', 'hub-zone-mask');
    var box = el('div', 'hub-zone-modal');
    buildBox(box);
    modal.appendChild(box);
    modal.addEventListener('click', function (e) { if (e.target === modal) { closeModal(); } });
    document.body.appendChild(modal);
  }

  /* ---------- 删除：行内 🗑 + 双选项弹窗 ---------- */

  function openDeleteModal(title) {
    var w = zoneByTitle[title];
    if (!w) { toast('未找到分区信息，稍后再试'); refreshList(); return; }
    openModal(function (box) {
      box.appendChild(el('h3', null, '删除会话「' + zoneFriendly(title) + '」？'));
      box.appendChild(el('div', null, '是否删除本会话的相关文件？'));
      var hint = el('div', null,
        '「删除文件」会连同该会话在分区文件夹里创建的文件、本地记忆数据一起删除（不可恢复）；' +
        '「仅删会话」只删除会话与记忆数据，保留文件夹内容。');
      hint.style.opacity = '.75';
      box.appendChild(hint);
      box.appendChild(el('div', 'hub-zone-path', w.path));
      var err = el('div', 'hub-zone-err', '');
      box.appendChild(err);
      var btns = el('div', 'hub-zone-btns');
      var bCancel = el('button', null, '取消');
      var bSessOnly = el('button', null, '仅删会话');
      var bFiles = el('button', 'hub-zone-danger', '删除文件');
      [bCancel, bSessOnly, bFiles].forEach(function (b) { b.type = 'button'; });
      bCancel.addEventListener('click', closeModal);
      function doDelete(deleteFiles) {
        bCancel.disabled = bSessOnly.disabled = bFiles.disabled = true;
        err.textContent = '删除中…';
        fetch(API + '/delete', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ workspaceId: w.id, deleteFiles: deleteFiles }),
        })
          .then(function (r) { return r.json(); })
          .then(function (j) {
            if (!j.ok) { throw new Error(j.error || 'delete'); }
            closeModal();
            toast(deleteFiles ? '已删除会话和相关文件' : '已删除会话（文件夹已保留）');
            refreshList();
          })
          .catch(function (e) {
            err.textContent = String((e && e.message) || e);
            bCancel.disabled = bSessOnly.disabled = bFiles.disabled = false;
          });
      }
      bSessOnly.addEventListener('click', function () { doDelete(false); });
      bFiles.addEventListener('click', function () { doDelete(true); });
      btns.appendChild(bCancel);
      btns.appendChild(bSessOnly);
      btns.appendChild(bFiles);
      box.appendChild(btns);
    });
  }

  /* ---------- 归属：行内 📌 + 项目选择弹窗（方案 A 纯标记） ---------- */

  function postAssign(workspaceId, projectId, errEl, doneMsg) {
    var isUn = !projectId;
    fetch(API + (isUn ? '/unassign' : '/assign'), {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(isUn ? { workspaceId: workspaceId } : { workspaceId: workspaceId, projectId: projectId }),
    })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) { throw new Error(j.error || 'assign'); }
        closeModal();
        toast(doneMsg);
        refreshList();
      })
      .catch(function (e) {
        if (errEl) { errEl.textContent = String((e && e.message) || e); }
      });
  }

  function openAssignModal(title) {
    var w = zoneByTitle[title];
    if (!w) { toast('未找到分区信息，稍后再试'); refreshList(); return; }
    openModal(function (box) {
      box.appendChild(el('h3', null, '归属会话「' + zoneFriendly(title) + '」'));
      var cur = el('div', null,
        w.project ? ('当前归属：' + w.project.title) : '当前未归属任何项目。');
      cur.style.opacity = '.75';
      box.appendChild(cur);
      var hint = el('div', null, '只是标记归属，会话的工作目录、文件和记忆数据都不会变动。');
      hint.style.cssText = 'opacity:.55;font-size:11px;margin-top:4px';
      box.appendChild(hint);
      var listBox = el('div', 'hub-zone-plist');
      listBox.appendChild(el('div', null, '加载项目列表…'));
      box.appendChild(listBox);
      var err = el('div', 'hub-zone-err', '');
      box.appendChild(err);
      var btns = el('div', 'hub-zone-btns');
      var bCancel = el('button', null, '取消');
      bCancel.type = 'button';
      bCancel.addEventListener('click', closeModal);
      btns.appendChild(bCancel);
      box.appendChild(btns);

      fetch(API + '/projects')
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (!j.ok) { throw new Error(j.error || 'projects'); }
          listBox.textContent = '';
          var projects = j.projects || [];
          if (w.project) {
            var bUn = el('button', 'hub-zone-pitem', '✕ 取消归属（' + w.project.title + '）');
            bUn.type = 'button';
            bUn.addEventListener('click', function () {
              postAssign(w.id, null, err, '已取消归属');
            });
            listBox.appendChild(bUn);
          }
          if (!projects.length) {
            listBox.appendChild(el('div', null, '还没有项目工作区，请先在项目区创建一个。'));
            return;
          }
          projects.forEach(function (p) {
            if (w.project && w.project.id === p.id) { return; }
            var b = el('button', 'hub-zone-pitem');
            b.type = 'button';
            b.appendChild(el('div', null, p.title));
            b.appendChild(el('div', 'hub-zone-ppath', p.path));
            b.addEventListener('click', function () {
              postAssign(w.id, p.id, err, '已归属到项目「' + p.title + '」');
            });
            listBox.appendChild(b);
          });
        })
        .catch(function (e) {
          listBox.textContent = '';
          err.textContent = String((e && e.message) || e);
        });
    });
  }

  /* ---------- 行内注入：🗑 / 📌 / 归属标记 ---------- */

  function findZoneRow(opBtn) {
    return opBtn.closest('[role="treeitem"]') ||
      opBtn.parentElement.parentElement || opBtn.parentElement;
  }

  /** 在行内找标题文本节点（其 textContent 恰为 title 的最小元素）。 */
  function findTitleEl(row, title) {
    var all = row.querySelectorAll('span,div,a');
    for (var i = 0; i < all.length; i++) {
      if (all[i].textContent.trim() === title) { return all[i]; }
    }
    return null;
  }

  /* ---------- 显示名：机器命名只在后端文件夹，UI 显示友好名 ---------- */

  function isZoneTitle(t) { return zoneTitles.indexOf(t) >= 0; }

  /** 2026.0922-23.33 -> 09-22 23:33 */
  function zoneTimeText(title) {
    var m = /^(\d{4})\.(\d{2})(\d{2})-(\d{2})\.(\d{2})/.exec(title);
    return m ? (m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5]) : '';
  }

  /** 菜单/弹窗用友好名：会话 09-22 23:33 (1) */
  function zoneFriendly(title) {
    var m = /^(\d{4})\.(\d{2})(\d{2})-(\d{2})\.(\d{2})(\(\d+\))?$/.exec(title);
    if (!m) { return title; }
    return '会话 ' + m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5] + (m[6] ? ' ' + m[6] : '');
  }

  /** 标记优先地取分区行的标题元素（首次 tick 时 DOM 还是机器名，靠 findTitleEl 定位后打标）。 */
  function zoneTitleEl(row, machineTitle) {
    var marked = row.querySelector('[data-hub-zone-title]');
    if (marked) { return marked; }
    var t = findTitleEl(row, machineTitle);
    if (t) { t.setAttribute('data-hub-zone-title', machineTitle); }
    return t;
  }

  /** 会话子行的标题文本（原生会话行第一个 span 即标题，第二个是相对时间）。 */
  function sessionTitleOf(childRow) {
    var sp = childRow.querySelector('span');
    return sp ? (sp.textContent || '').trim() : '';
  }

  /**
   * 打开分区内被隐藏的会话行：向子行内层元素派发完整鼠标事件序列。
   * 仅 .click() 不会触发 dsh 原生的会话打开逻辑（它挂在 pointer/mouse 事件链上）。
   */
  function openZoneChild(t) {
    var c = document.querySelector('[data-hub-zone-child="' + t + '"]');
    if (!c) { return; }
    var target = c.querySelector('a[href]') || c.querySelector('span') || c;
    var seq = ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'];
    for (var i = 0; i < seq.length; i++) {
      var type = seq[i];
      var ev;
      if (type.indexOf('pointer') === 0 && typeof PointerEvent === 'function') {
        ev = new PointerEvent(type, { bubbles: true, cancelable: true, view: window, pointerId: 1, isPrimary: true, button: 0 });
      } else {
        ev = new MouseEvent(type, { bubbles: true, cancelable: true, view: window, button: 0 });
      }
      target.dispatchEvent(ev);
    }
  }

  /**
   * 把分区行重排成一条会话样式行：
   * 主文案 = 其内会话标题 + 小字分区时间；隐藏子级会话行；点分区行 = 进会话。
   * R24：非会话区（项目）工作区行及其子会话行整体隐藏；分区行文件夹图标隐藏。
   * R26：项目工作区行及子会话行恢复显示（hideWorkspaceHeader/hideWorkspaceChips 一并停用）。
   */
  function restyleZoneRows() {
    var root = document.querySelector('.hHd-Xa_root');
    if (!root) { return; }
    var items = root.querySelectorAll('[role="treeitem"]');
    var curZone = null;
    var inNonZone = false;
    var rows = {}, childOf = {};
    var WP = '工作区' + LQ, OP = RQ + '的操作';
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      var op = it.querySelector('button[aria-label^="' + WP + '"][aria-label$="' + OP + '"]');
      if (op) {
        var al = op.getAttribute('aria-label') || '';
        var t = al.substring(WP.length, al.length - OP.length);
        if (isZoneTitle(t)) {
          curZone = t;
          inNonZone = false;
          rows[curZone] = it;
        } else {
          // 项目工作区文件夹行：R26 起恢复显示（用户反馈看不到正常工作区）
          curZone = null;
          inNonZone = true;
        }
        continue;
      }
      if (inNonZone) {
        // 项目工作区下的会话行：R26 起恢复显示
        continue;
      }
      if (curZone && !childOf[curZone]) { childOf[curZone] = it; }
    }
    Object.keys(rows).forEach(function (t) {
      var row = rows[t], child = childOf[t];
      // 文件夹图标隐藏（按钮内的图标保留）
      var svgs = row.querySelectorAll('svg');
      for (var si = 0; si < svgs.length; si++) {
        if (!svgs[si].closest('button') && svgs[si].style.display !== 'none') {
          svgs[si].style.display = 'none';
        }
      }
      var titleEl = zoneTitleEl(row, t);
      if (!titleEl) { return; }
      var nameEl = titleEl.querySelector('[data-hub-zone-name]');
      if (!nameEl) {
        titleEl.textContent = '';
        nameEl = el('span');
        nameEl.setAttribute('data-hub-zone-name', t);
        titleEl.appendChild(nameEl);
        var timeEl = el('span', 'hub-zone-time');
        timeEl.setAttribute('data-hub-zone-time', t);
        titleEl.appendChild(timeEl);
      }
      var want = (child && sessionTitleOf(child)) || '新会话';
      if (nameEl.textContent !== want) { nameEl.textContent = want; }
      var ttEl = titleEl.querySelector('[data-hub-zone-time]');
      var tt = zoneTimeText(t);
      if (ttEl && ttEl.textContent !== tt) { ttEl.textContent = tt; }
      if (child) {
        child.setAttribute('data-hub-zone-child', t);
        if (child.style.display !== 'none') { child.style.display = 'none'; }
        if (!row.getAttribute('data-hub-zone-click')) {
          row.setAttribute('data-hub-zone-click', '1');
          row.addEventListener('click', function (e) {
            if (e.target && e.target.closest && e.target.closest('button')) { return; }
            setTimeout(function () { openZoneChild(t); }, 80);
          });
        }
      }
    });
  }

  /** 侧栏「工作区」原生分区头（标签 + 搜索/排序/新建图标行）隐藏。 */
  function hideWorkspaceHeader() {
    var region = document.querySelector('.hHd-Xa_regionArea');
    if (!region) { return; }
    var nodes = region.querySelectorAll('div, span');
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if ((n.textContent || '').trim() !== '工作区') { continue; }
      var row = n, found = false;
      for (var up = 0; up < 5 && row.parentElement && row.parentElement !== region; up++) {
        row = row.parentElement;
        if (row.querySelector('button') || row.querySelector('svg')) { found = true; break; }
      }
      // 只在确认爬到的是「带图标的标题行」时才隐藏，避免误藏整棵树
      if (found && row.style.display !== 'none') { row.style.display = 'none'; }
      return;
    }
  }

  /** 主面板（hero/会话页）里的工作区选择 chip 隐藏；侧栏与自身弹窗不动。 */
  function hideWorkspaceChips() {
    var names = {};
    var i;
    for (i = 0; i < zoneTitles.length; i++) { names[zoneTitles[i]] = 1; }
    for (i = 0; i < projectTitles.length; i++) { names[projectTitles[i]] = 1; }
    var all = document.querySelectorAll('span, div, button');
    for (i = 0; i < all.length; i++) {
      var n = all[i];
      if (n.closest('.hHd-Xa_root') || n.closest('.hub-zone-mask') ||
          n.closest('.hub-zone-toast') || n.closest('[role="menu"]') ||
          n.closest('[role="listbox"]')) { continue; }
      var t = (n.textContent || '').replace(/\s+/g, '');
      if (!t || t.length > 30) { continue; }
      var isWs = names[t] ||
        /^\d{4}\.\d{4}-\d{2}\.\d{2}(\(\d+\))?$/.test(t) ||
        /^会话\d{2}-\d{2}\d{2}:\d{2}(\(\d+\))?$/.test(t);
      if (!isWs) { continue; }
      // 爬到最小 chip 容器（父级文本至多再多个箭头字符）
      var chip = n;
      for (var up = 0; up < 4 && chip.parentElement; up++) {
        var pt = (chip.parentElement.textContent || '').replace(/\s+/g, '');
        if (pt.length <= t.length + 2) { chip = chip.parentElement; } else { break; }
      }
      if (chip !== document.body && chip !== document.documentElement &&
          chip.style.display !== 'none') {
        chip.style.display = 'none';
      }
    }
  }

  /** 工作区选择菜单（含新建会话的工作区选择）里的机器名替换成友好名。 */
  function restyleMenus() {
    var mis = document.querySelectorAll('[role="menuitem"]');
    for (var i = 0; i < mis.length; i++) {
      var tx = (mis[i].textContent || '').trim();
      if (!isZoneTitle(tx)) { continue; }
      var target = findTitleEl(mis[i], tx) || mis[i];
      var friendly = zoneFriendly(tx);
      if (target.textContent !== friendly) { target.textContent = friendly; }
    }
  }

  function injectZoneRowControls() {
    for (var i = 0; i < zoneTitles.length; i++) {
      var title = zoneTitles[i];
      var w = zoneByTitle[title] || {};
      var opBtn = document.querySelector(
        'button[aria-label="工作区' + LQ + title + RQ + '的操作"]');
      if (!opBtn || !opBtn.parentElement) { continue; }
      var holder = opBtn.parentElement;

      // 🗑 删除按钮
      if (!holder.querySelector(':scope > .hub-zone-del[data-zone="' + title + '"]')) {
        var del = el('button', 'hub-zone-del', '🗑');
        del.type = 'button';
        del.title = '删除会话（可选删除相关文件）';
        del.setAttribute('aria-label', '删除会话区会话 ' + title);
        del.setAttribute('data-zone', title);
        (function (t) {
          del.addEventListener('click', function (e) {
            e.stopPropagation();
            e.preventDefault();
            openDeleteModal(t);
          });
        })(title);
        holder.insertBefore(del, opBtn);
      }

      // 📌 归属按钮（放在 🗑 前面）
      if (!holder.querySelector(':scope > .hub-zone-asg[data-zone="' + title + '"]')) {
        var asg = el('button', 'hub-zone-asg', '📌');
        asg.type = 'button';
        asg.title = '标记归属到项目（不改目录与文件）';
        asg.setAttribute('aria-label', '归属会话区会话 ' + title + ' 到项目');
        asg.setAttribute('data-zone', title);
        (function (t) {
          asg.addEventListener('click', function (e) {
            e.stopPropagation();
            e.preventDefault();
            openAssignModal(t);
          });
        })(title);
        var delBtn = holder.querySelector(':scope > .hub-zone-del[data-zone="' + title + '"]');
        holder.insertBefore(asg, delBtn || opBtn);
      }

      // 「→ 项目名」标记：优先挂标题文本旁，找不到就不挂（下轮 tick 再试）
      var row = findZoneRow(opBtn);
      var tag = row.querySelector('.hub-zone-tag[data-zone-tag="' + title + '"]');
      var wantText = w.project ? ('→ ' + w.project.title) : '';
      if (tag) {
        if (!wantText) { tag.parentNode.removeChild(tag); }
        else if (tag.textContent !== wantText) { tag.textContent = wantText; }
      } else if (wantText) {
        var titleEl = zoneTitleEl(row, title);
        if (titleEl) {
          var nt = el('span', 'hub-zone-tag', wantText);
          nt.setAttribute('data-zone-tag', title);
          nt.title = '归属项目：' + w.project.title + '（' + w.project.path + '）';
          titleEl.appendChild(nt);
        }
      }
    }
  }

  /* ---------- 主循环 ---------- */

  function tick() {
    ensureStyle();
    var sb = document.querySelector('.hHd-Xa_root');
    if (!sb) { return; }
    mountBar();
    injectZoneRowControls();
    restyleZoneRows();
    // R26：工作区隐藏下线（hideWorkspaceHeader / hideWorkspaceChips 不再调用，
    // 函数保留以便日后需要时恢复；用户反馈看不到正常的工作区）
    restyleMenus();
    tickN++;
    if (tickN % 10 === 0) { refreshList(); } // 约 30s 一次后台刷新
  }

  /* ---------- 闪烁修复（R27，2026-09-24） ----------
     dsh 前端会自行重渲染侧栏（会话相对时间刷新、状态推送、hover 等），重渲染会把
     我们在行上的改造（隐藏子行 / 改写标题 / 藏图标 / 注入按钮）打掉；原先 900ms
     盲轮询最长近 1 秒才补回 → 行样式反复"闪回原样"，肉眼看到卡片背景闪烁。
     现改为 MutationObserver 监听侧栏子树：dsh 一改动立即（60ms 去抖内）补挂，
     空窗缩到一帧内；轮询降为 3s 一次仅作兜底。我们自己注入的节点改动会被过滤，
     不会自触发。 */

  var obArmed = false, scheduled = false;

  function insideOurs(tgt) {
    var n = tgt && (tgt.nodeType === 1 ? tgt : tgt.parentElement);
    return !!(n && n.closest && n.closest(
      '.hub-zone-bar,.hub-zone-mask,.hub-zone-toast,' +
      '[data-hub-zone-title],[data-hub-zone-name],[data-hub-zone-time],.hub-zone-tag'));
  }

  function isOurs(n) {
    if (!n || n.nodeType !== 1) { return false; }
    if (n.matches('.hub-zone-bar,.hub-zone-del,.hub-zone-asg,.hub-zone-tag,.hub-zone-time,' +
        '.hub-zone-mask,.hub-zone-toast,[data-hub-zone-name],[data-hub-zone-title]')) { return true; }
    return !!(n.querySelector && n.querySelector(
      '.hub-zone-del,.hub-zone-asg,.hub-zone-tag,[data-hub-zone-name],[data-hub-zone-time]'));
  }

  function schedule() {
    if (scheduled) { return; }
    scheduled = true;
    setTimeout(function () { scheduled = false; tick(); }, 60);
  }

  function armObserver() {
    if (obArmed || typeof MutationObserver !== 'function' || !document.body) { return; }
    obArmed = true;
    var ob = new MutationObserver(function (muts) {
      var sb = document.querySelector('.hHd-Xa_root');
      if (!sb) { return; }
      for (var i = 0; i < muts.length; i++) {
        var m = muts[i];
        if (insideOurs(m.target)) { continue; }
        if (m.type === 'characterData') {
          if (sb.contains(m.target)) { schedule(); return; }
          continue;
        }
        if (m.type !== 'childList') { continue; }
        var j, an, allOurs = m.addedNodes.length > 0;
        for (j = 0; j < m.addedNodes.length; j++) {
          an = m.addedNodes[j];
          if (an.nodeType === 1 && (an.matches('.hHd-Xa_root') ||
              (an.querySelector && an.querySelector('.hHd-Xa_root')))) { schedule(); return; }
          if (!isOurs(an)) { allOurs = false; }
        }
        if (allOurs) { continue; }           // 只新增了我们自己的注入物
        if (!sb.contains(m.target)) { continue; }
        schedule(); return;
      }
    });
    ob.observe(document.body, { childList: true, subtree: true, characterData: true });
  }

  setInterval(tick, 3000); // 兜底轮询（observer 漏网路径）
  armObserver();
  tick();
})();
