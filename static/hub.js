/* LLM Key Hub 共享脚本：侧边栏收起/展开（localStorage 持久，三页通用） */
(function () {
  var KEY = 'hub_side_collapsed';
  function apply(collapsed) {
    document.body.classList.toggle('side-collapsed', collapsed);
  }
  function init() {
    apply(localStorage.getItem(KEY) === '1');
    document.addEventListener('click', function (e) {
      var t = e.target.closest('[data-side-toggle]');
      if (!t) return;
      var collapsed = !document.body.classList.contains('side-collapsed');
      try { localStorage.setItem(KEY, collapsed ? '1' : '0'); } catch (err) {}
      apply(collapsed);
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
