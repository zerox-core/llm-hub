//#region @dsh-local/remote-agent — node half
/**
 * Same-origin routes on the dsh web server for the mobile RemoteConsole page:
 *   GET  /remote-agent/api/services               service whitelist with live status
 *   POST /remote-agent/api/services/{id}/start    start a whitelisted service
 *   POST /remote-agent/api/services/{id}/stop     stop a whitelisted service
 *   GET  /remote-agent/api/reports               recent task reports (jsonl tail)
 *
 * dsh core facade (v2 2026-09-23 — 手机端全量操控入口):
 *   GET  /remote-agent/api/dsh/sessions?limit=40            newest-first session list (sessionQuery)
 *   GET  /remote-agent/api/dsh/session-events?id=<sid>&limit=200   session event tail as chat lines
 *   POST /remote-agent/api/dsh/session-create               {prompt, title?, cwd?, model?{provider,model}, agentPreset?}
 *                                                            → create Agent + queue first prompt (fire-and-forget)
 *   POST /remote-agent/api/dsh/session-prompt               {sessionId, prompt} follow-up (phone-created live agents)
 *   POST /remote-agent/api/dsh/session-cancel                {sessionId} interrupt a busy phone-created agent
 *   GET  /remote-agent/api/dsh/active                        phone-created live agents + busy/error state
 *   GET  /remote-agent/api/dsh/model                          current default model selection
 *
 * Security: service ids are whitelist-only; start/stop invoke fixed scripts with
 * array-arg spawn (no shell interpolation of request input); no free-form command surface.
 * dsh services are resolved lazily via ctx.get() so a missing optional service
 * degrades one endpoint instead of breaking plugin boot.
 *
 * R31 (2026-09-24):
 *   GET /remote-agent | /remote-agent/ | /remote-agent/console.html   RemoteConsole page (static)
 *   Every /remote-agent/api/* route now requires the shared console token
 *   (remote_console_token in F:\llm_hub\data.json) via `Authorization: Bearer`
 *   or `?token=` — FAIL-CLOSED: unconfigured token locks all APIs (503).
 *   The page itself carries no secrets and stays ungated.
 */

import net from "node:net";
import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";

const REPORTS_PATH = "F:\\llm_hub\\logs\\agent_reports.jsonl";
const REPORTS_TAIL = 100;

/** 手机端新建会话的默认工作区根（复用 session-zone 的会话区约定）。 */
const REMOTE_ZONE_ROOT = "F:\\deepseek-harness\\playground";

/** RemoteConsole 页面：与 index.js 同目录，随请求读盘——改页面无需重启 dsh。 */
const PLUGIN_DIR = path.dirname(fileURLToPath(import.meta.url));
const CONSOLE_HTML = path.join(PLUGIN_DIR, "console.html");

/** 控制台令牌：data.json.remote_console_token（mtime 缓存——换 token 无需重启 dsh）。 */
const DATA_JSON = "F:\\llm_hub\\data.json";
let _tokenCache = { mtimeMs: -1, token: "" };
function readRemoteToken() {
  try {
    const st = fs.statSync(DATA_JSON);
    if (_tokenCache.mtimeMs === st.mtimeMs) return _tokenCache.token;
    const tok = String(JSON.parse(fs.readFileSync(DATA_JSON, "utf8")).remote_console_token || "");
    _tokenCache = { mtimeMs: st.mtimeMs, token: tok };
    return tok;
  } catch (_) {
    return "";
  }
}

const SERVICES = [
  {
    id: "dsh",
    name: "dsh 服务",
    kind: "port",
    port: 3080,
    startCmd: "F:\\llm_hub\\start_dsh.bat",
    stoppable: false,
    note: "本插件随 dsh 运行，停它自己会断链——需在电脑前操作",
  },
  {
    id: "hermes-panel",
    name: "Hermes 面板",
    kind: "port",
    port: 3100,
    startCmd: "F:\\zeroxcore\\scripts\\hermes-panel-start.bat",
    stopCmd: "F:\\zeroxcore\\scripts\\hermes-panel-stop.bat",
    stoppable: true,
  },
  {
    id: "cliproxy",
    name: "CLIProxyAPI",
    kind: "port",
    port: 8317,
    startCmd: "F:\\llm_hub\\cliproxy\\start_proxy.bat",
    stoppable: true,
    stopByImage: "cli-proxy-api.exe",
  },
  {
    id: "hub-heartbeat",
    name: "hub 心跳",
    kind: "proc",
    procMatch: "hub_heartbeat.ps1",
    startCmd: "F:\\llm_hub\\hub_heartbeat.ps1",
    startAs: "ps1",
    stoppable: true,
  },
  {
    id: "hub-tunnel",
    name: "SSH 隧道",
    kind: "proc",
    procMatch: "hub_tunnel.ps1",
    startCmd: "F:\\llm_hub\\hub_tunnel.ps1",
    startAs: "ps1",
    stoppable: true,
  },
];

const inject = ["webServer"];

/* ------------------------------------------------------------------ */
/* dsh core facade helpers                                             */
/* ------------------------------------------------------------------ */

/** Lazily resolve an optional cordis service; missing service → undefined. */
function svc(ctx, name) {
  try {
    return ctx.get(name);
  } catch (_) {
    return undefined;
  }
}

/** Join text blocks of a message content array. */
function contentToText(content) {
  if (!Array.isArray(content)) return "";
  return content
    .filter((b) => b && b.type === "text" && typeof b.text === "string")
    .map((b) => b.text)
    .join("");
}

/** Map one session event to a compact chat line; non-chat events → null. */
function chatLine(event) {
  try {
    if (event.type === "user/message") {
      const text = contentToText(event.data.content);
      return text ? { role: "user", text } : null;
    }
    if (event.type === "assistant/message") {
      const text = contentToText(event.data.message.content);
      return text ? { role: "assistant", text } : null;
    }
    if (event.type === "tool/call") {
      return { role: "tool", text: "[" + event.data.name + "]" };
    }
    return null;
  } catch (_) {
    return null;
  }
}

/** Unique zone-style cwd for phone-created sessions: YYYY.MMDD-HH.mm[(n)]. */
function defaultCwd() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const base =
    d.getFullYear() + "." + pad(d.getMonth() + 1) + pad(d.getDate()) +
    "-" + pad(d.getHours()) + "." + pad(d.getMinutes());
  for (let i = 0; i < 100; i++) {
    const name = i === 0 ? base : base + "(" + i + ")";
    const dir = path.join(REMOTE_ZONE_ROOT, name);
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true });
      return dir;
    }
  }
  const dir = path.join(REMOTE_ZONE_ROOT, "remote-" + Date.now());
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

/** Phone-created live agents: sessionId → tracking record. */
const liveAgents = new Map();

function publicAgentEntry(sessionId, rec) {
  return {
    sessionId,
    title: rec.title || "",
    createdAt: rec.createdAt,
    lastPromptAt: rec.lastPromptAt,
    busy: !!rec.busy,
    error: rec.error || "",
  };
}

/** Try the known interrupt method names on an Agent handle. */
function interruptAgent(agent) {
  for (const name of ["requestStop", "abort", "cancel", "stop", "interrupt"]) {
    try {
      const fn = agent?.[name];
      if (typeof fn === "function") {
        const r = fn.call(agent);
        if (r && typeof r.then === "function") return r.then(() => name);
        return Promise.resolve(name);
      }
    } catch (_) {
      /* try next */
    }
  }
  return Promise.resolve(null);
}

/* ------------------------------------------------------------------ */
/* service-control helpers (v1, unchanged)                             */
/* ------------------------------------------------------------------ */

function probePort(port, host = "127.0.0.1", timeoutMs = 1200) {
  return new Promise((resolve) => {
    const s = net.connect({ port, host });
    const done = (ok) => { try { s.destroy(); } catch (_) {} resolve(ok); };
    s.setTimeout(timeoutMs, () => done(false));
    s.once("connect", () => done(true));
    s.once("error", () => done(false));
  });
}

function psListMatch(pattern) {
  return new Promise((resolve) => {
    // the probe's own powershell cmdline contains the pattern too — exclude CIM probes and own PID
    const script =
      "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match '" + pattern +
      "' -and $_.ProcessId -ne $PID -and $_.CommandLine -notmatch 'Get-CimInstance|Win32_Process' } | Select-Object -ExpandProperty ProcessId";
    const p = spawn("powershell.exe", ["-NoProfile", "-Command", script], { shell: false });
    let out = "";
    p.stdout.on("data", (d) => (out += String(d)));
    p.on("error", () => resolve(false));
    p.on("close", () => resolve(out.trim().length > 0));
  });
}

const AGENT_LOG = "F:\\llm_hub\\logs\\remote-agent.log";
function log(line) {
  try {
    fs.appendFileSync(AGENT_LOG, new Date().toISOString() + " " + line + "\n");
  } catch (_) {}
}

function runBat(path) {
  const p = spawn("cmd.exe", ["/c", "start", "", "cmd", "/c", path], { detached: true, stdio: "ignore", shell: false });
  p.on("error", (e) => log("bat spawn error: " + e.message));
  p.unref();
  log("bat dispatched: " + path);
  return true;
}

function runPs1(path) {
  // NOTE: spawning powershell.exe directly with detached:true sets DETACHED_PROCESS (no
  // console) on Windows and PS 5.1 then exits 0 instantly WITHOUT executing anything.
  // Wrap through `cmd /c start "" /min powershell -File` — start allocates a new minimized
  // console and detaches from this process tree (validated by isolated node tests 2026-09-23).
  const p = spawn(
    "cmd.exe",
    ["/c", "start", "", "/min", "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
    { detached: true, stdio: "ignore", shell: false }
  );
  p.on("error", (err) => log("ps1 spawn error: " + err.message));
  p.on("close", (code) => log("ps1 launcher exited code=" + code + " : " + path));
  p.unref();
  log("ps1 dispatched (via cmd start): " + path);
  return true;
}

function killByImage(image) {
  return new Promise((resolve) => {
    const p = spawn("taskkill.exe", ["/IM", image, "/F", "/T"], { shell: false });
    p.on("error", () => resolve(false));
    p.on("close", (code) => resolve(code === 0));
  });
}

function killByCmdMatch(pattern) {
  return new Promise((resolve) => {
    const script =
      "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match '" + pattern + "' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }";
    const p = spawn("powershell.exe", ["-NoProfile", "-Command", script], { shell: false });
    p.on("error", () => resolve(false));
    p.on("close", () => resolve(true));
  });
}

async function serviceState(svc) {
  let running = false;
  try {
    if (svc.kind === "port") running = await probePort(svc.port);
    else running = await psListMatch(svc.procMatch);
  } catch (_) {
    running = false;
  }
  return {
    id: svc.id,
    name: svc.name,
    running,
    stoppable: !!svc.stoppable,
    note: svc.note || "",
    probedAt: new Date().toISOString(),
  };
}

async function readReports() {
  if (!fs.existsSync(REPORTS_PATH)) return [];
  const out = [];
  const rl = readline.createInterface({ input: fs.createReadStream(REPORTS_PATH, "utf8"), crlfDelay: Infinity });
  for await (const line of rl) {
    const t = line.trim();
    if (!t) continue;
    try {
      out.push(JSON.parse(t));
    } catch (_) {
      /* skip malformed lines */
    }
  }
  return out.slice(-REPORTS_TAIL).reverse();
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (c) => {
      data += c;
      if (data.length > 262144) {
        reject(new Error("body too large"));
        req.destroy();
      }
    });
    req.on("end", () => resolve(data));
    req.on("error", reject);
  });
}

/* ------------------------------------------------------------------ */
/* plugin apply                                                        */
/* ------------------------------------------------------------------ */

function apply(ctx) {
  const send = (res, code, obj) => {
    res.writeHead(code, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
    res.end(JSON.stringify(obj));
  };

  const byId = new Map(SERVICES.map((s) => [s.id, s]));

  /* ---------- v3 (R31): token auth + console page ---------- */

  // fail-closed：data.json 未配置 remote_console_token 时，所有 API 一律拒绝
  const unauthorized = (req, res) => {
    const want = readRemoteToken();
    if (!want) {
      send(res, 503, { ok: false, error: "remote_console_token 未配置，API 已锁定" });
      return true;
    }
    let got = "";
    try {
      const url = new URL(req.url || "/", "http://127.0.0.1");
      got =
        String((req.headers && req.headers.authorization) || "").replace(/^Bearer\s+/i, "") ||
        url.searchParams.get("token") ||
        "";
    } catch (_) {
      got = "";
    }
    if (got !== want) {
      send(res, 401, { ok: false, error: "unauthorized" });
      return true;
    }
    return false;
  };

  const serveConsole = (res) => {
    try {
      const html = fs.readFileSync(CONSOLE_HTML);
      res.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
      res.end(html);
    } catch (e) {
      res.writeHead(500, { "content-type": "text/plain; charset=utf-8" });
      res.end("console.html 读取失败: " + String((e && e.message) || e));
    }
  };

  for (const pagePath of ["/remote-agent", "/remote-agent/", "/remote-agent/console.html"]) {
    ctx.effect(() =>
      ctx.webServer.register({
        kind: "exact",
        path: pagePath,
        handler: async (_req, res) => serveConsole(res),
      })
    );
  }

  /* ---------- v1: service control + reports ---------- */

  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/remote-agent/api/services",
      handler: async (req, res) => {
        if (unauthorized(req, res)) return;
        const data = await Promise.all(SERVICES.map(serviceState));
        send(res, 200, { ok: true, data });
      },
    })
  );

  for (const svc of SERVICES) {
    ctx.effect(() =>
      ctx.webServer.register({
        kind: "exact",
        path: "/remote-agent/api/services/" + svc.id + "/start",
        handler: async (req, res) => {
        if (unauthorized(req, res)) return;
          if (svc.startAs === "ps1") runPs1(svc.startCmd);
          else runBat(svc.startCmd);
          send(res, 200, { ok: true, data: { id: svc.id, action: "start", dispatched: true } });
        },
      })
    );
    if (svc.stoppable) {
      ctx.effect(() =>
        ctx.webServer.register({
          kind: "exact",
          path: "/remote-agent/api/services/" + svc.id + "/stop",
          handler: async (req, res) => {
        if (unauthorized(req, res)) return;
            if (svc.stopCmd) runBat(svc.stopCmd);
            else if (svc.stopByImage) await killByImage(svc.stopByImage);
            else await killByCmdMatch(svc.procMatch);
            send(res, 200, { ok: true, data: { id: svc.id, action: "stop", dispatched: true } });
          },
        })
      );
    }
  }

  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/remote-agent/api/reports",
      handler: async (req, res) => {
        if (unauthorized(req, res)) return;
        try {
          send(res, 200, { ok: true, data: await readReports() });
        } catch (e) {
          send(res, 500, { ok: false, error: String((e && e.message) || e) });
        }
      },
    })
  );

  /* ---------- v2: dsh core facade ---------- */

  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/remote-agent/api/dsh/sessions",
      handler: async (req, res) => {
        if (unauthorized(req, res)) return;
        try {
          const sq = svc(ctx, "sessionQuery");
          if (!sq) {
            send(res, 503, { ok: false, error: "sessionQuery 服务不可用" });
            return;
          }
          const url = new URL(req.url || "/remote-agent/api/dsh/sessions", "http://127.0.0.1");
          let limit = Number(url.searchParams.get("limit") || 40);
          if (!Number.isFinite(limit) || limit < 1) limit = 40;
          if (limit > 200) limit = 200;
          const records = await sq.listSessions();
          const rows = [];
          for (const r of records.slice(0, limit)) {
            const h = (r && r.header) || {};
            let title = "";
            try {
              const t = await sq.readTitle(h.id);
              title = t && typeof t === "object" ? String(t.title || "") : String(t || "");
            } catch (_) {
              title = "";
            }
            rows.push({
              id: String(h.id || ""),
              title: title || h.title || "",
              live: !!(r && r.live),
              persisted: !!(r && r.persisted),
              cwd: h.cwd || "",
              createdAt: h.createdAt || null,
              updatedAt: h.updatedAt || null,
              phoneCreated: liveAgents.has(String(h.id || "")),
            });
          }
          send(res, 200, { ok: true, data: rows });
        } catch (e) {
          log("dsh/sessions error: " + String((e && e.message) || e));
          send(res, 500, { ok: false, error: String((e && e.message) || e) });
        }
      },
    })
  );

  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/remote-agent/api/dsh/session-events",
      handler: async (req, res) => {
        if (unauthorized(req, res)) return;
        try {
          const url = new URL(req.url || "/remote-agent/api/dsh/session-events", "http://127.0.0.1");
          const sid = String(url.searchParams.get("id") || "");
          if (!sid || !/^[A-Za-z0-9_-]+$/.test(sid)) {
            send(res, 400, { ok: false, error: "bad session id" });
            return;
          }
          let limit = Number(url.searchParams.get("limit") || 200);
          if (!Number.isFinite(limit) || limit < 1) limit = 200;
          if (limit > 2000) limit = 2000;
          const sq = svc(ctx, "sessionQuery");
          if (!sq) {
            send(res, 503, { ok: false, error: "sessionQuery 服务不可用" });
            return;
          }
          // listEvents() returns payload-free summaries; readSurface() carries the
          // full folded current-surface events WITH data (chat text, tool names...).
          let loaded;
          try {
            loaded = await sq.readSurface(sid);
          } catch (_) {
            loaded = await sq.readSession(sid);
          }
          const events = loaded.events || [];
          const tail = events.slice(-limit);
          const chat = [];
          for (const ev of tail) {
            const line = chatLine(ev);
            if (line) chat.push(line);
          }
          let debugRaw = null;
          if (url.searchParams.get("debug") === "1") {
            debugRaw = tail.slice(0, 12).map((ev) => {
              let raw = "";
              try {
                raw = JSON.stringify(ev);
                if (raw.length > 400) raw = raw.slice(0, 400);
              } catch (_) {}
              return raw;
            });
          }
          send(res, 200, {
            ok: true,
            data: {
              id: sid,
              totalEvents: events.length,
              chat,
              debugRaw,
              busy: !!(liveAgents.get(sid) && liveAgents.get(sid).busy),
              error: (liveAgents.get(sid) && liveAgents.get(sid).error) || "",
            },
          });
        } catch (e) {
          log("dsh/session-events error: " + String((e && e.message) || e));
          send(res, 500, { ok: false, error: String((e && e.message) || e) });
        }
      },
    })
  );

  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/remote-agent/api/dsh/model",
      handler: async (req, res) => {
        if (unauthorized(req, res)) return;
        try {
          const dm = svc(ctx, "agentDefaultModel");
          if (!dm || typeof dm.currentSelection !== "function") {
            send(res, 503, { ok: false, error: "agentDefaultModel 服务不可用" });
            return;
          }
          const sel = dm.currentSelection();
          send(res, 200, { ok: true, data: { provider: sel?.provider || "", model: sel?.model || "" } });
        } catch (e) {
          send(res, 500, { ok: false, error: String((e && e.message) || e) });
        }
      },
    })
  );

  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/remote-agent/api/dsh/active",
      handler: async (req, res) => {
        if (unauthorized(req, res)) return;
        const rows = [];
        for (const [sid, rec] of liveAgents) rows.push(publicAgentEntry(sid, rec));
        send(res, 200, { ok: true, data: rows });
      },
    })
  );

  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/remote-agent/api/dsh/session-create",
      handler: async (req, res) => {
        if (unauthorized(req, res)) return;
        if (req.method !== "POST") {
          send(res, 405, { ok: false, error: "POST only" });
          return;
        }
        let body;
        try {
          body = JSON.parse((await readBody(req)) || "{}");
        } catch {
          send(res, 400, { ok: false, error: "bad json" });
          return;
        }
        const prompt = String(body.prompt || "").trim();
        if (!prompt) {
          send(res, 400, { ok: false, error: "prompt 必填" });
          return;
        }
        try {
          const agents = svc(ctx, "agents");
          if (!agents || typeof agents.create !== "function") {
            send(res, 503, { ok: false, error: "agents 服务不可用" });
            return;
          }
          // model: explicit body.model {provider,model} > default selection
          let agentOptions;
          if (body.model && typeof body.model.provider === "string" && typeof body.model.model === "string") {
            agentOptions = { provider: body.model.provider, model: body.model.model };
          } else {
            const dm = svc(ctx, "agentDefaultModel");
            if (dm && typeof dm.currentSelection === "function") {
              const sel = dm.currentSelection();
              if (sel && sel.provider && sel.model) agentOptions = { provider: sel.provider, model: sel.model };
            }
          }
          const cwd =
            typeof body.cwd === "string" && path.isAbsolute(body.cwd) && fs.existsSync(body.cwd)
              ? body.cwd
              : defaultCwd();
          const sessionId = "session-" + randomUUID();
          // agent preset (optional): resolve first match of body.agentPreset / "default" / "general"
          const presets = svc(ctx, "agentPresets");
          let preset = null;
          if (presets && typeof presets.resolve === "function") {
            for (const cand of [body.agentPreset, "default", "general"]) {
              if (!cand) continue;
              try {
                preset = await presets.resolve(String(cand));
                break;
              } catch (_) {
                preset = null;
              }
            }
          }
          const createArg = {
            sessionId,
            meta: Object.assign(
              { cwd },
              preset && preset.id ? { agentPreset: preset.id } : {}
            ),
            setup: (agentCtx) => {
              if (preset && presets && typeof presets.mount === "function") {
                try {
                  presets.mount(agentCtx, preset.id);
                } catch (e) {
                  log("preset mount failed: " + String((e && e.message) || e));
                }
              }
            },
          };
          if (agentOptions) createArg.agentOptions = agentOptions;
          const handle = await agents.create(createArg);
          const agent = handle && handle.agent;
          if (!agent || typeof agent.followup !== "function") {
            try {
              await handle.dispose();
            } catch (_) {}
            send(res, 500, { ok: false, error: "agent 创建异常（无 followup）" });
            return;
          }
          liveAgents.set(sessionId, {
            handle,
            title: String(body.title || "").slice(0, 80),
            createdAt: new Date().toISOString(),
            lastPromptAt: new Date().toISOString(),
            busy: false,
            error: "",
          });
          const rec = liveAgents.get(sessionId);
          // rename via sessionTitle (optional)
          const sessionTitle = svc(ctx, "sessionTitle");
          if (sessionTitle && typeof sessionTitle.rename === "function" && body.title) {
            try {
              sessionTitle.rename(agent.session, String(body.title).slice(0, 80));
            } catch (_) {}
          }
          rec.busy = true;
          try {
            agent.followup({
              content: [{ type: "text", text: prompt }],
              source: { kind: "user" },
            });
          } catch (e) {
            rec.busy = false;
            rec.error = String((e && e.message) || e);
            log("followup rejected: " + rec.error);
            send(res, 500, { ok: false, error: "prompt 未被接受：" + rec.error });
            return;
          }
          // drain in background: idle → flush persisted log
          Promise.resolve()
            .then(() => agent.whenIdle())
            .then(
              () => {
                rec.busy = false;
                const sessions = svc(ctx, "sessions");
                if (sessions && typeof sessions.flush === "function") {
                  try {
                    sessions.flush(agent.session);
                  } catch (_) {}
                }
                log("remote session idle: " + sessionId);
              },
              (e) => {
                rec.busy = false;
                rec.error = String((e && e.message) || e);
                log("remote session failed: " + sessionId + " " + rec.error);
              }
            );
          send(res, 200, {
            ok: true,
            data: { sessionId, cwd, dispatched: true },
          });
        } catch (e) {
          log("dsh/session-create error: " + String((e && e.message) || e));
          send(res, 500, { ok: false, error: String((e && e.message) || e) });
        }
      },
    })
  );

  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/remote-agent/api/dsh/session-prompt",
      handler: async (req, res) => {
        if (unauthorized(req, res)) return;
        if (req.method !== "POST") {
          send(res, 405, { ok: false, error: "POST only" });
          return;
        }
        let body;
        try {
          body = JSON.parse((await readBody(req)) || "{}");
        } catch {
          send(res, 400, { ok: false, error: "bad json" });
          return;
        }
        const sessionId = String(body.sessionId || "");
        const prompt = String(body.prompt || "").trim();
        if (!sessionId || !prompt) {
          send(res, 400, { ok: false, error: "sessionId 与 prompt 均必填" });
          return;
        }
        const rec = liveAgents.get(sessionId);
        if (!rec || !rec.handle || !rec.handle.agent) {
          send(res, 409, {
            ok: false,
            error: "该会话不是本机手机入口创建的活跃会话（或已随 dsh 重启丢失）",
          });
          return;
        }
        if (rec.busy) {
          send(res, 409, { ok: false, error: "会话忙（上一轮未完成），可先取消" });
          return;
        }
        try {
          const agent = rec.handle.agent;
          rec.busy = true;
          rec.error = "";
          rec.lastPromptAt = new Date().toISOString();
          agent.followup({
            content: [{ type: "text", text: prompt }],
            source: { kind: "user" },
          });
          Promise.resolve()
            .then(() => agent.whenIdle())
            .then(
              () => {
                rec.busy = false;
                const sessions = svc(ctx, "sessions");
                if (sessions && typeof sessions.flush === "function") {
                  try {
                    sessions.flush(agent.session);
                  } catch (_) {}
                }
              },
              (e) => {
                rec.busy = false;
                rec.error = String((e && e.message) || e);
              }
            );
          send(res, 200, { ok: true, data: { sessionId, dispatched: true } });
        } catch (e) {
          rec.busy = false;
          rec.error = String((e && e.message) || e);
          log("dsh/session-prompt error: " + rec.error);
          send(res, 500, { ok: false, error: String((e && e.message) || e) });
        }
      },
    })
  );

  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/remote-agent/api/dsh/session-cancel",
      handler: async (req, res) => {
        if (unauthorized(req, res)) return;
        if (req.method !== "POST") {
          send(res, 405, { ok: false, error: "POST only" });
          return;
        }
        let body;
        try {
          body = JSON.parse((await readBody(req)) || "{}");
        } catch {
          send(res, 400, { ok: false, error: "bad json" });
          return;
        }
        const sessionId = String(body.sessionId || "");
        const rec = liveAgents.get(sessionId);
        if (!rec || !rec.handle || !rec.handle.agent) {
          send(res, 404, { ok: false, error: "活跃会话不存在" });
          return;
        }
        const how = await interruptAgent(rec.handle.agent);
        rec.error = how ? "" : "未找到可用的中断方法";
        send(res, 200, { ok: true, data: { sessionId, interruptedVia: how } });
      },
    })
  );
}

export { apply, inject };
//#endregion
