//#region @dsh-local/dsh-updater — node half
/**
 * Registers same-origin HTTP routes on the dsh web server:
 *   GET  /dsh-updater/api/status   installed version vs npm registry dist-tags
 *   POST /dsh-updater/api/update   spawn detached restarter (npm install + patch + restart),
 *                                  respond, then exit so the restarter can bring dsh back up.
 * The UI lives in the dsh_patch injected hub_ui_kit.js; no CORS, no tokens page-side.
 */

import { spawn } from "node:child_process";
import fs from "node:fs";

const APP_DIR = "F:\\deepseek-harness\\app";
const PKG_JSON = APP_DIR + "\\node_modules\\@deepseek-ai\\dsh\\package.json";
const REGISTRY = "https://registry.npmjs.org/@deepseek-ai%2Fdsh";
const RESTARTER = "F:\\llm_hub\\dsh_patch\\dsh_restarter.py";
const PY_EXE = "C:\\Windows\\py.EXE";
const RESULT_FILE = "F:\\llm_hub\\_push\\dsh_update_result.json";

const inject = ["webServer"];

let cache = { at: 0, data: null };
let updateStarted = false;

function parseVer(v) {
  const m = /^(\d+)\.(\d+)\.(\d+)(?:-([a-zA-Z]+)\.?(\d+)?)?$/.exec(String(v || ""));
  if (!m) return null;
  const preRank = { alpha: 0, beta: 1, rc: 2 };
  return {
    n: [Number(m[1]), Number(m[2]), Number(m[3])],
    pre: m[4] ? (preRank[m[4].toLowerCase()] !== undefined ? preRank[m[4].toLowerCase()] : 0) : 3,
    preN: m[5] ? Number(m[5]) : 0,
  };
}

function cmpVer(a, b) {
  const x = parseVer(a);
  const y = parseVer(b);
  if (!x || !y) return 0;
  for (let i = 0; i < 3; i++) {
    if (x.n[i] !== y.n[i]) return x.n[i] < y.n[i] ? -1 : 1;
  }
  if (x.pre !== y.pre) return x.pre < y.pre ? -1 : 1;
  if (x.preN !== y.preN) return x.preN < y.preN ? -1 : 1;
  return 0;
}

function installedVersion() {
  try {
    return JSON.parse(fs.readFileSync(PKG_JSON, "utf8")).version || null;
  } catch {
    return null;
  }
}

async function registryInfo() {
  if (cache.data && Date.now() - cache.at < 120000) return cache.data;
  const r = await fetch(REGISTRY, { signal: AbortSignal.timeout(10000) });
  if (!r.ok) throw new Error("registry HTTP " + r.status);
  const j = await r.json();
  const tags = j["dist-tags"] || {};
  const time = j.time || {};
  const data = {
    latest: tags.latest || null,
    next: tags.next || null,
    alpha: tags.alpha || null,
    latestTime: tags.latest ? time[tags.latest] || null : null,
    nextTime: tags.next ? time[tags.next] || null : null,
    has: (v) => !!(j.versions && j.versions[v]),
  };
  cache = { at: Date.now(), data };
  return data;
}

function pickTarget(cur, reg) {
  // 优先 next（同版本线的下一个 rc），其次 latest；都不比当前新则不更新。
  for (const cand of [reg.next, reg.latest]) {
    if (cand && cur && cmpVer(cand, cur) > 0) return cand;
  }
  return null;
}

function readLastResult() {
  try {
    return JSON.parse(fs.readFileSync(RESULT_FILE, "utf8"));
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

  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/dsh-updater/api/status",
      handler: async (req, res) => {
        try {
          const current = installedVersion();
          const reg = await registryInfo();
          const target = current ? pickTarget(current, reg) : null;
          send(res, 200, {
            ok: true,
            current,
            latest: reg.latest,
            next: reg.next,
            alpha: reg.alpha,
            latestTime: reg.latestTime,
            nextTime: reg.nextTime,
            updateAvailable: !!target,
            target,
            updateStarted,
            lastUpdate: readLastResult(),
          });
        } catch (e) {
          send(res, 502, {
            ok: false,
            error: String((e && e.message) || e),
            current: installedVersion(),
          });
        }
      },
    })
  );

  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/dsh-updater/api/update",
      handler: async (req, res) => {
        if (req.method !== "POST") {
          send(res, 405, { ok: false, error: "POST only" });
          return;
        }
        try {
          if (updateStarted) {
            send(res, 409, { ok: false, error: "更新已在进行中" });
            return;
          }
          const current = installedVersion();
          const reg = await registryInfo();
          const target = current ? pickTarget(current, reg) : null;
          if (!target) {
            send(res, 200, { ok: true, noop: true, message: "已是最新", current });
            return;
          }
          updateStarted = true;
          const child = spawn(PY_EXE, ["-3", RESTARTER, "--target", target], {
            detached: true,
            stdio: "ignore",
            env: process.env,
          });
          child.unref();
          send(res, 200, { ok: true, from: current, target, restarting: true });
          setTimeout(() => process.exit(0), 800);
        } catch (e) {
          updateStarted = false;
          send(res, 500, { ok: false, error: String((e && e.message) || e) });
        }
      },
    })
  );
}

export { apply, inject };
//#endregion
