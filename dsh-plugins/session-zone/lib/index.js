//#region @dsh-local/session-zone — node half
/**
 * 会话区（playground 时间分区）后端：同源 HTTP 路由挂在 dsh web server 上。
 *
 *   GET  /zone/api/list      列出会话区内的工作区（path 位于 ZONE_ROOT 下），
 *                            每项带 project 归属标记（方案 A 纯标记，2026-09-22 拍板）
 *   POST /zone/api/create    建 YYYY.MMDD-HH.mm[(n)] 子目录 + workspaceRegistry.create
 *   POST /zone/api/delete    {workspaceId, deleteFiles} 归档会话 + 删工作区注册 +
 *                            删会话存储（session.v3.jsonl.zstd 与 projcache），
 *                            deleteFiles=true 时连 cwd 文件夹一起删
 *   GET  /zone/api/projects  列出非会话区工作区（归属候选目标）
 *   POST /zone/api/assign    {workspaceId, projectId} 标记会话分区归属到项目工作区
 *   POST /zone/api/unassign  {workspaceId} 取消归属标记
 *
 * 命名规则（用户 2026-09-22 拍板）：按时间分区 YYYY.MMDD-HH.mm，
 * 同一分钟内冲突追加 (1) (2) ... 后缀。
 *
 * 归属规则（用户 2026-09-22 拍板，方案 A）：纯标记——不改会话 cwd、不搬存储、
 * 不动文件，只在 .zone-attribution.json 记 zoneWorkspaceId -> projectWorkspaceId；
 * dsh 原生视角会话仍属于会话区工作区（原生 attach 强制校验 header.cwd canon）。
 *
 * 安全边界：delete 只放行 ZONE_ROOT 下的工作区；assign 只放行 ZONE_ROOT 下的
 * 工作区且目标必须在 ZONE_ROOT 之外；会话存储目录按 sessionId 精确匹配目录名。
 */

import fs from "node:fs";
import path from "node:path";

const ZONE_ROOT = "F:\\deepseek-harness\\playground";
const HOME_DIR = "F:\\deepseek-harness\\home";
const SESSIONS_DIR = HOME_DIR + "\\sessions";
const PROJCACHE_DIR = HOME_DIR + "\\storages\\session_projcache\\sessions";
const ATTR_FILE = ZONE_ROOT + "\\.zone-attribution.json";

/** 会话日志 30 秒内还有写入就拒绝删除（防止删到正在进行的会话）。 */
const ACTIVE_GRACE_MS = 30000;

const inject = ["webServer", "workspaceRegistry"];

function pad(n) {
  return String(n).padStart(2, "0");
}

/** 基础名：2026.0922-22.02（服务器本地时间）。 */
function zoneBaseName(d) {
  return (
    d.getFullYear() +
    "." +
    pad(d.getMonth() + 1) +
    pad(d.getDate()) +
    "-" +
    pad(d.getHours()) +
    "." +
    pad(d.getMinutes())
  );
}

/** 同分钟冲突时追加 (1) (2) ... 直到不撞名。 */
function uniqueZoneDir() {
  const base = zoneBaseName(new Date());
  for (let i = 0; i < 100; i++) {
    const name = i === 0 ? base : base + "(" + i + ")";
    const dir = path.join(ZONE_ROOT, name);
    if (!fs.existsSync(dir)) return { name, dir };
  }
  const name = base + "(" + Date.now() + ")";
  return { name, dir: path.join(ZONE_ROOT, name) };
}

/** path 是否位于 ZONE_ROOT 之内（大小写不敏感，带分隔符边界）。 */
function isUnderZone(p) {
  const norm = path.resolve(String(p || "")).toLowerCase();
  const root = path.resolve(ZONE_ROOT).toLowerCase();
  return norm.startsWith(root + path.sep);
}

/** 归属映射读写（zoneWorkspaceId -> projectWorkspaceId），坏文件当空处理。 */
function loadAttr() {
  try {
    const m = JSON.parse(fs.readFileSync(ATTR_FILE, "utf8"));
    return m && typeof m === "object" && !Array.isArray(m) ? m : {};
  } catch {
    return {};
  }
}

function saveAttr(m) {
  fs.mkdirSync(ZONE_ROOT, { recursive: true });
  fs.writeFileSync(ATTR_FILE, JSON.stringify(m, null, 1), "utf8");
}

/** 在 home\sessions 下按目录名精确找 session-<uuid>（两层：--slug--\session-uuid）。 */
function findSessionDirs(sessionId) {
  const out = [];
  let lvl1;
  try {
    lvl1 = fs.readdirSync(SESSIONS_DIR, { withFileTypes: true });
  } catch {
    return out;
  }
  for (const e1 of lvl1) {
    if (!e1.isDirectory()) continue;
    const sub = path.join(SESSIONS_DIR, e1.name);
    let lvl2;
    try {
      lvl2 = fs.readdirSync(sub, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const e2 of lvl2) {
      if (e2.isDirectory() && e2.name === sessionId) {
        out.push(path.join(sub, e2.name));
      }
    }
  }
  return out;
}

/** 目录树里最近一次的 mtime（用于活跃启发式），找不到文件返回 0。 */
function latestMtime(dir) {
  let latest = 0;
  const stack = [dir];
  let guard = 0;
  while (stack.length && guard++ < 500) {
    const cur = stack.pop();
    let ents;
    try {
      ents = fs.readdirSync(cur, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const e of ents) {
      const p = path.join(cur, e.name);
      if (e.isDirectory()) {
        stack.push(p);
      } else {
        try {
          const m = fs.statSync(p).mtimeMs;
          if (m > latest) latest = m;
        } catch {
          /* ignore */
        }
      }
    }
  }
  return latest;
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (c) => {
      data += c;
      if (data.length > 65536) {
        reject(new Error("body too large"));
        req.destroy();
      }
    });
    req.on("end", () => resolve(data));
    req.on("error", reject);
  });
}

/** 2026-09-24：从 projcache 元数据读会话标题（record.rows.title.val，回退 titleInput.val.first）。
 *  供侧栏分区行同步显示主面板的真实会话标题；读不到返回 null。 */
function readSessionTitle(sessionId) {
  try {
    const p = path.join(PROJCACHE_DIR, sessionId + ".json");
    if (!fs.existsSync(p)) return null;
    const j = JSON.parse(fs.readFileSync(p, "utf8"));
    const rows = j && j.record && j.record.rows;
    if (!rows) return null;
    const t = rows.title && rows.title.val;
    if (typeof t === "string" && t.trim()) return t.trim();
    const f = rows.titleInput && rows.titleInput.val && rows.titleInput.val.first;
    if (typeof f === "string" && f.trim()) return f.trim();
    return null;
  } catch {
    return null;
  }
}

function apply(ctx) {
  const send = (res, code, obj) => {
    res.writeHead(code, {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    });
    res.end(JSON.stringify(obj));
  };

  const allById = () => {
    const byId = {};
    for (const w of ctx.workspaceRegistry.list()) byId[String(w.id)] = w;
    return byId;
  };

  /** 归属目标校验：映射里的 projectId 仍存在且在会话区外才算有效。 */
  const resolveProject = (byId, pid) => {
    const pw = pid ? byId[String(pid)] : null;
    return pw && !isUnderZone(pw.path)
      ? { id: String(pw.id), title: pw.title, path: pw.path }
      : null;
  };

  const zoneList = () => {
    const attr = loadAttr();
    const byId = allById();
    return ctx.workspaceRegistry
      .list()
      .filter((w) => isUnderZone(w.path))
      .map((w) => {
        const sessionIds = w.sessionIds.map((s) => String(s));
        // 2026-09-24：取第一个有标题的会话标题，供侧栏分区行同步显示
        let sessionTitle = null;
        for (const sid of sessionIds) {
          sessionTitle = readSessionTitle(sid);
          if (sessionTitle) break;
        }
        return {
          id: String(w.id),
          path: w.path,
          title: w.title,
          createdAt: w.createdAt,
          sessionIds,
          sessionTitle,
          project: resolveProject(byId, attr[String(w.id)]),
        };
      });
  };

  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/zone/api/list",
      handler: async (_req, res) => {
        try {
          send(res, 200, { ok: true, zoneRoot: ZONE_ROOT, workspaces: zoneList() });
        } catch (e) {
          send(res, 500, { ok: false, error: String((e && e.message) || e) });
        }
      },
    })
  );

  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/zone/api/projects",
      handler: async (_req, res) => {
        try {
          const projects = ctx.workspaceRegistry
            .list()
            .filter((w) => !isUnderZone(w.path))
            .map((w) => ({ id: String(w.id), title: w.title, path: w.path }));
          send(res, 200, { ok: true, projects });
        } catch (e) {
          send(res, 500, { ok: false, error: String((e && e.message) || e) });
        }
      },
    })
  );

  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/zone/api/assign",
      handler: async (req, res) => {
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
        const workspaceId = String(body.workspaceId || "");
        const projectId = String(body.projectId || "");
        if (!workspaceId || !projectId) {
          send(res, 400, { ok: false, error: "workspaceId 与 projectId 均必填" });
          return;
        }
        try {
          const ws = ctx.workspaceRegistry.get(workspaceId);
          if (!ws) {
            send(res, 404, { ok: false, error: "workspace not found" });
            return;
          }
          if (!isUnderZone(ws.path)) {
            send(res, 403, { ok: false, error: "仅会话区内的工作区可标记归属" });
            return;
          }
          const pw = ctx.workspaceRegistry.get(projectId);
          if (!pw) {
            send(res, 404, { ok: false, error: "project workspace not found" });
            return;
          }
          if (isUnderZone(pw.path)) {
            send(res, 400, { ok: false, error: "不能归属到另一个会话分区" });
            return;
          }
          const attr = loadAttr();
          attr[String(ws.id)] = String(pw.id);
          saveAttr(attr);
          send(res, 200, {
            ok: true,
            workspaceId: String(ws.id),
            project: { id: String(pw.id), title: pw.title, path: pw.path },
          });
        } catch (e) {
          send(res, 500, { ok: false, error: String((e && e.message) || e) });
        }
      },
    })
  );

  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/zone/api/unassign",
      handler: async (req, res) => {
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
        const workspaceId = String(body.workspaceId || "");
        if (!workspaceId) {
          send(res, 400, { ok: false, error: "workspaceId required" });
          return;
        }
        try {
          const attr = loadAttr();
          const had = Object.hasOwn(attr, workspaceId);
          delete attr[workspaceId];
          saveAttr(attr);
          send(res, 200, { ok: true, workspaceId, removed: had });
        } catch (e) {
          send(res, 500, { ok: false, error: String((e && e.message) || e) });
        }
      },
    })
  );

  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/zone/api/create",
      handler: async (req, res) => {
        if (req.method !== "POST") {
          send(res, 405, { ok: false, error: "POST only" });
          return;
        }
        try {
          fs.mkdirSync(ZONE_ROOT, { recursive: true });
          const { name, dir } = uniqueZoneDir();
          fs.mkdirSync(dir, { recursive: true });
          const ws = await ctx.workspaceRegistry.create(dir, name);
          send(res, 200, {
            ok: true,
            workspace: { id: String(ws.id), path: ws.path, title: ws.title },
          });
        } catch (e) {
          send(res, 500, { ok: false, error: String((e && e.message) || e) });
        }
      },
    })
  );

  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/zone/api/delete",
      handler: async (req, res) => {
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
        const workspaceId = String(body.workspaceId || "");
        const deleteFiles = body.deleteFiles === true;
        if (!workspaceId) {
          send(res, 400, { ok: false, error: "workspaceId required" });
          return;
        }
        try {
          const ws = ctx.workspaceRegistry.get(workspaceId);
          if (!ws) {
            send(res, 404, { ok: false, error: "workspace not found" });
            return;
          }
          if (!isUnderZone(ws.path)) {
            send(res, 403, { ok: false, error: "仅允许删除会话区内的工作区" });
            return;
          }
          const sessionIds = ws.sessionIds.map((s) => String(s));

          // 活跃保护：任一会话目录 30 秒内仍有写入则拒绝
          for (const sid of sessionIds) {
            for (const dir of findSessionDirs(sid)) {
              const m = latestMtime(dir);
              if (m && Date.now() - m < ACTIVE_GRACE_MS) {
                send(res, 409, {
                  ok: false,
                  error: "会话可能仍在进行中（30 秒内有写入），请先关闭或稍后再删",
                  sessionId: sid,
                });
                return;
              }
            }
          }

          // 1) 归档（从所有分组表面隐藏，幂等）
          const archiveErrors = [];
          for (const sid of sessionIds) {
            try {
              await ctx.workspaceRegistry.archiveSession(sid);
            } catch (e) {
              archiveErrors.push(sid + ": " + String((e && e.message) || e));
            }
          }
          // 2) 删工作区注册（目录与会话日志由下面手动清）
          await ctx.workspaceRegistry.delete(ws.id);
          // 3) 删会话存储 + projcache 元数据
          const removed = [];
          for (const sid of sessionIds) {
            for (const dir of findSessionDirs(sid)) {
              fs.rmSync(dir, { recursive: true, force: true });
              removed.push(dir);
            }
            const pc = path.join(PROJCACHE_DIR, sid + ".json");
            if (fs.existsSync(pc)) {
              fs.rmSync(pc, { force: true });
              removed.push(pc);
            }
          }
          // 4) 文件夹：deleteFiles 全删；否则仅当空目录时顺手收掉
          let folderRemoved = false;
          if (deleteFiles) {
            fs.rmSync(ws.path, { recursive: true, force: true });
            folderRemoved = true;
          } else {
            try {
              fs.rmdirSync(ws.path); // 仅空目录成功
              folderRemoved = true;
            } catch {
              /* 非空，保留 */
            }
          }
          // 5) 顺手清掉这条归属标记
          const attr = loadAttr();
          if (Object.hasOwn(attr, workspaceId)) {
            delete attr[workspaceId];
            saveAttr(attr);
          }
          send(res, 200, {
            ok: true,
            workspaceId,
            title: ws.title,
            sessions: sessionIds,
            removedPaths: removed,
            folderRemoved,
            filesDeleted: deleteFiles,
            archiveErrors: archiveErrors.length ? archiveErrors : undefined,
          });
        } catch (e) {
          send(res, 500, { ok: false, error: String((e && e.message) || e) });
        }
      },
    })
  );
}

export { apply, inject };
//#endregion
