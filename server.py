# -*- coding: utf-8 -*-
"""LLM Key Hub - 本地大模型 Key / 额度管理工具（仅本机使用）

v3 新增：
- 允许付费开关（默认关：只测/只调有免费额度的模型）
- 模型排序（轮询按排序进行）+ 模型分类
- 删除单个模型 / 一键清理（过期 + 用尽 + 测试失败的模型）
- 统一轮询代理入口 POST /v1/chat/completions（model=auto 时按排序轮询免费额度模型）
- bl usage freetier 试用完即停开关
"""
import datetime
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data.json"
STATIC_DIR = BASE_DIR / "static"
LOG_FILE = BASE_DIR / "logs.jsonl"

HUB_BASE = "http://127.0.0.1:8787/v1"

_lock = threading.Lock()


# ---------------- 调用日志（JSONL 追加，约 2MB 滚动截断） ----------------

def log_call(entry):
    """落一条调用日志。字段：source(proxy/test), provider, model_requested, model_used,
    ok, status, latency_ms, prompt_tokens, completion_tokens, total_tokens,
    stream, rotated, blocked, error"""
    entry = dict(entry)
    entry["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if LOG_FILE.stat().st_size > 2 * 1024 * 1024:
            lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
            LOG_FILE.write_text("\n".join(lines[-2000:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def read_logs(limit=200):
    """最新在前。"""
    if not LOG_FILE.exists():
        return []
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    out = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out[::-1][:limit]


def _default_data():
    return {"providers": [], "quota": {"synced_at": None, "entries": {}, "raw": None, "last_error": None}}


def _migrate_provider(p):
    p.setdefault("allow_paid", False)
    p.setdefault("model_order", None)
    p.setdefault("test_results", {})
    p.setdefault("models", [])
    p.setdefault("active_model", None)
    return p


def load_data():
    with _lock:
        if not DATA_FILE.exists():
            return _default_data()
        try:
            d = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            return _default_data()
        d.setdefault("providers", [])
        d.setdefault("quota", {"synced_at": None, "entries": {}, "raw": None, "last_error": None})
        d.setdefault("ag_quota", {"accounts": [], "synced_at": "", "groups": {}})
        d.setdefault("hub_key", "")
        for p in d["providers"]:
            _migrate_provider(p)
        return d


def save_data(d):
    with _lock:
        tmp = DATA_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, DATA_FILE)


def mask_key(k: str) -> str:
    if not k:
        return ""
    if len(k) <= 10:
        return k[:2] + "****"
    return k[:5] + "..." + k[-4:]


def get_hub_key(d):
    """Hub 统一入口 key：首次访问自动生成并落盘，之后保持不变。"""
    if not d.get("hub_key"):
        d["hub_key"] = "hub-" + secrets.token_urlsafe(18)
        save_data(d)
    return d["hub_key"]


def find_bl():
    cands = [
        shutil.which("bl"),
        shutil.which("bl.cmd"),
        os.path.expandvars(r"%APPDATA%\npm\bl.CMD"),
        os.path.expandvars(r"%APPDATA%\npm\bl"),
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def run_bl(args, timeout=180):
    """运行 bl CLI，返回 (returncode, stdout, stderr)。找不到 bl 返回 None。"""
    bl = find_bl()
    if not bl:
        return None
    cmd = '"{}" {}'.format(bl, " ".join(args))
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"


# ---------------- 模型分类 ----------------

def model_category(name: str) -> str:
    n = (name or "").lower()
    if "embedding" in n:
        return "向量模型"
    if "rerank" in n:
        return "重排序"
    if any(k in n for k in ("tts", "cosyvoice", "sambert")):
        return "语音合成"
    if any(k in n for k in ("asr", "paraformer", "gummy", "filetrans")):
        return "语音识别"
    if any(k in n for k in ("t2v", "i2v", "r2v", "video", "happyhorse")):
        return "视频生成"
    if any(k in n for k in ("t2i", "i2i", "image", "wanx", "flux")):
        return "图像生成"
    if any(k in n for k in ("omni", "vl", "vision")):
        return "多模态"
    if any(k in n for k in ("audio", "livetranslate")):
        return "音频"
    return "文本生成"


# 可以走 /chat/completions 轮询的类别
CHAT_CATS = {"文本生成", "多模态", "音频"}


# ---------------- 免费额度判断 ----------------

def today_str():
    return time.strftime("%Y-%m-%d")


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def quota_state(d, model):
    """返回 (state, entry)。state: free_ok | free_empty | free_expired | no_entry"""
    e = (d.get("quota", {}).get("entries") or {}).get(model)
    if not e:
        return "no_entry", None
    exp = e.get("expires")
    if exp and str(exp)[:10] < today_str():
        return "free_expired", e
    try:
        rem = float(e.get("remaining"))
    except (TypeError, ValueError):
        rem = None
    if rem is not None and rem <= 0:
        return "free_empty", e
    return "free_ok", e


# ---------------- Antigravity 额度（按组共享：5 小时 + 每周） ----------------
# 官方口径：同一组内所有模型共享一个 5 小时额度 + 一个每周额度。
# 已核实分组：gemini-* → Gemini 组；claude-* / gpt-* → Claude and GPT 组。
AG_AUTH_DIR = os.path.expanduser(os.path.join("~", ".cli-proxy-api"))
AG_QUOTA_HOSTS = [
    "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
    "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
]
AG_GROUP_TITLES = {"gemini": "Gemini 组", "third": "Claude / GPT 组"}
# Google 侧按 User-Agent 做产品 license 校验：不带 antigravity UA 会 403 (#3501)
AG_UA = "antigravity/1.11.5 windows/amd64"


def is_ag_provider(p) -> bool:
    return p.get("type") == "openai" and ":8317" in (p.get("base_url") or "")


def ag_model_group(model: str) -> str:
    ml = (model or "").lower()
    if ml.startswith("gemini"):
        return "gemini"
    return "third"


def _ag_group_key(display_name: str, buckets) -> str:
    dn = (display_name or "").lower()
    if "gemini" in dn:
        return "gemini"
    ids = [(b.get("bucketId") or "") for b in buckets]
    if any("gemini" in i for i in ids):
        return "gemini"
    return "third"


def _utc_to_local_str(s: str) -> str:
    try:
        dt = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def ag_auth_files():
    out = []
    try:
        for fn in os.listdir(AG_AUTH_DIR):
            if fn.startswith("antigravity-") and fn.endswith(".json"):
                path = os.path.join(AG_AUTH_DIR, fn)
                try:
                    with open(path, encoding="utf-8") as f:
                        j = json.load(f)
                    out.append({"file": path, "email": fn[len("antigravity-"):-len(".json")], "auth": j})
                except Exception:
                    pass
    except Exception:
        pass
    return out


def fetch_ag_quota(d):
    """逐个本地 AG 凭证查询额度摘要（该接口不消耗对话额度）。返回 accounts 列表。"""
    prev_groups = ((d.get("ag_quota") or {}).get("groups") or {})
    accounts = []
    for item in ag_auth_files():
        auth = item["auth"]
        tok = auth.get("access_token") or ""
        acc = {"email": item["email"], "ok": False, "error": "", "groups": []}
        if not tok:
            acc["error"] = "凭证文件缺少 access_token（可能需重新登录）"
            accounts.append(acc)
            continue
        body, err = None, ""
        for host in AG_QUOTA_HOSTS:
            try:
                r = httpx.post(host, json={}, headers={"Authorization": "Bearer " + tok,
                                                       "User-Agent": AG_UA},
                               timeout=20, verify=False)
                if r.status_code == 200:
                    body = r.json()
                    break
                err = "HTTP %s" % r.status_code
            except Exception as e:
                err = str(e)[:200]
        if body is None:
            acc["error"] = err or "查询失败"
            accounts.append(acc)
            continue
        acc["ok"] = True
        for g in body.get("groups") or []:
            buckets = []
            for b in g.get("buckets") or []:
                rf = b.get("remainingFraction")
                buckets.append({
                    "bucket_id": b.get("bucketId") or "",
                    "window": b.get("window") or "",
                    "reset_time": b.get("resetTime") or "",
                    "remaining": rf if isinstance(rf, (int, float)) else None,
                })
            gkey = _ag_group_key(g.get("displayName"), buckets)
            cd = (prev_groups.get(gkey) or {})
            acc["groups"].append({
                "key": gkey,
                "title": AG_GROUP_TITLES.get(gkey, g.get("displayName") or gkey),
                "display_name": g.get("displayName") or "",
                "buckets": buckets,
                "cooldown_until": cd.get("cooldown_until") or "",
            })
        accounts.append(acc)
    return accounts


def ag_group_state(d, model: str):
    """(state, rem5h, remweekly, cooldown_until)；state: ok | empty | cooldown | unknown。
    unknown = 从未同步过额度，不拦截（乐观放行）。"""
    aq = d.get("ag_quota") or {}
    gkey = ag_model_group(model)
    cd = ((aq.get("groups") or {}).get(gkey) or {}).get("cooldown_until") or ""
    if cd and cd > now_str():
        return "cooldown", None, None, cd
    rem5 = remw = None
    for acc in aq.get("accounts") or []:
        if not acc.get("ok"):
            continue
        for g in acc.get("groups") or []:
            if g.get("key") != gkey:
                continue
            for b in g.get("buckets") or []:
                if b.get("window") == "5h":
                    rem5 = b.get("remaining")
                elif b.get("window") == "weekly":
                    remw = b.get("remaining")
    if rem5 is None and remw is None:
        return "unknown", None, None, cd
    if (rem5 is not None and rem5 <= 0.0005) or (remw is not None and remw <= 0.0005):
        return "empty", rem5, remw, cd
    return "ok", rem5, remw, cd


def _ag_set_cooldown(d, model: str):
    """某组调用命中额度类失败 → 冷却到该组 5h bucket 的重置时刻（取不到就 30 分钟）。"""
    aq = d.setdefault("ag_quota", {"accounts": [], "synced_at": "", "groups": {}})
    groups = aq.setdefault("groups", {})
    gkey = ag_model_group(model)
    until = ""
    for acc in aq.get("accounts") or []:
        for g in acc.get("groups") or []:
            if g.get("key") != gkey:
                continue
            for b in g.get("buckets") or []:
                if b.get("window") == "5h" and b.get("reset_time"):
                    until = _utc_to_local_str(b["reset_time"])
    if not until:
        until = (datetime.datetime.now() + datetime.timedelta(seconds=1800)).strftime("%Y-%m-%d %H:%M:%S")
    groups.setdefault(gkey, {})["cooldown_until"] = until
    save_data(d)
    return gkey, until


def ordered_models(p):
    """模型按用户排序（model_order）返回；未排序时按原始列表。"""
    models = p.get("models") or []
    order = p.get("model_order") or []
    seen, out = set(), []
    for m in order:
        if m in models and m not in seen:
            seen.add(m)
            out.append(m)
    for m in models:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def chat_candidates(d, p, extra_cats=None):
    """轮询候选：按排序，且（bailian 未允许付费时）必须有免费额度；仅保留可对话类别。
    extra_cats：额外放行的类别（仅 /v1/models 列表展示用，auto 轮询不传、不受影响）。"""
    cats = CHAT_CATS | set(extra_cats or ())
    out = []
    for m in ordered_models(p):
        if model_category(m) not in cats:
            continue
        if p.get("type") == "bailian" and not p.get("allow_paid"):
            st, _ = quota_state(d, m)
            if st != "free_ok":
                continue
        if is_ag_provider(p):
            agst, _r5, _rw, _cd = ag_group_state(d, m)
            if agst in ("empty", "cooldown"):
                continue
        out.append(m)
    return out


def all_chat_candidates(d):
    """跨渠道轮询候选：[(provider, model), ...]；按渠道排列顺序，各渠道内部按模型排序。"""
    pairs = []
    for p in d["providers"]:
        if not p.get("base_url"):
            continue
        for m in chat_candidates(d, p):
            pairs.append((p, m))
    return pairs


def find_model_provider(d, model):
    """指定模型 → 第一个拥有该模型的渠道；都没有则 None。"""
    for p in d["providers"]:
        if model in (p.get("models") or []):
            return p
    return None


# ---------------- 模型列表拉取（OpenAI 兼容） ----------------

def candidate_bases(base_url: str):
    b = base_url.rstrip("/")
    cands = [b]
    if not b.endswith("/v1"):
        cands.append(b + "/v1")
    if "dashscope.aliyuncs.com" in b and "compatible-mode" not in b:
        cands.append(b + "/compatible-mode/v1")
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def fetch_models(base_url: str, api_key: str):
    """返回 (生效 base_url, [model_id...], 尝试记录)"""
    tried = []
    headers = {"Authorization": "Bearer " + api_key} if api_key else {}
    with httpx.Client(timeout=20.0, trust_env=False) as cli:
        for b in candidate_bases(base_url):
            url = b + "/models"
            try:
                r = cli.get(url, headers=headers)
                if r.status_code == 200:
                    data = r.json()
                    items = data.get("data") or data.get("models") or []
                    ids = [m.get("id") for m in items if isinstance(m, dict) and m.get("id")]
                    return b, ids, tried
                tried.append("{} -> HTTP {}".format(url, r.status_code))
            except Exception as e:
                tried.append("{} -> {}".format(url, type(e).__name__))
    raise RuntimeError("模型列表拉取失败：" + "；".join(tried))


def test_model(base_url: str, api_key: str, model: str):
    """对指定模型发一个最小请求，返回 (ok, latency_ms, detail, usage)"""
    url = base_url.rstrip("/") + "/chat/completions"
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8, "stream": False}
    headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
    t0 = time.time()
    try:
        with httpx.Client(timeout=45.0, trust_env=False) as cli:
            r = cli.post(url, json=body, headers=headers)
        ms = int((time.time() - t0) * 1000)
        if r.status_code == 200:
            usage = None
            try:
                usage = r.json().get("usage")
            except Exception:
                pass
            return True, ms, "ok", usage
        snippet = re.sub(r"\s+", " ", r.text)[:400]
        return False, ms, "HTTP {}: {}".format(r.status_code, snippet), None
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        return False, ms, "{}: {}".format(type(e).__name__, e), None


# ---------------- 百炼免费额度（bl usage free） ----------------

MODEL_KEYS = ("model", "model_code", "modelCode", "model_id", "modelId", "name", "code")
REM_KEYS = ("remaining", "remaining_quota", "remainingTokens", "remaining_tokens",
            "remain", "left", "free_remaining", "remainingQuota")
TOT_KEYS = ("total", "total_quota", "totalTokens", "total_tokens", "quota",
            "free_total", "totalQuota", "limit")
EXP_KEYS = ("expires_at", "expire_time", "expiry", "expire", "expires",
            "gmt_expire", "expired_at", "deadline", "valid_until", "expireTime")
STATUS_KEYS = ("status", "state")


def _first(d, keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _find_entry_list(x):
    if isinstance(x, list) and x and all(isinstance(i, dict) for i in x):
        if any(_first(i, MODEL_KEYS) for i in x):
            return x
    if isinstance(x, dict):
        for v in x.values():
            r = _find_entry_list(v)
            if r:
                return r
    if isinstance(x, list):
        for v in x:
            r = _find_entry_list(v)
            if r:
                return r
    return None


def parse_usage_free(data):
    """把 bl usage free 的 JSON 输出规整成 {model: {remaining,total,expires,status}}"""
    entries = {}
    lst = _find_entry_list(data)
    if not lst:
        return entries
    for item in lst:
        m = _first(item, MODEL_KEYS)
        if not m:
            continue
        entries[str(m)] = {
            "remaining": _first(item, REM_KEYS),
            "total": _first(item, TOT_KEYS),
            "expires": _first(item, EXP_KEYS),
            "status": _first(item, STATUS_KEYS),
        }
    return entries


def refresh_quota():
    d = load_data()
    res = run_bl(["usage", "free", "--all", "--output", "json"])
    if res is None:
        d["quota"]["last_error"] = "bl_missing"
        save_data(d)
        return {"ok": False, "state": "bl_missing",
                "message": "未检测到百炼 CLI（bl）。请先安装：npm install -g bailian-cli"}
    rc, so, se = res
    payload = (so.strip() or se.strip())
    if rc != 0:
        low = payload.lower()
        state = "error"
        if "console access token" in low or "login --console" in low or "console" in low and "login" in low:
            state = "console_login_required"
        d["quota"]["last_error"] = state
        save_data(d)
        return {"ok": False, "state": state, "message": payload[:800]}
    try:
        data = json.loads(so.strip())
    except Exception:
        d["quota"]["last_error"] = "parse_error"
        save_data(d)
        return {"ok": False, "state": "parse_error", "message": "无法解析 bl 输出：" + payload[:500]}
    entries = parse_usage_free(data)
    d["quota"]["entries"] = entries
    d["quota"]["raw"] = data
    d["quota"]["synced_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    d["quota"]["last_error"] = None
    save_data(d)
    return {"ok": True, "state": "ok", "count": len(entries), "synced_at": d["quota"]["synced_at"]}


# ---------------- FastAPI ----------------

app = FastAPI(title="LLM Key Hub")


class ProviderIn(BaseModel):
    name: str
    type: str = "openai"          # openai | bailian
    base_url: str
    api_key: str = ""


class ProviderUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    allow_paid: bool | None = None


class ModelIn(BaseModel):
    model: str


class MoveIn(BaseModel):
    model: str
    dir: int                      # -1 上移 / +1 下移


class FreetierIn(BaseModel):
    mode: str = "on"              # on | off


def get_provider(pid):
    d = load_data()
    for p in d["providers"]:
        if p["id"] == pid:
            return d, p
    raise HTTPException(404, "provider not found")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/monitor")
def monitor_page():
    return FileResponse(STATIC_DIR / "monitor.html")


@app.get("/api/state")
def state():
    d = load_data()
    bl = find_bl()
    return {
        "providers": d["providers"],
        "quota": d["quota"],
        "ag_quota": d.get("ag_quota") or {"accounts": [], "synced_at": "", "groups": {}},
        "bl_installed": bool(bl),
        "hub_base": HUB_BASE,
        "hub_key": get_hub_key(d),
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.post("/api/providers")
def add_provider(inp: ProviderIn):
    d = load_data()
    p = {
        "id": uuid.uuid4().hex[:10],
        "name": inp.name.strip() or "未命名渠道",
        "type": inp.type,
        "base_url": inp.base_url.strip(),
        "api_key": inp.api_key.strip(),
        "models": [],
        "model_order": None,
        "allow_paid": False,
        "active_model": None,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "last_refresh": None,
    }
    try:
        eff, ids, _ = fetch_models(p["base_url"], p["api_key"])
        p["base_url"] = eff
        p["models"] = ids
        p["last_refresh"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if ids and not p["active_model"]:
            p["active_model"] = ids[0]
        p["fetch_error"] = None
    except Exception as e:
        p["fetch_error"] = str(e)
    d["providers"].append(p)
    save_data(d)
    return p


@app.put("/api/providers/{pid}")
def update_provider(pid: str, inp: ProviderUpdate):
    d, p = get_provider(pid)
    if inp.name is not None:
        p["name"] = inp.name.strip() or p["name"]
    if inp.base_url is not None:
        p["base_url"] = inp.base_url.strip()
    if inp.api_key is not None:
        p["api_key"] = inp.api_key.strip()
    if inp.allow_paid is not None:
        p["allow_paid"] = bool(inp.allow_paid)
    save_data(d)
    return {"ok": True, "allow_paid": p["allow_paid"]}


@app.delete("/api/providers/{pid}")
def delete_provider(pid: str):
    d = load_data()
    before = len(d["providers"])
    d["providers"] = [p for p in d["providers"] if p["id"] != pid]
    if len(d["providers"]) == before:
        raise HTTPException(404, "provider not found")
    save_data(d)
    return {"ok": True}


@app.post("/api/providers/{pid}/models/refresh")
def refresh_models(pid: str):
    d, p = get_provider(pid)
    try:
        eff, ids, tried = fetch_models(p["base_url"], p["api_key"])
    except Exception as e:
        p["fetch_error"] = str(e)
        save_data(d)
        return {"ok": False, "message": str(e)}
    p["base_url"] = eff
    p["models"] = ids
    p["last_refresh"] = time.strftime("%Y-%m-%d %H:%M:%S")
    p["fetch_error"] = None
    if ids and p.get("active_model") not in ids:
        p["active_model"] = ids[0]
    save_data(d)
    return {"ok": True, "count": len(ids), "base_url": eff}


@app.post("/api/providers/{pid}/models/delete")
def delete_model(pid: str, inp: ModelIn):
    d, p = get_provider(pid)
    p["models"] = [m for m in p.get("models", []) if m != inp.model]
    if p.get("model_order"):
        p["model_order"] = [m for m in p["model_order"] if m != inp.model]
    p.get("test_results", {}).pop(inp.model, None)
    if p.get("active_model") == inp.model:
        p["active_model"] = p["models"][0] if p["models"] else None
    save_data(d)
    return {"ok": True, "left": len(p["models"])}


@app.post("/api/providers/{pid}/models/cleanup")
def cleanup_models(pid: str):
    """清理：免费额度已过期/已用尽 + 测试失败的模型（bailian 渠道）；其他渠道仅清测试失败的。"""
    d, p = get_provider(pid)
    tr = p.get("test_results", {})
    removed, keep = [], []
    for m in p.get("models", []):
        failed = bool(tr.get(m) and not tr[m].get("ok"))
        drop = failed
        if p.get("type") == "bailian":
            st, _ = quota_state(d, m)
            if st in ("free_expired", "free_empty"):
                drop = True
        (removed if drop else keep).append(m)
    p["models"] = keep
    if p.get("model_order"):
        p["model_order"] = [m for m in p["model_order"] if m in keep]
    for m in removed:
        tr.pop(m, None)
    if p.get("active_model") not in keep:
        p["active_model"] = keep[0] if keep else None
    save_data(d)
    return {"ok": True, "removed": removed, "left": len(keep)}


@app.post("/api/providers/{pid}/order/move")
def move_model(pid: str, inp: MoveIn):
    d, p = get_provider(pid)
    order = ordered_models(p)
    if inp.model not in order:
        raise HTTPException(404, "model not found")
    i = order.index(inp.model)
    j = i + (1 if inp.dir > 0 else -1)
    if 0 <= j < len(order):
        order[i], order[j] = order[j], order[i]
    p["model_order"] = order
    save_data(d)
    return {"ok": True}


@app.put("/api/providers/{pid}/active")
def set_active(pid: str, inp: ModelIn):
    d, p = get_provider(pid)
    p["active_model"] = inp.model
    save_data(d)
    return {"ok": True, "active_model": inp.model}


PAID_BLOCK_TEXT = {
    "free_expired": "免费额度已过期",
    "free_empty": "免费额度已用完",
    "no_entry": "该模型没有免费额度",
}


@app.post("/api/providers/{pid}/test")
def test(pid: str, inp: ModelIn):
    d, p = get_provider(pid)
    if p.get("type") == "bailian" and not p.get("allow_paid"):
        st, _ = quota_state(d, inp.model)
        if st != "free_ok":
            msg = PAID_BLOCK_TEXT.get(st, st)
            log_call({"source": "test", "provider": p.get("name"),
                      "model_requested": inp.model, "model_used": inp.model,
                      "ok": False, "blocked": True, "latency_ms": 0,
                      "error": "仅免费额度策略拦截：" + msg})
            return {"ok": False, "blocked": True, "latency_ms": 0,
                    "detail": msg +
                              "，未发起调用（当前策略：仅免费额度）。如确认要付费测试，请勾选渠道上的「允许付费」。"}
    if is_ag_provider(p):
        agst, _r5, _rw, cd = ag_group_state(d, inp.model)
        if agst in ("empty", "cooldown"):
            gname = AG_GROUP_TITLES.get(ag_model_group(inp.model), "额度组")
            msg = ("%s额度已耗尽，正在冷却至 %s" % (gname, cd)) if agst == "cooldown" else \
                  ("%s的 5 小时 / 每周额度已耗尽，等待重置" % gname)
            log_call({"source": "test", "provider": p.get("name"),
                      "model_requested": inp.model, "model_used": inp.model,
                      "ok": False, "blocked": True, "latency_ms": 0,
                      "error": "AG 组额度拦截：" + msg})
            return {"ok": False, "blocked": True, "latency_ms": 0, "detail": msg + "，未发起调用。"}
    ok, ms, detail, usage = test_model(p["base_url"], p["api_key"], inp.model)
    p.setdefault("test_results", {})[inp.model] = {
        "ok": ok, "latency_ms": ms, "detail": detail,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_data(d)
    u = usage or {}
    log_call({"source": "test", "provider": p.get("name"),
              "model_requested": inp.model, "model_used": inp.model,
              "ok": ok, "status": 200 if ok else None, "latency_ms": ms,
              "prompt_tokens": u.get("prompt_tokens"),
              "completion_tokens": u.get("completion_tokens"),
              "total_tokens": u.get("total_tokens"),
              "error": None if ok else detail[:300]})
    return {"ok": ok, "latency_ms": ms, "detail": detail}


@app.post("/api/quota/refresh")
def quota_refresh():
    return refresh_quota()


# ---------------- 百炼试用完即停 ----------------

@app.post("/api/bl/freetier")
def bl_freetier(inp: FreetierIn):
    """bl usage freetier --all [--off]：给全部免费额度模型开启/关闭「用完即停」。"""
    args = ["usage", "freetier", "--all"]
    if inp.mode == "off":
        args.append("--off")
    res = run_bl(args, timeout=300)
    if res is None:
        return {"ok": False, "message": "未检测到百炼 CLI（bl）"}
    rc, so, se = res
    out = (so.strip() or se.strip())
    return {"ok": rc == 0, "output": out[:800]}


# ---------------- 百炼控制台授权（WebUI 一键触发，浏览器登录） ----------------

BL_LOGIN_LOG = BASE_DIR / "bl_login.log"
_bl_login = {"proc": None, "started_at": None, "log": None}


@app.post("/api/bl/login")
def bl_login_start():
    """启动 bl auth login --console：会在本机弹出浏览器，用户在浏览器里登录阿里云。"""
    bl = find_bl()
    if not bl:
        raise HTTPException(400, "未检测到百炼 CLI（bl）")
    proc = _bl_login.get("proc")
    if proc and proc.poll() is None:
        return {"ok": True, "already": True,
                "message": "授权流程已在进行中，请在浏览器里完成登录"}
    try:
        logf = open(BL_LOGIN_LOG, "wb")
    except Exception:
        logf = subprocess.DEVNULL
    cmd = '"{}" auth login --console'.format(bl)
    try:
        proc = subprocess.Popen(
            cmd, shell=True, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    except Exception as e:
        raise HTTPException(500, "启动授权流程失败：%r" % e)
    _bl_login["proc"] = proc
    _bl_login["log"] = logf if logf is not subprocess.DEVNULL else None
    _bl_login["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return {"ok": True, "pid": proc.pid,
            "message": "授权流程已启动，浏览器即将打开阿里云登录页，请完成登录"}


@app.get("/api/bl/login/status")
def bl_login_status():
    """前端轮询：授权进程是否还在跑；结束后自动同步一次额度并回传结果。"""
    proc = _bl_login.get("proc")
    running = bool(proc and proc.poll() is None)
    out = {"running": running, "started_at": _bl_login.get("started_at")}
    try:
        if BL_LOGIN_LOG.exists():
            txt = BL_LOGIN_LOG.read_text(encoding="utf-8", errors="replace")
            out["log_tail"] = txt[-600:]
    except Exception:
        pass
    if not running and proc is not None:
        out["exit_code"] = proc.returncode
        _bl_login["proc"] = None
        lg = _bl_login.get("log")
        try:
            if lg:
                lg.close()
        except Exception:
            pass
        _bl_login["log"] = None
        out["quota_sync"] = refresh_quota()  # 结束后立刻试同步
    return out


@app.get("/api/bl/status")
def bl_status():
    bl = find_bl()
    if not bl:
        return {"installed": False}
    res = run_bl(["auth", "status", "--output", "json"], timeout=60)
    out = {"installed": True, "path": bl}
    if res and res[0] == 0:
        try:
            out["auth"] = json.loads(res[1].strip())
        except Exception:
            out["auth_raw"] = res[1][:300]
    else:
        out["auth_error"] = (res[1] + res[2])[:300] if res else "unknown"
    return out


# ---------------- 反重力（Google Antigravity）授权 ----------------

CLIPROXY_DIR = BASE_DIR / "cliproxy"
AG_LOGIN_LOG = BASE_DIR / "ag_login.log"
_ag_login = {"proc": None, "started_at": None, "log": None, "restarted": False}


def find_cliproxy():
    exe = CLIPROXY_DIR / "cli-proxy-api.exe"
    return str(exe) if exe.exists() else None


def ag_api_key():
    cfg = CLIPROXY_DIR / "config.yaml"
    if not cfg.exists():
        return ""
    try:
        txt = cfg.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    m = re.search(r'api-keys:\s*\r?\n\s*-\s*"([^"]+)"', txt)
    return m.group(1) if m else ""


def ag_models_count():
    """查本地反代 (127.0.0.1:8317) 的模型数；拿不到 = 未启动/未授权"""
    try:
        with httpx.Client(timeout=8.0, trust_env=False) as cli:
            r = cli.get("http://127.0.0.1:8317/v1/models",
                        headers={"Authorization": "Bearer " + ag_api_key()})
            if r.status_code == 200:
                data = r.json()
                items = data.get("data") or data.get("models") or []
                return len(items)
    except Exception:
        pass
    return 0


def _ag_restart_proxy():
    """杀掉旧的 cli-proxy-api 进程并重新拉起（登录完成后加载新凭证）"""
    subprocess.run("taskkill /F /IM cli-proxy-api.exe", shell=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    exe = find_cliproxy()
    if not exe:
        return
    cmd = '"{}" -config config.yaml'.format(exe)
    try:
        subprocess.Popen(cmd, shell=True, cwd=str(CLIPROXY_DIR),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL,
                         creationflags=0x00000008 | 0x00000200)
    except Exception:
        pass
    time.sleep(3)


def _ag_refresh_hub_provider():
    """登录完成后，把 Hub 里指向 8317 的渠道模型列表刷新一遍"""
    d = load_data()
    hit = None
    for p in d.get("providers", []):
        if "8317" in (p.get("base_url") or ""):
            hit = p
            break
    if not hit:
        return {"refreshed": False, "reason": "hub 里没有指向 8317 的渠道"}
    try:
        eff, ids, _ = fetch_models(hit["base_url"], hit.get("api_key") or ag_api_key())
    except Exception as e:
        return {"refreshed": False, "reason": str(e)[:200]}
    hit["base_url"] = eff
    hit["models"] = ids
    hit["last_refresh"] = time.strftime("%Y-%m-%d %H:%M:%S")
    hit["fetch_error"] = None
    if ids and hit.get("active_model") not in ids:
        hit["active_model"] = ids[0]
    save_data(d)
    return {"refreshed": True, "count": len(ids)}


@app.get("/api/ag/status")
def ag_status():
    """反代是否在跑 + 当前可用模型数（未授权时通常是 0）"""
    return {"installed": bool(find_cliproxy()),
            "models_count": ag_models_count(),
            "api_key_set": bool(ag_api_key())}


@app.post("/api/ag/login")
def ag_login_start():
    """启动 cli-proxy-api -antigravity-login：会在本机弹出浏览器，走 Google 授权。"""
    exe = find_cliproxy()
    if not exe:
        raise HTTPException(400, "未检测到 cliproxy/cli-proxy-api.exe")
    proc = _ag_login.get("proc")
    if proc and proc.poll() is None:
        return {"ok": True, "already": True,
                "message": "反重力授权已在进行中，请在浏览器里完成 Google 登录"}
    try:
        logf = open(AG_LOGIN_LOG, "wb")
    except Exception:
        logf = subprocess.DEVNULL
    cmd = '"{}" -config config.yaml -antigravity-login'.format(exe)
    try:
        # CREATE_NEW_CONSOLE：DETACHED 无控制台会让 cli-proxy-api 的浏览器唤起/日志静默失败
        proc = subprocess.Popen(
            cmd, shell=True, cwd=str(CLIPROXY_DIR),
            stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=0x00000010 | 0x00000200)
    except Exception as e:
        raise HTTPException(500, "启动授权流程失败：%r" % e)
    _ag_login["proc"] = proc
    _ag_login["log"] = logf if logf is not subprocess.DEVNULL else None
    _ag_login["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _ag_login["restarted"] = False
    return {"ok": True, "pid": proc.pid,
            "message": "授权流程已启动，浏览器即将打开 Google 登录页，请完成授权"}


@app.get("/api/ag/quota")
def api_ag_quota():
    d = load_data()
    return {"ok": True, "ag_quota": d.get("ag_quota") or {},
            "accounts_found": len(ag_auth_files())}


@app.post("/api/ag/quota/refresh")
def api_ag_quota_refresh():
    """同步反重力额度（5 小时 / 每周，按组）。不消耗对话额度。"""
    d = load_data()
    accounts = fetch_ag_quota(d)
    aq = d.setdefault("ag_quota", {"accounts": [], "synced_at": "", "groups": {}})
    # 同步回来的数据若某组冷却已过期，清掉冷却
    now = now_str()
    groups = aq.setdefault("groups", {})
    for gkey, gv in list(groups.items()):
        if gv.get("cooldown_until") and gv["cooldown_until"] <= now:
            gv["cooldown_until"] = ""
    # 把冷却状态合并进 accounts 展示结构
    for acc in accounts:
        for g in acc.get("groups") or []:
            g["cooldown_until"] = (groups.get(g.get("key")) or {}).get("cooldown_until") or ""
    aq["accounts"] = accounts
    aq["synced_at"] = now
    save_data(d)
    ok_n = len([a for a in accounts if a.get("ok")])
    return {"ok": True, "synced": ok_n, "accounts": len(accounts), "ag_quota": aq}


@app.get("/api/ag/login/status")
def ag_login_status():
    """前端轮询：授权进程状态；结束后自动重启反代并刷新 Hub 渠道模型列表。"""
    proc = _ag_login.get("proc")
    running = bool(proc and proc.poll() is None)
    out = {"running": running, "started_at": _ag_login.get("started_at"),
           "models_count": ag_models_count()}
    try:
        if AG_LOGIN_LOG.exists():
            txt = AG_LOGIN_LOG.read_text(encoding="utf-8", errors="replace")
            out["log_tail"] = txt[-800:]
    except Exception:
        pass
    if not running and proc is not None:
        out["exit_code"] = proc.returncode
        _ag_login["proc"] = None
        lg = _ag_login.get("log")
        try:
            if lg:
                lg.close()
        except Exception:
            pass
        _ag_login["log"] = None
        if out["models_count"] == 0 and not _ag_login.get("restarted"):
            _ag_login["restarted"] = True
            _ag_restart_proxy()
            out["proxy_restarted"] = True
            out["models_count"] = ag_models_count()
        out["hub_refresh"] = _ag_refresh_hub_provider()
    return out


# ---------------- 统一轮询代理（OpenAI 兼容） ----------------

QUOTA_LIKE = re.compile(
    r"quota|insufficient|exceed|throttl|allocation|freetier|arrearage|overdue|"
    r"free.?tier|rate.?limit|欠费|余额|额度|限流", re.I)


def _quota_like(code, payload):
    if code == 429:
        return True
    txt = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return bool(QUOTA_LIKE.search(txt or ""))


def _pick_provider(d, req):
    pid = req.headers.get("x-hub-provider")
    if pid:
        for p in d["providers"]:
            if p["id"] == pid or p["name"] == pid:
                return p
        raise HTTPException(404, "provider not found: " + pid)
    if not d["providers"]:
        raise HTTPException(400, "还没有任何渠道，请先打开 Hub 页面添加")
    return d["providers"][0]


def _require_hub_key(d, req):
    key = get_hub_key(d)
    auth = req.headers.get("authorization") or ""
    if auth != "Bearer " + key:
        raise HTTPException(status_code=401,
                            detail="未授权：请携带 Hub 统一 key（Authorization: Bearer <hub_key>，见 Hub 面板顶部）。")


@app.get("/v1/models")
def hub_models(req: Request):
    """统一模型清单：auto + 各渠道可对话模型 + 图像生成模型（供标准客户端拉列表，需 hub_key）。
    图像生成模型只在列表中可见、可显式指定调用；不进入 auto 轮询。"""
    d = load_data()
    _require_hub_key(d, req)
    data = [{"id": "auto", "object": "model", "owned_by": "llm-hub"}]
    seen = {"auto"}
    for p in d["providers"]:
        for m in chat_candidates(d, p, extra_cats={"图像生成"}):
            if m in seen:
                continue
            seen.add(m)
            data.append({"id": m, "object": "model",
                         "owned_by": p.get("name") or p.get("type") or "provider"})
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def hub_chat(req: Request):
    """OpenAI 兼容统一入口（一条 key、一个 URL）。
    - 鉴权：Authorization: Bearer <hub_key>（Hub 面板顶部查看/复制）。
    - model=auto 且未指定渠道：跨渠道大轮动——按渠道顺序逐个尝试各渠道的可调用模型；
      加请求头 X-Hub-Provider 则只在该渠道内轮动。
    - 指定具体模型：自动定位拥有该模型的渠道（或按 X-Hub-Provider 指定），
      同样受「仅免费额度」（百炼）与 AG 组额度保护。
    响应头 X-Hub-Provider / X-Hub-Model 标明实际命中的渠道与模型。"""
    body = await req.json()
    d = load_data()
    _require_hub_key(d, req)
    model = body.get("model") or "auto"
    stream = bool(body.get("stream"))

    if req.headers.get("x-hub-provider"):
        p = _pick_provider(d, req)
        scoped = [(p, m) for m in (chat_candidates(d, p) if model == "auto" else [model])]
    elif model == "auto":
        scoped = all_chat_candidates(d)
    else:
        p = find_model_provider(d, model)
        if p is None:
            if not d["providers"]:
                raise HTTPException(400, "还没有任何渠道，请先打开 Hub 页面添加")
            p = d["providers"][0]
        scoped = [(p, model)]

    if model == "auto":
        if not scoped:
            log_call({"source": "proxy", "provider": None,
                      "model_requested": "auto", "model_used": None,
                      "ok": False, "blocked": True, "latency_ms": 0,
                      "error": "没有可调用的免费额度模型"})
            return JSONResponse(status_code=403, content={"error": {
                "message": "没有可调用的免费额度模型（全部无额度/耗尽/过期）。如确认付费调用，请在 Hub 页面勾选「允许付费」。",
                "type": "hub_no_free_model"}})
    else:
        if p.get("type") == "bailian" and not p.get("allow_paid"):
            st, _ = quota_state(d, model)
            if st != "free_ok":
                msg = PAID_BLOCK_TEXT.get(st, st)
                log_call({"source": "proxy", "provider": p.get("name"),
                          "model_requested": model, "model_used": model,
                          "ok": False, "blocked": True, "latency_ms": 0,
                          "error": "仅免费额度策略拦截：" + msg})
                return JSONResponse(status_code=403, content={"error": {
                    "message": "模型 {} {}，已按「仅免费额度」策略拦截。如需付费调用请勾选「允许付费」。".format(
                        model, msg),
                    "type": "hub_paid_blocked"}})
        if is_ag_provider(p):
            agst, _r5, _rw, cd = ag_group_state(d, model)
            if agst in ("empty", "cooldown"):
                gname = AG_GROUP_TITLES.get(ag_model_group(model), "额度组")
                msg = ("%s额度已耗尽，正在冷却至 %s" % (gname, cd)) if agst == "cooldown" else \
                      ("%s的 5 小时 / 每周额度已耗尽，等待重置后自动恢复" % gname)
                log_call({"source": "proxy", "provider": p.get("name"),
                          "model_requested": model, "model_used": model,
                          "ok": False, "blocked": True, "latency_ms": 0,
                          "error": "AG 组额度拦截：" + msg})
                return JSONResponse(status_code=429, content={"error": {
                    "message": "模型 {} 所属{}。".format(model, msg),
                    "type": "hub_ag_group_blocked"}})

    if stream:
        # 流式不做轮询，直接用第一个候选透传
        p0, m0 = scoped[0]
        return await _forward_stream(p0, body, m0, model_requested=model)

    last = None
    cooled_groups = set()
    first_model = scoped[0][1]
    for p, m in scoped:
        if is_ag_provider(p) and (p["id"], ag_model_group(m)) in cooled_groups:
            log_call({"source": "proxy", "provider": p.get("name"),
                      "model_requested": model, "model_used": m,
                      "ok": False, "blocked": True, "latency_ms": 0,
                      "error": "同组共享额度，跳过（该组已冷却）"})
            continue
        code, resp, ms = await _forward_chat(p, body, m)
        usage = resp.get("usage") if code == 200 and isinstance(resp, dict) else None
        u = usage or {}
        log_call({"source": "proxy", "provider": p.get("name"),
                  "model_requested": model, "model_used": m,
                  "ok": code == 200, "status": code, "latency_ms": ms,
                  "prompt_tokens": u.get("prompt_tokens"),
                  "completion_tokens": u.get("completion_tokens"),
                  "total_tokens": u.get("total_tokens"),
                  "rotated": (model == "auto" and m != first_model) or (model not in ("auto", m)),
                  "error": None if code == 200 else
                           (json.dumps(resp, ensure_ascii=False)[:300] if isinstance(resp, dict) else str(resp)[:300])})
        if code == 200:
            return JSONResponse(content=resp,
                                headers={"X-Hub-Model": m, "X-Hub-Provider": p.get("id")})
        last = (p, m, code, resp)
        if not _quota_like(code, resp):
            break
        if is_ag_provider(p):
            # 额度类失败 → 该模型所在组整组冷却（组内模型共享额度），到 5h 重置点自动恢复
            gkey, until = _ag_set_cooldown(d, m)
            cooled_groups.add((p["id"], gkey))
            d = load_data()
    p, m, code, resp = last
    content = resp if isinstance(resp, dict) else {"error": {"message": str(resp)[:500]}}
    return JSONResponse(status_code=code if 100 <= code <= 599 else 502,
                        content=content,
                        headers={"X-Hub-Model": m, "X-Hub-Provider": p.get("id")})


async def _forward_chat(p, body, model):
    b = dict(body)
    b["model"] = model
    b["stream"] = False
    headers = {"Authorization": "Bearer " + p["api_key"], "Content-Type": "application/json"}
    url = p["base_url"].rstrip("/") + "/chat/completions"
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=20.0),
                                     trust_env=False) as cli:
            r = await cli.post(url, json=b, headers=headers)
        ms = int((time.time() - t0) * 1000)
        try:
            return r.status_code, r.json(), ms
        except Exception:
            return r.status_code, r.text, ms
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        return 502, {"error": {"message": "{}: {}".format(type(e).__name__, e)}}, ms


async def _forward_stream(p, body, model, model_requested=None):
    b = dict(body)
    b["model"] = model
    headers = {"Authorization": "Bearer " + p["api_key"], "Content-Type": "application/json"}
    url = p["base_url"].rstrip("/") + "/chat/completions"
    t0 = time.time()
    client = httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=20.0), trust_env=False)
    request = client.build_request("POST", url, json=b, headers=headers)
    resp = await client.send(request, stream=True)

    async def gen():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            ms = int((time.time() - t0) * 1000)
            log_call({"source": "proxy", "provider": p.get("name"),
                      "model_requested": model_requested or model, "model_used": model,
                      "ok": resp.status_code == 200, "status": resp.status_code,
                      "latency_ms": ms, "stream": True,
                      "error": None if resp.status_code == 200 else "stream HTTP %d" % resp.status_code})
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(gen(), status_code=resp.status_code,
                             media_type="text/event-stream",
                             headers={"X-Hub-Model": model})


# ---------------- 调用监控（日志查询 / 统计 / 清空） ----------------

@app.get("/api/logs")
def api_logs(limit: int = 100):
    limit = max(1, min(limit, 500))
    return {"logs": read_logs(limit)}


@app.get("/api/logs/stats")
def api_log_stats():
    logs = read_logs(2000)
    total = len(logs)
    ok_n = sum(1 for l in logs if l.get("ok"))
    blocked_n = sum(1 for l in logs if l.get("blocked"))
    lats = [l["latency_ms"] for l in logs if isinstance(l.get("latency_ms"), (int, float)) and l.get("latency_ms")]
    tok_total = sum(l.get("total_tokens") or 0 for l in logs)
    by_model = {}
    for l in logs:
        m = l.get("model_used") or l.get("model_requested") or "?"
        b = by_model.setdefault(m, {"calls": 0, "ok": 0, "tokens": 0, "lat_sum": 0, "lat_n": 0})
        b["calls"] += 1
        if l.get("ok"):
            b["ok"] += 1
        b["tokens"] += l.get("total_tokens") or 0
        if isinstance(l.get("latency_ms"), (int, float)) and l.get("latency_ms"):
            b["lat_sum"] += l["latency_ms"]
            b["lat_n"] += 1
    models = sorted(
        ({"model": m, "calls": b["calls"], "ok": b["ok"], "tokens": b["tokens"],
          "avg_ms": int(b["lat_sum"] / b["lat_n"]) if b["lat_n"] else None}
         for m, b in by_model.items()),
        key=lambda x: -x["calls"])
    return {
        "total": total, "ok": ok_n, "blocked": blocked_n,
        "ok_rate": round(ok_n / total * 100, 1) if total else None,
        "avg_ms": int(sum(lats) / len(lats)) if lats else None,
        "tokens_total": tok_total,
        "by_model": models,
    }


@app.post("/api/logs/clear")
def api_logs_clear():
    try:
        if LOG_FILE.exists():
            LOG_FILE.unlink()
    except Exception as e:
        return {"ok": False, "message": repr(e)}
    return {"ok": True}


if __name__ == "__main__":
    import socket
    _s = socket.socket()
    try:
        _s.settimeout(1.5)
        _already = _s.connect_ex(("127.0.0.1", 8787)) == 0
    finally:
        _s.close()
    if _already:
        print("LLM Key Hub 已经在运行： http://127.0.0.1:8787")
        print("无需重复启动，直接在浏览器打开上面的地址即可。本窗口可以关闭。")
        sys.exit(0)
    print("LLM Key Hub 已启动: http://127.0.0.1:8787  (仅监听本机回环)")
    try:
        uvicorn.run(app, host="127.0.0.1", port=8787, log_level="warning")
    except OSError as e:
        print("启动失败：%r" % e)
        sys.exit(1)
