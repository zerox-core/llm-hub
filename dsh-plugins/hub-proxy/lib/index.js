//#region @dsh-local/hub-proxy — node half
/**
 * Same-origin proxy for the LLM Key Hub harness pin API.
 *
 * Why: the dsh page (https://dsh.zeroxcore.tech) cannot call
 * https://hub.zeroxcore.tech directly — the browser's CORS preflight (OPTIONS,
 * no credentials) is rejected by nginx auth_basic (401), and the upstream CORS
 * allowlist does not include the dsh origin ("Disallowed CORS origin").
 * So hub_model_card.js calls this same-origin route instead; the hub admin
 * credential lives only server-side, never in the served page JS.
 *
 *   GET/POST /hub-proxy/api/harness/model  ->  https://hub.zeroxcore.tech/api/harness/model
 */

const HUB_BASE = "https://hub.zeroxcore.tech";
const HUB_AUTH = "Basic " + Buffer.from("hubadmin:f844c52d260ac865").toString("base64");
const TIMEOUT_MS = 10000;

const inject = ["webServer"];

function send(res, status, obj) {
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
  });
  res.end(JSON.stringify(obj));
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

async function forward(req, res) {
  try {
    const init = {
      method: req.method === "POST" ? "POST" : "GET",
      headers: { authorization: HUB_AUTH },
      signal: AbortSignal.timeout(TIMEOUT_MS),
    };
    if (init.method === "POST") {
      init.headers["content-type"] = "application/json";
      init.body = await readBody(req);
    }
    const r = await fetch(HUB_BASE + "/api/harness/model", init);
    const text = await r.text();
    res.writeHead(r.status, {
      "content-type": r.headers.get("content-type") || "application/json; charset=utf-8",
      "cache-control": "no-store",
    });
    res.end(text);
  } catch (e) {
    send(res, 502, { ok: false, error: "hub-proxy upstream: " + String((e && e.message) || e) });
  }
}

function apply(ctx) {
  ctx.effect(() =>
    ctx.webServer.register({
      kind: "exact",
      path: "/hub-proxy/api/harness/model",
      handler: async (req, res) => {
        if (req.method !== "GET" && req.method !== "POST") {
          send(res, 405, { ok: false, error: "GET/POST only" });
          return;
        }
        await forward(req, res);
      },
    })
  );
}

export { apply, inject };
