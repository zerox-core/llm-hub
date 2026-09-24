(function () {
  if (window.__hubRemoteConsole) return;
  window.__hubRemoteConsole = true;

  function ensureStyle() {
    if (document.getElementById("hub-rc-style")) return;
    var st = document.createElement("style");
    st.id = "hub-rc-style";
    st.textContent = [
      ".hub-rc-btn{display:flex;align-items:center;gap:8px;width:100%;padding:7px 10px;border:0;",
      "  background:transparent;border-radius:8px;color:var(--dsh-fg, #121722);font-size:13px;",
      "  cursor:pointer;text-align:left;}",
      ".hub-rc-btn:hover{background:rgba(128,140,160,.14)}",
      ".hub-rc-overlay{position:fixed;inset:0;z-index:9999;background:rgba(18,23,34,.45);}",
      ".hub-rc-panel{position:absolute;top:0;right:0;bottom:0;width:100%;max-width:520px;",
      "  background:#F4F6FB;display:flex;flex-direction:column;border-left:1px solid #E5E9F2;",
      "  box-shadow:-8px 0 32px rgba(18,23,34,.18);}",
      ".hub-rc-top{display:flex;align-items:center;gap:8px;padding:10px 12px;background:#fff;",
      "  border-bottom:1px solid #E5E9F2;flex:0 0 auto;}",
      ".hub-rc-top .t{flex:1;font-size:14px;font-weight:600;color:#121722;}",
      ".hub-rc-top button{border:1px solid #E5E9F2;background:#fff;border-radius:8px;",
      "  padding:5px 10px;font-size:12px;color:#2F5DDF;cursor:pointer;}",
      ".hub-rc-top button:hover{background:#F4F6FB}",
      ".hub-rc-frame{flex:1;border:0;width:100%;background:#F4F6FB;}",
      "@media (min-width:900px){.hub-rc-panel{width:520px;max-width:520px;}}",
    ].join("\n");
    document.head.appendChild(st);
  }

  function closePanel() {
    var ov = document.getElementById("hub-rc-overlay");
    if (ov) ov.remove();
  }

  function openPanel() {
    ensureStyle();
    if (document.getElementById("hub-rc-overlay")) return;
    var ov = document.createElement("div");
    ov.id = "hub-rc-overlay";
    ov.className = "hub-rc-overlay";
    ov.innerHTML = [
      '<div class="hub-rc-panel">',
      '  <div class="hub-rc-top">',
      '    <span class="t">远程控制台</span>',
      '    <button class="hub-rc-newtab" type="button">新标签打开</button>',
      '    <button class="hub-rc-close" type="button">关闭</button>',
      '  </div>',
      '  <iframe class="hub-rc-frame" src="/remote-agent/" title="远程控制台"></iframe>',
      "</div>",
    ].join("\n");
    document.body.appendChild(ov);
    ov.addEventListener("click", function (e) {
      if (e.target === ov) closePanel();
    });
    ov.querySelector(".hub-rc-close").addEventListener("click", closePanel);
    ov.querySelector(".hub-rc-newtab").addEventListener("click", function () {
      window.open("/remote-agent/", "_blank");
    });
  }

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closePanel();
  });

  function mount() {
    try {
      var root =
        document.querySelector(".hHd-Xa_footerActions") ||
        document.querySelector('[data-slot="sidebar.footer.action"]');
      if (!root || document.getElementById("hub-rc-btn")) return;
      ensureStyle();
      var btn = document.createElement("button");
      btn.id = "hub-rc-btn";
      btn.className = "hub-rc-btn";
      btn.type = "button";
      btn.textContent = "远程控制台";
      btn.title = "手机端远程控制台（嵌入面板）";
      btn.addEventListener("click", openPanel);
      root.appendChild(btn);
    } catch (_) {}
  }

  setInterval(mount, 1200);
  mount();
})();
