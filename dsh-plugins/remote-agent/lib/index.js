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
 *   POST /remote-agent/api/dsh/session-prompt               {sessionId, prompt} follow-up（R39 起支持任意可见会话：复用进程内活跃 agent 或 agents.resume 恢复持久化会话）
 *   POST /remote-agent/api/dsh/session-cancel                {sessionId} interrupt a busy attached agent
 *   GET  /remote-agent/api/dsh/active                        phone-created live agents + busy/error state
 *   GET  /remote-agent/api/dsh/model                          current default model selection
 *   GET  /remote-agent/api/dsh/workspaces                     R44: 原生工作区列表（PC 端创建）+ 消息区根路径
 *   POST /remote-agent/api/dsh/session-archive                R44: {sessionId} 归档（=桌面删除，两端同步消失）
 *   GET  /remote-agent/api/dsh/models                       R45: 模型目录（llm.listProviders/listModels，按 provider 分组）
 *   POST /remote-agent/api/dsh/model-set                    R45: {provider, model, sessionId?} 切换模型（resolveCallConfig 校验 + saveSelection 持久化；带 sessionId 时改该会话下一次请求）
 *   GET  /remote-agent/api/dsh/commands?id=<sid>            R45: 会话可用快捷指令列表（/compact 等）
 *   POST /remote-agent/api/dsh/command                      R45: {sessionId, line} 执行快捷指令，返回 {kind, text}
 *   GET  /remote-agent/api/status                        dsh online heartbeat {uptimeSec, activeCount, model, now} (v34)
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
 *
 * R34 (2026-09-24):
 *   GET /remote-agent/api/status — heartbeat for the console page (8s poll);
 *   console.html v34 restyled to a native-app look; the services tab was removed
 *   from the page (service APIs remain token-gated but are no longer used by it);
 *   the page auto-detects dsh online/offline and self-recovers without user input.
 */

import net from "node:net";
import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";

// R39: 与 dsh-llm createUserMessage 对齐——为用户消息补稳定 id（brandString 运行时为恒等）。
// 不补 id 时，持久化日志重放校验会抛 "user/message ... lacks an identified message"，
// 导致该会话后续事件读取直接 500（曾见聊天 6074ba6b index 8、你好啊 4274504e index 177）。
function createUserMessage(content, source) {
  return Object.freeze({ id: randomUUID(), role: "user", content, source });
}

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

/* ------------------------------------------------------------------ */
/* R32+R44: 工作区/已删除会话过滤 + 控制台会话持久名单                  */
/* ------------------------------------------------------------------ */
/* dsh 界面删除的会话记在全局 archivedSessionIds，界面列表按工作区分组；
 * 而 sessionQuery.listSessions() 返回磁盘语料全量、不做任何过滤。
 * R44 起控制台直接对齐原生语义（数据源 = dsh-workspace 的
 * ctx.workspaceRegistry 服务，文件只作兜底）：
 * 已归档（已删除）一律不显示；不属于任何工作区的孤儿持久会话不显示；
 * 任一工作区会话 / 正在运行（live）/ 控制台创建的会话正常显示，
 * 并按「工作区分组 + 消息区」输出给手机端。 */

/* R44: 原生工作区服务直达——归档与分组与桌面同一套语义、同一份数据。
 * 手动改 workspace.json 会被运行中注册表的下一次 setState 覆盖，
 * 所以归档/挂载一律走服务方法，不写文件。 */
function wsRegistry(ctx) {
  const r = svc(ctx, "workspaceRegistry");
  return r && typeof r.list === "function" ? r : null;
}
/** 当前全部原生工作区快照；服务不可用返回 null（调用方走文件兜底）。 */
function nativeWorkspaces(ctx) {
  const reg = wsRegistry(ctx);
  if (!reg) return null;
  try {
    return reg.list().map((w) => ({
      id: String(w.id),
      title: String(w.title || ""),
      path: String(w.path || ""),
      sessionIds: (w.sessionIds || []).map(String),
      updatedAt: w.updatedAt || null,
    }));
  } catch (e) {
    log("workspaceRegistry.list failed: " + String((e && e.message) || e));
    return null;
  }
}
function normCwd(p) {
  return String(p || "").replace(/\//g, "\\").replace(/\\+$/, "").toLowerCase();
}
/** cwd 命中哪个已注册工作区（分隔符/大小写归一后精确相等）。 */
function workspaceOfCwd(wsList, cwd) {
  const n = normCwd(cwd);
  if (!n || !wsList) return null;
  for (const w of wsList) if (normCwd(w.path) === n) return w;
  return null;
}
/** cwd 是否落在手机消息区根（playground）之下。 */
function isZoneCwd(cwd) {
  const n = normCwd(cwd);
  const root = normCwd(REMOTE_ZONE_ROOT);
  return !!n && (n === root || n.startsWith(root + "\\"));
}

const WS_JSON = "F:\\deepseek-harness\\home\\storages\\workspace.json";
let _wsCache = { mtimeMs: -1, data: null };
function readWorkspaceIndex(ctx) {
  // 优先原生服务（与桌面同一权威数据源）；不可用时回退读 workspace.json，
  // 并集取全部工作区的 sessionIds（R44 前只取 updatedAt 最大者）。
  const wsList = nativeWorkspaces(ctx);
  if (wsList) {
    let archived = new Set();
    try {
      archived = new Set((wsRegistry(ctx).archivedSessionIds || []).map(String));
    } catch (_) {}
    const anyIds = new Set();
    for (const w of wsList) for (const id of w.sessionIds) anyIds.add(String(id));
    return { archived, anyIds, wsList };
  }
  try {
    const st = fs.statSync(WS_JSON);
    if (_wsCache.data && _wsCache.mtimeMs === st.mtimeMs) return _wsCache.data;
    const raw = JSON.parse(fs.readFileSync(WS_JSON, "utf8"));
    const g = (raw && raw.global) || {};
    const wsTable = ((raw && raw.tables) || {}).workspaces || {};
    const wsFileList = [];
    const anyIds = new Set();
    for (const k of Object.keys(wsTable)) {
      const w = wsTable[k] || {};
      const ids = (w.sessionIds || []).map(String);
      for (const id of ids) anyIds.add(id);
      wsFileList.push({
        id: String(k),
        title: String(w.title || ""),
        path: String(w.path || ""),
        sessionIds: ids,
        updatedAt: w.updatedAt || null,
      });
    }
    const data = {
      archived: new Set((g.archivedSessionIds || []).map(String)),
      anyIds,
      wsList: wsFileList,
    };
    _wsCache = { mtimeMs: st.mtimeMs, data };
    return data;
  } catch (_) {
    return null; /* 索引不可读时不过滤（fail-open），避免误杀全部会话 */
  }
}
function isSessionVisible(id, flags, ws) {
  if (!id) return false;
  if (!ws) return true;
  if (ws.archived.has(id)) return false;
  return ws.anyIds.has(id) || !!((flags && flags.live) || isPhoneCreated(id));
}

/* R32: 控制台创建的会话持久名单——dsh 重启后仍能在手机端列出/读取。 */
const PHONE_SESSIONS_FILE = "F:\\llm_hub\\remote_console_sessions.json";
let _phoneIds = null;
function phoneIds() {
  if (_phoneIds) return _phoneIds;
  try {
    _phoneIds = new Set(JSON.parse(fs.readFileSync(PHONE_SESSIONS_FILE, "utf8")).map(String));
  } catch (_) {
    _phoneIds = new Set();
  }
  return _phoneIds;
}
function isPhoneCreated(id) {
  const k = String(id || "");
  return liveAgents.has(k) || phoneIds().has(k);
}
function notePhoneSession(id) {
  try {
    const ids = phoneIds();
    ids.add(String(id || ""));
    fs.writeFileSync(PHONE_SESSIONS_FILE, JSON.stringify([...ids], null, 1), "utf8");
    _phoneIds = ids;
  } catch (e) {
    log("notePhoneSession failed: " + String((e && e.message) || e));
  }
}

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

/**
 * R39 懒挂载：把任意可见会话挂进 liveAgents。
 * 1) 进程内已有活跃 agent（如桌面 web 正开着该会话）→ 直接复用其 Agent
 *    （借用，不持有 handle，不能 dispose）；
 * 2) 否则 agents.resume() 从持久化存储重建 agent（桌面已关闭、或 dsh 重启
 *    后的手机会话，都走这条路恢复）。
 * 返回 { rec } 或 { status, error }。
 */
async function attachAgent(ctx, sessionId) {
  const agents = svc(ctx, "agents");
  if (!agents) return { status: 503, error: "agents 服务不可用" };
  try {
    const existing = typeof agents.get === "function" ? agents.get(sessionId) : null;
    if (existing && typeof existing.followup === "function") {
      const rec = {
        handle: null,
        agent: existing,
        title: "",
        createdAt: new Date().toISOString(),
        lastPromptAt: null,
        busy: false,
        error: "",
      };
      liveAgents.set(sessionId, rec);
      log("attach: reused live agent " + sessionId);
      return { rec };
    }
  } catch (_) {}
  try {
    const handle = await agents.resume({ resumeSessionId: sessionId });
    const agent = handle && handle.agent;
    if (!agent || typeof agent.followup !== "function") {
      try {
        if (handle && typeof handle.dispose === "function") await handle.dispose();
      } catch (_) {}
      return { status: 409, error: "会话恢复失败（句柄异常）" };
    }
    const rec = {
      handle,
      agent,
      title: "",
      createdAt: new Date().toISOString(),
      lastPromptAt: null,
      busy: false,
      error: "",
    };
    liveAgents.set(sessionId, rec);
    log("attach: resumed persisted session " + sessionId);
    return { rec };
  } catch (e) {
    const msg = String((e && e.message) || e);
    log("attach: resume failed " + sessionId + " " + msg);
    return { status: 409, error: "会话恢复失败：" + msg };
  }
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
          const wsIdx = readWorkspaceIndex(ctx);
          const all = await sq.listSessions();
          const records = wsIdx
            ? all.filter((r) => {
                const id = String((r && r.header && r.header.id) || "");
                return isSessionVisible(id, { live: !!(r && r.live) }, wsIdx);
              })
            : all;
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
            // R44: 工作区分组——cwd 命中已注册工作区路径的归该工作区；
            // 未命中但在 playground 根下的归「消息区」（手机默认窗口）。
            const wsOf = wsIdx ? workspaceOfCwd(wsIdx.wsList, h.cwd) : null;
            rows.push({
              id: String(h.id || ""),
              title: title || h.title || "",
              live: !!(r && r.live),
              persisted: !!(r && r.persisted),
              cwd: h.cwd || "",
              createdAt: h.createdAt || null,
              updatedAt: h.updatedAt || null,
              phoneCreated: isPhoneCreated(String(h.id || "")),
              workspaceId: wsOf ? wsOf.id : null,
              workspaceTitle: wsOf ? wsOf.title : null,
              zone: !wsOf && isZoneCwd(h.cwd || ""),
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

  /* R44: 原生工作区列表（仅 PC 端可创建）——手机端按此做工作区分组，
   * 并可挑选某个工作区路径作为 session-create 的 cwd 把会话建进去。 */
  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/remote-agent/api/dsh/workspaces",
      handler: async (req, res) => {
        if (unauthorized(req, res)) return;
        try {
          const wsIdx = readWorkspaceIndex(ctx);
          const list = (wsIdx && wsIdx.wsList) || [];
          send(res, 200, {
            ok: true,
            data: {
              zoneRoot: REMOTE_ZONE_ROOT,
              workspaces: list.map((w) => ({
                id: w.id,
                title: w.title,
                path: w.path,
                sessionIds: w.sessionIds,
                updatedAt: w.updatedAt,
              })),
            },
          });
        } catch (e) {
          log("dsh/workspaces error: " + String((e && e.message) || e));
          send(res, 500, { ok: false, error: String((e && e.message) || e) });
        }
      },
    })
  );

  /* R44: 归档会话——走原生 workspaceRegistry.archiveSession()，与桌面「删除」
   * 完全同一语义：写入全局 archivedSessionIds，桌面与手机同时消失；
   * 会话本体与日志保留在磁盘（原生语义，不动工作区账务，可恢复）。 */
  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/remote-agent/api/dsh/session-archive",
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
        if (!sessionId || !/^[A-Za-z0-9_-]+$/.test(sessionId)) {
          send(res, 400, { ok: false, error: "bad session id" });
          return;
        }
        try {
          const reg = wsRegistry(ctx);
          if (!reg || typeof reg.archiveSession !== "function") {
            send(res, 503, { ok: false, error: "工作区服务不可用，请在桌面端删除该会话" });
            return;
          }
          await reg.archiveSession(sessionId);
          log("remote session archived: " + sessionId);
          send(res, 200, { ok: true, data: { sessionId, archived: true } });
        } catch (e) {
          const msg = String((e && e.message) || e);
          if (/cannot archive session/.test(msg)) {
            send(res, 404, { ok: false, error: "会话不存在（可能已被删除）" });
            return;
          }
          log("dsh/session-archive error: " + msg);
          send(res, 500, { ok: false, error: msg });
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
          const wsIdx = readWorkspaceIndex(ctx);
          let liveNow = false;
          try {
            const ss = svc(ctx, "sessions");
            if (ss && typeof ss.get === "function") liveNow = !!ss.get(sid);
          } catch (_) {}
          if (!isSessionVisible(sid, { live: liveNow }, wsIdx)) {
            send(res, 404, { ok: false, error: "该会话已删除或不在当前工作区，无法读取" });
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

  /* R45: 模型目录——对齐 dsh 官方 buildModelCatalog：llm.listProviders() 逐个
   * listModels，单 provider 失败降级为 failures 条目，不拖垮整体。 */
  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/remote-agent/api/dsh/models",
      handler: async (req, res) => {
        if (unauthorized(req, res)) return;
        try {
          const llm = svc(ctx, "llm");
          if (!llm || typeof llm.listProviders !== "function") {
            send(res, 503, { ok: false, error: "llm 服务不可用" });
            return;
          }
          let defSel = { provider: "", model: "" };
          try {
            const dm = svc(ctx, "agentDefaultModel");
            if (dm && typeof dm.currentSelection === "function") {
              const s = dm.currentSelection();
              defSel = { provider: (s && s.provider) || "", model: (s && s.model) || "" };
            }
          } catch (_) {}
          const groups = [];
          const failures = [];
          await Promise.all(
            llm.listProviders().map(async (p) => {
              try {
                const models = await llm.listModels(p.id);
                const entries = (models || [])
                  .map((m) => ({ id: String(m.id || ""), name: String(m.name || m.id || "") }))
                  .filter((m) => m.id);
                if (entries.length > 0) groups.push({ id: String(p.id), name: String(p.name || p.id), models: entries });
              } catch (e) {
                failures.push({ id: String(p.id), message: String((e && e.message) || e) });
              }
            })
          );
          send(res, 200, { ok: true, data: { default: defSel, groups, failures } });
        } catch (e) {
          log("dsh/models error: " + String((e && e.message) || e));
          send(res, 500, { ok: false, error: String((e && e.message) || e) });
        }
      },
    })
  );

  /* R45: 切换模型——对齐 dsh 官方 selectModel 语义：resolveCallConfig 校验可服务，
   * saveSelection 持久化默认（后续新会话生效）；带 sessionId 时同时
   * agents.selectForNextRequest 改该会话下一次请求。 */
  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/remote-agent/api/dsh/model-set",
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
        const provider = String(body.provider || "");
        const model = String(body.model || "");
        if (!provider || !model) {
          send(res, 400, { ok: false, error: "provider 与 model 均必填" });
          return;
        }
        try {
          const llm = svc(ctx, "llm");
          const dm = svc(ctx, "agentDefaultModel");
          if (!llm || typeof llm.resolveCallConfig !== "function") {
            send(res, 503, { ok: false, error: "llm 服务不可用" });
            return;
          }
          if (!dm || typeof dm.saveSelection !== "function") {
            send(res, 503, { ok: false, error: "agentDefaultModel 服务不可用" });
            return;
          }
          const resolved = await llm.resolveCallConfig({ provider, model });
          const selected = { provider: resolved.provider, model: resolved.model };
          if (resolved.reasoningEffort !== undefined) selected.reasoningEffort = resolved.reasoningEffort;
          const sid = String(body.sessionId || "");
          if (sid && /^[A-Za-z0-9_-]+$/.test(sid)) {
            try {
              const attached = await attachAgent(ctx, sid);
              const agentsSvc = svc(ctx, "agents");
              if (attached.rec && agentsSvc && typeof agentsSvc.selectForNextRequest === "function") {
                agentsSvc.selectForNextRequest(attached.rec.agent, selected);
                log("model-set next-request: " + sid + " -> " + selected.provider + "/" + selected.model);
              }
            } catch (e) {
              log("model-set next-request failed: " + sid + " " + String((e && e.message) || e));
            }
          }
          await dm.saveSelection(selected);
          log("model-set: default -> " + selected.provider + "/" + selected.model);
          send(res, 200, { ok: true, data: { selected } });
        } catch (e) {
          log("dsh/model-set error: " + String((e && e.message) || e));
          send(res, 500, { ok: false, error: String((e && e.message) || e) });
        }
      },
    })
  );

  /* R45: 会话快捷指令列表——commands.list(agent)（/compact、/plan 等）。 */
  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/remote-agent/api/dsh/commands",
      handler: async (req, res) => {
        if (unauthorized(req, res)) return;
        try {
          const url = new URL(req.url || "/remote-agent/api/dsh/commands", "http://127.0.0.1");
          const sid = String(url.searchParams.get("id") || "");
          if (!sid || !/^[A-Za-z0-9_-]+$/.test(sid)) {
            send(res, 400, { ok: false, error: "bad session id" });
            return;
          }
          const cmds = svc(ctx, "commands");
          if (!cmds || typeof cmds.list !== "function") {
            send(res, 503, { ok: false, error: "commands 服务不可用" });
            return;
          }
          const wsIdx = readWorkspaceIndex(ctx);
          let liveNow = false;
          try {
            const ss = svc(ctx, "sessions");
            if (ss && typeof ss.get === "function") liveNow = !!ss.get(sid);
          } catch (_) {}
          if (!isSessionVisible(sid, { live: liveNow }, wsIdx)) {
            send(res, 404, { ok: false, error: "该会话已删除或不在当前工作区" });
            return;
          }
          const attached = await attachAgent(ctx, sid);
          if (!attached.rec) {
            send(res, attached.status || 409, { ok: false, error: attached.error });
            return;
          }
          const list = cmds.list(attached.rec.agent) || [];
          send(res, 200, {
            ok: true,
            data: list.map((c) => ({
              name: String(c.name || ""),
              description: String(c.description || ""),
              hint: String((c.input && c.input.hint) || ""),
            })),
          });
        } catch (e) {
          log("dsh/commands error: " + String((e && e.message) || e));
          send(res, 500, { ok: false, error: String((e && e.message) || e) });
        }
      },
    })
  );

  /* R45: 执行快捷指令——commands.execute(agent, line)；结果 {kind, text} 直接回给手机。 */
  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/remote-agent/api/dsh/command",
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
        const line = String(body.line || "").trim();
        if (!sessionId || !/^[A-Za-z0-9_-]+$/.test(sessionId)) {
          send(res, 400, { ok: false, error: "bad session id" });
          return;
        }
        if (!line || line.length > 2000) {
          send(res, 400, { ok: false, error: "line 必填" });
          return;
        }
        try {
          const cmds = svc(ctx, "commands");
          if (!cmds || typeof cmds.execute !== "function") {
            send(res, 503, { ok: false, error: "commands 服务不可用" });
            return;
          }
          const wsIdx = readWorkspaceIndex(ctx);
          let liveNow = false;
          try {
            const ss = svc(ctx, "sessions");
            if (ss && typeof ss.get === "function") liveNow = !!ss.get(sessionId);
          } catch (_) {}
          if (!isSessionVisible(sessionId, { live: liveNow }, wsIdx)) {
            send(res, 404, { ok: false, error: "该会话已删除或不在当前工作区" });
            return;
          }
          const attached = await attachAgent(ctx, sessionId);
          if (!attached.rec) {
            send(res, attached.status || 409, { ok: false, error: attached.error });
            return;
          }
          log("dsh/command: " + sessionId + " " + line.slice(0, 80));
          const exec = await cmds.execute(attached.rec.agent, line, []);
          if (!exec) {
            send(res, 404, { ok: false, error: "未知指令或格式不符：" + line.slice(0, 60) });
            return;
          }
          const r = exec.result || {};
          send(res, 200, {
            ok: true,
            data: { commandId: exec.commandId || null, kind: r.kind || "success", text: r.text || "" },
          });
        } catch (e) {
          log("dsh/command error: " + String((e && e.message) || e));
          send(res, 500, { ok: false, error: String((e && e.message) || e) });
        }
      },
    })
  );

  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/remote-agent/api/status",
      handler: async (req, res) => {
        if (unauthorized(req, res)) return;
        let model = { provider: "", model: "" };
        try {
          const dm = svc(ctx, "agentDefaultModel");
          if (dm && typeof dm.currentSelection === "function") {
            const sel = dm.currentSelection();
            model = { provider: (sel && sel.provider) || "", model: (sel && sel.model) || "" };
          }
        } catch (_) {}
        send(res, 200, {
          ok: true,
          data: {
            uptimeSec: Math.floor(process.uptime()),
            activeCount: liveAgents.size,
            model,
            now: new Date().toISOString(),
          },
        });
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
            agent,
            title: String(body.title || "").slice(0, 80),
            createdAt: new Date().toISOString(),
            lastPromptAt: new Date().toISOString(),
            busy: false,
            error: "",
          });
          notePhoneSession(sessionId);
          // R44: cwd 命中 PC 端已注册工作区 → 挂进该工作区（桌面立即可见、手机按工作区分组）。
          // 未命中（消息区目录）不创建工作区——原生工作区只能 PC 端创建。
          const reg = wsRegistry(ctx);
          if (reg) {
            try {
              const wsOfNew = typeof reg.resolveByPath === "function" ? await reg.resolveByPath(cwd) : null;
              if (wsOfNew && typeof wsOfNew.attachSession === "function") {
                await wsOfNew.attachSession(sessionId);
                log("attached to workspace: " + sessionId + " -> " + String(wsOfNew.path || ""));
              }
            } catch (e) {
              log("workspace attach failed: " + sessionId + " " + String((e && e.message) || e));
            }
          }
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
            agent.followup(
              createUserMessage([{ type: "text", text: prompt }], { kind: "user" })
            );
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
        let rec = liveAgents.get(sessionId);
        if (!rec || !rec.agent) {
          // R39 懒挂载：手机可直接续聊任意可见会话，不再限于手机入口创建
          const wsIdx = readWorkspaceIndex(ctx);
          let liveNow = false;
          try {
            const ss = svc(ctx, "sessions");
            if (ss && typeof ss.get === "function") liveNow = !!ss.get(sessionId);
          } catch (_) {}
          if (!isSessionVisible(sessionId, { live: liveNow }, wsIdx)) {
            send(res, 404, { ok: false, error: "该会话已删除或不在当前工作区，无法续聊" });
            return;
          }
          const attached = await attachAgent(ctx, sessionId);
          if (!attached.rec) {
            send(res, attached.status || 409, { ok: false, error: attached.error });
            return;
          }
          rec = attached.rec;
        }
        if (rec.busy) {
          send(res, 409, { ok: false, error: "会话忙（上一轮未完成），可先取消" });
          return;
        }
        try {
          const agent = rec.agent;
          rec.busy = true;
          rec.error = "";
          rec.lastPromptAt = new Date().toISOString();
          agent.followup(
            createUserMessage([{ type: "text", text: prompt }], { kind: "user" })
          );
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
        if (!rec || !rec.agent) {
          send(res, 404, { ok: false, error: "活跃会话不存在（手机侧尚未挂载，无可取消的生成）" });
          return;
        }
        const how = await interruptAgent(rec.agent);
        rec.error = how ? "" : "未找到可用的中断方法";
        send(res, 200, { ok: true, data: { sessionId, interruptedVia: how } });
      },
    })
  );
}

export { apply, inject };
//#endregion
