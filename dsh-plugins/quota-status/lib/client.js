window.__ModuleLoader__.load({
  id: "@dsh-local/quota-status",
  factory: (require) => {
    var module = { exports: {} };
    var exports = module.exports;
    Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
    let react_jsx_runtime = require("react/jsx-runtime");
    let react = require("react");
    const { jsx, jsxs } = react_jsx_runtime;
    const { useCallback, useEffect, useRef, useState } = react;

    //#region styles (dsw design tokens, jobs-style style-tag injection)
    const css = ".qs_root{position:relative}"
      + ".qs_trigger{min-height:28px;color:var(--dsw-alias-label-tertiary);cursor:pointer;background:0 0;border:0;border-radius:6px;align-items:center;gap:4px;padding:3px 6px;font-size:12px;line-height:18px;display:inline-flex}"
      + ".qs_trigger:hover,.qs_trigger:focus-visible{color:var(--dsw-alias-label-secondary);background:var(--dsw-alias-interactive-bg-hover)}"
      + ".qs_pct{font-variant-numeric:tabular-nums;font-size:11px}"
      + ".qs_menu{position:absolute;bottom:calc(100% + 8px);right:0;width:340px;max-height:400px;overflow-y:auto;background:var(--dsw-specific-menu);border:1px solid var(--dsw-alias-border-l2);border-radius:10px;box-shadow:var(--dsw-elevation-prominent);padding:10px 12px;z-index:1000;color:var(--dsw-alias-label-primary);font-size:12px;text-align:left}"
      + ".qs_head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:4px}"
      + ".qs_title{font-size:13px;font-weight:600;color:var(--dsw-alias-label-primary);flex:1}"
      + ".qs_updated{color:var(--dsw-alias-label-caption);font-size:11px;white-space:nowrap}"
      + ".qs_refresh{cursor:pointer;background:0 0;border:1px solid var(--dsw-alias-border-l2);border-radius:6px;color:var(--dsw-alias-label-secondary);font-size:11px;padding:2px 8px;line-height:16px}"
      + ".qs_refresh:hover{background:var(--dsw-alias-interactive-bg-hover)}"
      + ".qs_refresh:disabled{opacity:.5;cursor:default}"
      + ".qs_group{margin-top:8px}"
      + ".qs_groupName{color:var(--dsw-alias-label-caption);font-size:11px;margin-bottom:2px}"
      + ".qs_row{padding:5px 0;border-top:1px solid var(--dsw-alias-border-l1)}"
      + ".qs_rowTop{display:flex;justify-content:space-between;gap:8px;align-items:baseline}"
      + ".qs_rowTitle{color:var(--dsw-alias-label-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
      + ".qs_rowVal{color:var(--dsw-alias-label-primary);font-variant-numeric:tabular-nums;white-space:nowrap}"
      + ".qs_rowSub{color:var(--dsw-alias-label-caption);font-size:11px;margin-top:1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
      + ".qs_bar{height:4px;border-radius:2px;background:var(--dsw-alias-fill-l2);margin-top:4px;overflow:hidden}"
      + ".qs_barFill{height:100%;border-radius:2px}"
      + ".qs_err{color:var(--dsw-alias-state-error-primary);padding:6px 0}"
      + ".qs_empty{color:var(--dsw-alias-label-dimmed);padding:6px 0}";
    const tagId = "@dsh-local/quota-status/QuotaStatus.module.css";
    if (typeof document !== "undefined" && document.querySelector("style[data-plugin-css=" + JSON.stringify(tagId) + "]") === null) {
      const tag = document.createElement("style");
      tag.dataset.plugin = "@dsh-local/quota-status";
      tag.dataset.pluginCss = tagId;
      tag.textContent = css;
      document.head.appendChild(tag);
    }
    //#endregion

    //#region helpers
    function toneOf(frac) {
      if (frac == null) return "var(--dsw-alias-label-dimmed)";
      if (frac > 0.5) return "var(--dsw-alias-state-success-primary)";
      if (frac > 0.2) return "var(--dsw-alias-state-warn-label)";
      return "var(--dsw-alias-state-error-primary)";
    }

    function fmtReset(reset, verb) {
      if (!reset) return null;
      let d = null;
      if (/^\d{4}-\d{2}-\d{2}T/.test(reset)) d = new Date(reset);
      else if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(reset)) d = new Date(reset.replace(" ", "T"));
      else if (/^\d{4}-\d{2}-\d{2}$/.test(reset)) d = new Date(reset + "T00:00:00");
      const v = verb || "重置";
      if (!d || isNaN(d.getTime())) return reset + " " + v;
      const pad = (n) => String(n).padStart(2, "0");
      const ms = d.getTime() - Date.now();
      if (ms <= 0) return "即将" + v;
      if (ms < 48 * 3600000) return pad(d.getHours()) + ":" + pad(d.getMinutes()) + " " + v;
      return pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " + v;
    }

    function Ring(props) {
      const frac = props.frac;
      const C = 2 * Math.PI * 7;
      return jsxs("svg", {
        width: 18, height: 18, viewBox: "0 0 18 18", "aria-hidden": true,
        children: [
          jsx("circle", { cx: 9, cy: 9, r: 7, fill: "none", stroke: "var(--dsw-alias-fill-l2)", strokeWidth: 2.5 }, "track"),
          frac != null
            ? jsx("circle", {
                cx: 9, cy: 9, r: 7, fill: "none",
                stroke: toneOf(frac), strokeWidth: 2.5, strokeLinecap: "round",
                strokeDasharray: (frac * C).toFixed(2) + " " + C.toFixed(2),
                transform: "rotate(-90 9 9)",
              }, "prog")
            : null,
        ],
      });
    }
    //#endregion

    //#region QuotaStatusAction — the composer input.right contribution
    function QuotaStatusAction() {
      const [data, setData] = useState(null);
      const [error, setError] = useState(null);
      const [open, setOpen] = useState(false);
      const [busy, setBusy] = useState(false);
      const rootRef = useRef(null);

      const load = useCallback(async (refresh) => {
        try {
          if (refresh) setBusy(true);
          const r = await fetch(refresh ? "/quota-status/api/refresh" : "/quota-status/api/data", {
            method: refresh ? "POST" : "GET",
          });
          const j = await r.json();
          if (j && j.ok) {
            setData(j.data);
            setError(null);
          } else {
            setError((j && j.error) || "HTTP " + r.status);
          }
        } catch (e) {
          setError(String((e && e.message) || e));
        } finally {
          setBusy(false);
        }
      }, []);

      useEffect(() => {
        load(false);
        const t = setInterval(() => load(false), 60000);
        return () => clearInterval(t);
      }, [load]);

      useEffect(() => {
        if (!open) return;
        const onDown = (ev) => {
          if (rootRef.current && !rootRef.current.contains(ev.target)) setOpen(false);
        };
        const onKey = (ev) => {
          if (ev.key === "Escape") setOpen(false);
        };
        document.addEventListener("mousedown", onDown);
        document.addEventListener("keydown", onKey);
        return () => {
          document.removeEventListener("mousedown", onDown);
          document.removeEventListener("keydown", onKey);
        };
      }, [open]);

      const channels = (data && data.channels) || [];
      const primary = channels.filter((c) => c.primary && typeof c.remaining === "number");
      const worst = primary.length ? Math.min.apply(null, primary.map((c) => c.remaining)) : null;
      const pctText = worst == null ? "—" : Math.round(worst * 100) + "%";

      // group rows by channel, first-seen order
      const groups = [];
      const seen = {};
      for (const c of channels) {
        if (!seen[c.channel]) {
          seen[c.channel] = [];
          groups.push({ name: c.channel, rows: seen[c.channel] });
        }
        seen[c.channel].push(c);
      }

      const trigger = jsxs("button", {
        type: "button",
        className: "qs_trigger",
        title: error ? "模型额度（服务异常，点击查看）" : "模型额度",
        onClick: () => setOpen((o) => !o),
        children: [
          jsx(Ring, { frac: worst }, "ring"),
          jsx("span", { className: "qs_pct", children: pctText }, "pct"),
        ],
      });

      let body;
      if (error) {
        body = jsx("div", { className: "qs_err", children: "额度服务异常：" + error });
      } else if (!data) {
        body = jsx("div", { className: "qs_empty", children: "加载中…" });
      } else if (!groups.length) {
        body = jsx("div", { className: "qs_empty", children: "暂无额度数据" });
      } else {
        body = groups.map((g) =>
          jsxs("div", {
            className: "qs_group",
            children: [
              jsx("div", { className: "qs_groupName", children: g.name }, "gn"),
              g.rows.map((c) => {
                const sub = [fmtReset(c.reset, c.verb), c.sub].filter(Boolean).join(" · ");
                return jsxs("div", {
                  className: "qs_row",
                  children: [
                    jsxs("div", {
                      className: "qs_rowTop",
                      children: [
                        jsx("span", { className: "qs_rowTitle", title: c.title, children: c.title }, "t"),
                        jsx("span", { className: "qs_rowVal", children: c.remainingText }, "v"),
                      ],
                    }, "top"),
                    c.remaining != null
                      ? jsx("div", {
                          className: "qs_bar",
                          children: jsx("div", {
                            className: "qs_barFill",
                            style: { width: Math.round(c.remaining * 100) + "%", background: toneOf(c.remaining) },
                          }),
                        }, "bar")
                      : null,
                    sub ? jsx("div", { className: "qs_rowSub", children: sub }, "sub") : null,
                    (c.details || []).map((d, i) =>
                      jsx("div", { className: "qs_rowSub", children: d }, "d" + i)
                    ),
                  ],
                }, c.id);
              }),
            ],
          }, g.name)
        );
      }

      return jsxs("div", {
        className: "qs_root",
        ref: rootRef,
        children: [
          trigger,
          open
            ? jsxs("div", {
                className: "qs_menu",
                children: [
                  jsxs("div", {
                    className: "qs_head",
                    children: [
                      jsx("span", { className: "qs_title", children: "模型额度" }, "t"),
                      data && data.updatedAt
                        ? jsx("span", { className: "qs_updated", children: "同步 " + String(data.updatedAt).slice(11, 19) }, "u")
                        : null,
                      jsx("button", {
                        type: "button",
                        className: "qs_refresh",
                        disabled: busy,
                        onClick: () => load(true),
                        children: busy ? "刷新中…" : "刷新",
                      }, "r"),
                    ],
                  }, "head"),
                  body,
                ],
              }, "menu")
            : null,
        ],
      });
    }
    //#endregion

    //#region registration
    const inject = ["slots"];
    function apply(ctx) {
      ctx.slots.inject("conversation.input.right", () =>
        ctx.slots.register(
          {
            name: "conversation.input.right",
            id: "quota-status",
            order: 10,
          },
          QuotaStatusAction
        )
      );
    }
    exports.apply = apply;
    exports.inject = inject;
    return module.exports;
    //#endregion
  },
});
