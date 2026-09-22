//#region @dsh-local/quota-status — node half
/**
 * Registers two same-origin HTTP routes on the dsh web server:
 *   GET  /quota-status/api/data     condensed quota snapshot proxied from llm-hub
 *   POST /quota-status/api/refresh  ask hub to re-sync every quota source, then return fresh data
 * The browser half renders these; no CORS, no tokens page-side.
 */

const DEFAULT_HUB = "https://hub.zeroxcore.tech";
const HUB_AUTH = "Basic " + Buffer.from("hubadmin:f844c52d260ac865").toString("base64");

const inject = ["webServer"];

function pct(x) {
  return Math.max(0, Math.min(1, x));
}

function fmtNum(n) {
  if (typeof n !== "number") return "—";
  if (n >= 10000) return (n / 10000).toFixed(n >= 100000 ? 0 : 1).replace(/\.0$/, "") + " 万";
  return String(n);
}

function condense(st) {
  const channels = [];

  // --- Google Antigravity: per-account groups with 5h / weekly windows ---
  const ag = st && st.ag_quota;
  if (ag && Array.isArray(ag.accounts)) {
    for (const acc of ag.accounts) {
      if (!acc || acc.ok === false) {
        channels.push({
          id: "ag:" + (acc && acc.email ? acc.email : "?"),
          channel: "Antigravity",
          title: (acc && acc.email) || "账号",
          remaining: null,
          remainingText: "不可用",
          sub: (acc && acc.error) || "同步失败",
          reset: null,
          verb: "",
          primary: false,
        });
        continue;
      }
      for (const g of acc.groups || []) {
        for (const b of g.buckets || []) {
          const win = b.window === "5h" ? "5 小时限额" : b.window === "weekly" ? "每周限额" : String(b.window || "限额");
          channels.push({
            id: "ag:" + acc.email + ":" + b.bucket_id,
            channel: "Antigravity",
            title: (g.title || g.key || "组") + " · " + win,
            remaining: typeof b.remaining === "number" ? pct(b.remaining) : null,
            remainingText: typeof b.remaining === "number" ? (b.remaining * 100).toFixed(1) + "%" : "—",
            sub: acc.email + (b.cooldown_until ? " · 冷却至 " + b.cooldown_until : ""),
            reset: b.reset_time || null,
            verb: "重置",
            primary: b.window === "5h",
          });
        }
      }
    }
  }

  // --- WorkBuddy: credit packages ---
  const wb = st && st.wb_quota;
  if (wb && Array.isArray(wb.accounts)) {
    for (const acc of wb.accounts) {
      const c = (acc && acc.credits) || {};
      const details = (c.packages || []).map(
        (p) => p.name + " " + p.remain + "/" + p.size + " · " + String(p.cycle_end || "").slice(5, 10) + " 截止"
      );
      channels.push({
        id: "wb:" + (acc.uid || "?"),
        channel: "WorkBuddy",
        title: "CodeBuddy 积分",
        remaining: c.total_size ? pct(c.total_remain / c.total_size) : null,
        remainingText: typeof c.total_remain === "number" ? c.total_remain + " / " + c.total_size : "—",
        sub: acc.nickname || "",
        details,
        reset: c.packages && c.packages[0] ? c.packages[0].cycle_end : null,
        verb: "截止",
        primary: true,
      });
    }
  }

  // --- GitHub Copilot: premium requests ---
  const cp = st && st.copilot_quota;
  if (cp && cp.premium) {
    const used = cp.premium.used || 0;
    const total = cp.premium.total || 0;
    channels.push({
      id: "copilot:" + (cp.login || "?"),
      channel: "GitHub Copilot",
      title: "高级请求（premium）· " + (cp.plan || ""),
      remaining: total ? pct((total - used) / total) : null,
      remainingText: total ? total - used + " / " + total : "—",
      sub: cp.login || "",
      reset: cp.resets || null,
      verb: "重置",
      primary: true,
    });
  }

  // --- 百炼: per-model free quotas, worst first ---
  const q = st && st.quota;
  if (q && q.entries) {
    const rows = Object.entries(q.entries)
      .map(([model, e]) => ({
        id: "bailian:" + model,
        channel: "百炼",
        title: model,
        remaining: e && e.total ? pct(e.remaining / e.total) : null,
        remainingText: e ? fmtNum(e.remaining) + " / " + fmtNum(e.total) : "—",
        sub: "",
        reset: (e && e.expires) || null,
        verb: "到期",
        primary: false,
      }))
      .sort((a, b) => (a.remaining == null ? 1 : a.remaining) - (b.remaining == null ? 1 : b.remaining));
    channels.push(...rows);
  }

  const synced = [
    ag && ag.synced_at,
    wb && wb.synced_at,
    cp && cp.synced_at,
    q && q.synced_at,
  ].filter(Boolean);
  return {
    channels,
    updatedAt: synced.sort().pop() || null,
    serverTime: (st && st.server_time) || null,
  };
}

function apply(ctx, config) {
  const hubBase = String((config && config.hubBase) || DEFAULT_HUB).replace(/\/+$/, "");

  const send = (res, code, obj) => {
    res.writeHead(code, {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    });
    res.end(JSON.stringify(obj));
  };

  const loadState = async () => {
    const r = await fetch(hubBase + "/api/state", { headers: { Authorization: HUB_AUTH }, signal: AbortSignal.timeout(8000) });
    if (!r.ok) throw new Error("hub /api/state HTTP " + r.status);
    return await r.json();
  };

  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/quota-status/api/data",
      handler: async (req, res) => {
        try {
          send(res, 200, { ok: true, data: condense(await loadState()) });
        } catch (e) {
          send(res, 502, { ok: false, error: String((e && e.message) || e) });
        }
      },
    })
  );

  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/quota-status/api/refresh",
      handler: async (req, res) => {
        try {
          const eps = ["/api/ag/quota/refresh", "/api/wb/quota/refresh", "/api/copilot/quota/refresh", "/api/quota/refresh"];
          await Promise.allSettled(
            eps.map((ep) => fetch(hubBase + ep, { method: "POST", headers: { Authorization: HUB_AUTH }, signal: AbortSignal.timeout(25000) }))
          );
          send(res, 200, { ok: true, data: condense(await loadState()) });
        } catch (e) {
          send(res, 502, { ok: false, error: String((e && e.message) || e) });
        }
      },
    })
  );
}

export { apply, inject };
//#endregion
