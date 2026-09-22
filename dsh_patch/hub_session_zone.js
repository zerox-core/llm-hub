/* HUB Session Zone — injected into dsh frontend by F:\llm_hub\dsh_patch\patch_dsh.py
   会话区（playground 时间分区）：
   1) 侧栏顶部「会话区」栏：＋新会话 → POST /zone/api/create → 自动点原生"新建会话"
   2) 会话区工作区行内注入 🗑 → 弹窗「是否删除本会话的相关文件？」
      删除文件=连 cwd 文件夹+会话存储一起删；仅删会话=只删会话与记忆数据
   3) 行内 📌 归属（方案 A 纯标记，2026-09-22 拍板）：把会话分区标记归属到某个
      项目工作区，行上显示「→ 项目名」；不改 cwd、不搬存储、不动文件
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
      toast('已进入新会话 ' + title);
      setTimeout(function () { refreshList(); }, 1500);
      return;
    }
    if (triesLeft <= 0) {
      toast('分区 ' + title + ' 已建好，请手动点它的「新建会话」');
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
      box.appendChild(el('h3', null, '删除会话「' + title + '」？'));
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
      box.appendChild(el('h3', null, '归属会话「' + title + '」'));
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
        var titleEl = findTitleEl(row, title);
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
    tickN++;
    if (tickN % 15 === 0) { refreshList(); } // 约 13s 一次后台刷新
  }

  setInterval(tick, 900);
  tick();
})();
