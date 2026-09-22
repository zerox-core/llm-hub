//#region @dsh-local/session-zone — node half
/**
 * 会话区（playground 时间分区）后端：同源 HTTP 路由挂在 dsh web server 上。
 *
 *   GET  /zone/api/list     列出会话区内的工作区（path 位于 ZONE_ROOT 下）
 *   POST /zone/api/create   建 YYYY.MMDD-HH.mm[(n)] 子目录 + workspaceRegistry.create
 *   POST /zone/api/delete   {workspaceId, deleteFiles} 归档会话 + 删工作区注册 +
 *                           删会话存储（session.v3.jsonl.zstd 与 projcache），
 *                           deleteFiles=true 时连 cwd 文件夹一起删
 *
 * 命名规则（用户 2026-09-22 拍板）：按时间分区 YYYY.MMDD-HH.mm，
 * 同一分钟内冲突追加 (1) (2) ... 后缀。
 *
 * 安全边界：delete 只放行 ZONE_ROOT 下的工作区；会话存储目录按 sessionId
 * 精确匹配目录名，不做模糊删除。
 */

import fs from "node:fs";
import path from "node:path";

const ZONE_ROOT = "F:\\deepseek-harness\\playground";
const HOME_DIR = "F:\\deepseek-harness\\home";
const SESSIONS_DIR = HOME_DIR + "\\sessions";
const PROJCACHE_DIR = HOME_DIR + "\\storages\\session_projcache\\sessions";

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

function apply(ctx) {
  const send = (res, code, obj) => {
    res.writeHead(code, {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    });
    res.end(JSON.stringify(obj));
  };

  const zoneList = () =>
    ctx.workspaceRegistry
      .list()
      .filter((w) => isUnderZone(w.path))
      .map((w) => ({
        id: String(w.id),
        path: w.path,
        title: w.title,
        createdAt: w.createdAt,
        sessionIds: w.sessionIds.map((s) => String(s)),
      }));

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
