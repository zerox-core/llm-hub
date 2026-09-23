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
import glob
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import yaml

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data.json"
STATIC_DIR = BASE_DIR / "static"
LOG_FILE = BASE_DIR / "logs.jsonl"

HUB_BASE = "http://127.0.0.1:8787/v1"

# ---- cloud mode (HUB_CLOUD=1): container orchestration, disable local process mgmt ----
HUB_CLOUD = os.environ.get("HUB_CLOUD") == "1"
CLIPROXY_BASE = os.environ.get("CLIPROXY_BASE", "http://127.0.0.1:8317").rstrip("/")
COPILOT_BASE = os.environ.get("COPILOT_BASE", "http://127.0.0.1:4141").rstrip("/")
DATA_FILE = Path(os.environ.get("HUB_DATA_FILE", str(DATA_FILE)))
LOG_FILE = Path(os.environ.get("HUB_LOG_FILE", str(LOG_FILE)))

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
    p.setdefault("disabled_models", [])   # 号池里被取消勾选的模型
    p.setdefault("pool_id", None)         # 所属号池
    return p


def _migrate_pools(d):
    """号池迁移：无号池时建默认号池并把现有渠道全部归入；渠道缺 pool_id 时归入第一个号池。"""
    pools = d.setdefault("pools", [])
    if not pools:
        pools.append({"id": "pool_default", "name": "默认号池",
                      "created_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    ids = {pl["id"] for pl in pools}
    default_id = pools[0]["id"]
    for p in d.get("providers", []):
        if p.get("pool_id") not in ids:
            p["pool_id"] = default_id


def _pool_new_key():
    return "pool-" + secrets.token_urlsafe(18)


def _ensure_pool_keys(d):
    """号池 key 惰性生成（老数据迁移补齐）；有新建则落盘。"""
    changed = False
    for pl in d.get("pools") or []:
        if not pl.get("key"):
            pl["key"] = _pool_new_key()
            changed = True
    if changed:
        save_data(d)
    return changed


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
        d.setdefault("wb_quota", {"accounts": [], "rates": {}, "synced_at": ""})
        d.setdefault("hub_key", "")
        d.setdefault("harness_model", "auto")       # Harness 模型选择：auto=轮询 / 具体模型=锁定
        d.setdefault("autostart_harness", True)     # 启动器联动：Hub 启动后自动拉起 dsh
        d.setdefault("autostart_copilot", True)     # 启动器联动：Hub 启动后自动拉起 copilot-api
        d.setdefault("copilot_quota", {})           # Copilot premium 配额（check-usage 同步）
        d.setdefault("call_config", {"mode": "lan", "public_host": ""})  # 调用地址开关：lan=本机(局域网) / public=公网
        for p in d["providers"]:
            _migrate_provider(p)
        _migrate_pools(d)
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

# WorkBuddy（腾讯 CodeBuddy）反代：与 AG 共用本地 cli-proxy-api（:8317），
# Hub 渠道用 kind="wb" 标记区分；模型前缀用于从 8317 模型列表里分拣 wb 模型。
WB_MODEL_PREFIXES = ("glm-5", "kimi", "minimax", "hy3", "hy4", "deepseek-v4")
WB_AUTH_FILE = os.path.join(AG_AUTH_DIR, "workbuddy.json")


def is_wb_provider(p) -> bool:
    return p.get("kind") == "wb"


def is_ag_provider(p) -> bool:
    return (p.get("type") == "openai" and ":8317" in (p.get("base_url") or "")
            and p.get("kind") != "wb")


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
    """某额度组某模型判定失败 → 组冷却到 5h bucket 的重置时刻；取不到则不设冷却（2026-09-23 R24：30 分钟兜底已删）。"""
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
    # 2026-09-23 R24: 取不到 bucket 重置时刻就不设冷却（原 30 分钟兜底已删）；
    # 本次请求内的避让由调用方轮询循环 cooled_groups 内存集合负责。
    if until:
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
    disabled = set(p.get("disabled_models") or [])
    out = []
    for m in ordered_models(p):
        if m in disabled:
            continue
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


def model_disabled(p, model):
    """模型是否在号池中被取消勾选（取消勾选 = 不进轮询、不可经 Hub 调用）。"""
    return model in (p.get("disabled_models") or [])


def find_model_provider(d, model):
    """指定模型 → 第一个拥有该模型的渠道；都没有则 None。"""
    for p in d["providers"]:
        if model in (p.get("models") or []):
            return p
    return None


def pool_scope_candidates(d, pl, extra_cats=None):
    """号池 key 的候选 (provider, model) 对：
    模型粒度号池（pool.models 非空）= 勾选的模型；渠道粒度号池 = 池内全部渠道的可对话模型。
    extra_cats 仅列表/显式调用校验时放行（如图像生成）；auto 轮询不传。"""
    out = []
    if pl.get("models"):
        provs = {p["id"]: p for p in d["providers"]}
        for item in pl["models"]:
            pid, _, m = str(item or "").partition("::")
            p = provs.get(pid)
            if not p or not p.get("base_url"):
                continue
            if m in chat_candidates(d, p, extra_cats=extra_cats):
                out.append((p, m))
        return out
    for p in d["providers"]:
        if p.get("pool_id") == pl["id"] and p.get("base_url"):
            out.extend((p, m) for m in chat_candidates(d, p, extra_cats=extra_cats))
    return out


def pool_scope_set(d, pl, extra_cats=None):
    return {(p["id"], m) for p, m in pool_scope_candidates(d, pl, extra_cats=extra_cats)}


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

# dsh 面板注入的模型卡片从 3080 端口页面跨源调用本 API，需要 CORS 放行
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:3080", "http://localhost:3080", "https://zxc66.asia"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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
        "wb_quota": d.get("wb_quota") or {"accounts": [], "rates": {}, "synced_at": ""},
        "bl_installed": bool(bl),
        "hub_base": HUB_BASE,
        "hub_key": get_hub_key(d),
        "pools": _pool_view(d),
        "harness_model": d.get("harness_model") or "auto",
        "autostart_harness": bool(d.get("autostart_harness", True)),
        "copilot_quota": d.get("copilot_quota") or {},
        "autostart_copilot": bool(d.get("autostart_copilot", True)),
        "call_config": d.get("call_config") or {"mode": "lan", "public_host": ""},
        "lan_ip": _lan_ip(),
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "wb_status": {"installed": (CLIPROXY_DIR / "plugins" / "workbuddy.dll").exists(),
                      "logged_in": wb_auth_exists(),
                      "account": wb_account_info()},
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
    # 渠道身份过滤：WB 卡只留 WB 自家模型；AG(8317 非 wb)卡排除 WB 模型，两卡各管各的
    if p.get("kind") == "wb":
        ids = [m for m in ids if str(m).startswith(WB_MODEL_PREFIXES)]
    elif "8317" in (p.get("base_url") or ""):
        ids = [m for m in ids if not str(m).startswith(WB_MODEL_PREFIXES)]
    if p.get("type") == "bailian":
        # 百炼（2026-09-21 用户拍板）：只保留「文本生成 / 多模态」且当前有可用免费额度的模型；
        # 音频 / 向量 / 重排序 / 图像生成与 237 个无免费额度模型一律不进列表。
        quota_ready = bool((d.get("quota") or {}).get("entries"))
        filtered = []
        for m in ids:
            if model_category(m) not in ("文本生成", "多模态"):
                continue
            if quota_ready and quota_state(d, m)[0] != "free_ok":
                continue
            filtered.append(m)
        if filtered:
            ids = filtered
        elif p.get("models"):
            # 过滤后为空（如额度数据未同步/全过期）→ 保留旧列表，避免误清空
            ids = p["models"]
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


# ---------------- 号池（号池 → 账号 → 模型）与模型勾选 ----------------

class PoolIn(BaseModel):
    name: str
    models: list[str] | None = None     # 模型粒度成员：["渠道id::模型id", ...]


class PoolUpdate(BaseModel):
    name: str | None = None
    provider_id: str | None = None      # 把该渠道移入本号池
    reset_key: bool = False             # 重置号池 key（旧 key 立即失效）
    models: list[str] | None = None     # 更新模型粒度成员
    clear_models: bool = False          # 清掉模型粒度成员，改回渠道粒度


class EnabledIn(BaseModel):
    model: str
    enabled: bool


class EnabledBulkIn(BaseModel):
    enabled: bool
    models: list[str] | None = None     # None = 本渠道全部模型


def _norm_pool_models(d, items):
    """规范化模型粒度成员：["渠道id::模型id"]；校验渠道与模型存在。"""
    provs = {p["id"]: p for p in d["providers"]}
    out = []
    for it in items or []:
        pid, _, m = str(it or "").partition("::")
        p = provs.get(pid)
        if not p:
            raise HTTPException(400, "渠道不存在：%s" % pid)
        if m not in (p.get("models") or []):
            raise HTTPException(400, "渠道 %s 下没有模型 %s" % (p.get("name"), m))
        if it not in out:
            out.append(it)
    return out


def _pool_view(d):
    _ensure_pool_keys(d)
    out = []
    for pl in d.get("pools") or []:
        provs = [p for p in d["providers"] if p.get("pool_id") == pl["id"]]
        out.append({"id": pl["id"], "name": pl.get("name") or pl["id"],
                    "key": pl.get("key") or "",
                    "models": pl.get("models") or [],
                    "providers": [p["id"] for p in provs]})
    known = {pl["id"] for pl in d.get("pools") or []}
    rest = [p for p in d["providers"] if p.get("pool_id") not in known]
    if rest:
        out.append({"id": "", "name": "未分组", "key": "", "models": [],
                    "providers": [p["id"] for p in rest]})
    return out


@app.get("/api/pools")
def list_pools():
    d = load_data()
    return {"pools": _pool_view(d)}


@app.post("/api/pools")
def add_pool(inp: PoolIn):
    d = load_data()
    _ensure_pool_keys(d)
    pl = {"id": "pool_" + uuid.uuid4().hex[:8],
          "name": inp.name.strip() or "未命名号池",
          "key": _pool_new_key(),
          "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if inp.models is not None:
        pl["models"] = _norm_pool_models(d, inp.models)
    d["pools"].append(pl)
    save_data(d)
    _dsh_sync_pools(load_data())
    return pl


@app.put("/api/pools/{pool_id}")
def update_pool(pool_id: str, inp: PoolUpdate):
    d = load_data()
    pl = next((x for x in d["pools"] if x["id"] == pool_id), None)
    if not pl:
        raise HTTPException(404, "pool not found")
    if inp.name is not None:
        pl["name"] = inp.name.strip() or pl["name"]
    if inp.reset_key:
        pl["key"] = _pool_new_key()
    if inp.models is not None:
        pl["models"] = _norm_pool_models(d, inp.models)
    if inp.clear_models:
        pl.pop("models", None)
    if inp.provider_id:
        p = next((x for x in d["providers"] if x["id"] == inp.provider_id), None)
        if not p:
            raise HTTPException(404, "provider not found")
        p["pool_id"] = pool_id
    save_data(d)
    _dsh_sync_pools(load_data())
    return {"ok": True, "key": pl.get("key")}


@app.delete("/api/pools/{pool_id}")
def delete_pool(pool_id: str):
    d = load_data()
    if len(d["pools"]) <= 1:
        raise HTTPException(400, "至少保留一个号池")
    pl = next((x for x in d["pools"] if x["id"] == pool_id), None)
    if not pl:
        raise HTTPException(404, "pool not found")
    d["pools"] = [x for x in d["pools"] if x["id"] != pool_id]
    default_id = d["pools"][0]["id"]
    for p in d["providers"]:
        if p.get("pool_id") == pool_id:
            p["pool_id"] = default_id
    save_data(d)
    _dsh_sync_pools(load_data())
    return {"ok": True}


@app.post("/api/providers/{pid}/models/enabled")
def set_model_enabled(pid: str, inp: EnabledIn):
    d, p = get_provider(pid)
    dis = p.setdefault("disabled_models", [])
    if inp.enabled:
        p["disabled_models"] = [m for m in dis if m != inp.model]
    elif inp.model not in dis:
        dis.append(inp.model)
    save_data(d)
    _dsh_settings_refresh(d)
    return {"ok": True, "enabled": inp.enabled}


@app.post("/api/providers/{pid}/models/enabled_bulk")
def set_models_enabled_bulk(pid: str, inp: EnabledBulkIn):
    d, p = get_provider(pid)
    targets = inp.models if inp.models is not None else list(p.get("models") or [])
    dis = set(p.get("disabled_models") or [])
    if inp.enabled:
        dis -= set(targets)
    else:
        dis |= set(targets)
    p["disabled_models"] = sorted(dis)
    save_data(d)
    _dsh_settings_refresh(d)
    return {"ok": True, "disabled": len(p["disabled_models"])}


# ---------------- Harness 模型选择（Auto = 轮询 / 单选 = 锁定） ----------------

class HarnessModelIn(BaseModel):
    model: str = "auto"


def _harness_options(d):
    """可选模型 = 各渠道号池中已勾选、且满足免费额度/AG 组策略的可对话模型。"""
    opts = []
    for p in d["providers"]:
        if not p.get("base_url"):
            continue
        ms = chat_candidates(d, p)
        if ms:
            opts.append({"provider_id": p["id"],
                         "provider_name": p.get("name") or p["id"],
                         "models": ms})
    return opts


@app.get("/api/harness/model")
def get_harness_model():
    d = load_data()
    return {"model": d.get("harness_model") or "auto",
            "options": _harness_options(d)}


@app.post("/api/harness/model")
def set_harness_model(inp: HarnessModelIn):
    d = load_data()
    m = (inp.model or "auto").strip()
    if m != "auto":
        p = find_model_provider(d, m)
        if p is None:
            raise HTTPException(404, "模型不在任何渠道：%s" % m)
        if model_disabled(p, m):
            raise HTTPException(400, "模型未在号池中勾选：%s" % m)
        if model_category(m) not in CHAT_CATS:
            raise HTTPException(400, "该模型不是可对话模型：%s" % m)
    d["harness_model"] = m
    save_data(d)
    return {"ok": True, "model": m}


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
    if HUB_CLOUD: return {"ok": False, "message": "cloud mode: login locally then migrate auth files"}
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
            r = cli.get(CLIPROXY_BASE + "/v1/models",
                        headers={"Authorization": "Bearer " + ag_api_key()})
            if r.status_code == 200:
                data = r.json()
                items = data.get("data") or data.get("models") or []
                return len(items)
    except Exception:
        pass
    return 0


def _ag_restart_proxy():
    if HUB_CLOUD: return {"ok": False, "message": "cloud mode: proxy managed by container"}
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
        if "8317" in (p.get("base_url") or "") and p.get("kind") != "wb":
            hit = p
            break
    if not hit:
        return {"refreshed": False, "reason": "hub 里没有指向 8317 的渠道"}
    try:
        eff, ids, _ = fetch_models(hit["base_url"], hit.get("api_key") or ag_api_key())
    except Exception as e:
        return {"refreshed": False, "reason": str(e)[:200]}
    ids = [m for m in ids if not str(m).startswith(WB_MODEL_PREFIXES)]
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
    if HUB_CLOUD: return {"ok": False, "message": "cloud mode: login locally then migrate auth files"}
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


# ---------------- WorkBuddy（腾讯 CodeBuddy）反代授权 ----------------
# 与 AG 共用同一个 cli-proxy-api（:8317）进程，WB 以插件（plugins/workbuddy.dll）
# 形式注册 workbuddy provider；登录走 CPA 管理口（/v0/management）：
#   1) GET /workbuddy-auth-url   → {url, state}（腾讯 CodeBuddy 登录页，微信扫码/手机号/邮箱）
#   2) GET /get-auth-status?state=… → wait / ok / error
# 成功后 CPA 把凭证写进 ~/.cli-proxy-api/workbuddy.json，无需重启进程。

WB_MGMT_BASE = CLIPROXY_BASE + "/v0/management"
_wb_login = {"state": None, "url": None, "started_at": None,
             "status": None, "detail": None, "restarted": False}


def wb_mgmt_key():
    """CPA 管理密钥（首次启用插件时由安装脚本写入 cliproxy/mgmt_key.txt）"""
    f = CLIPROXY_DIR / "mgmt_key.txt"
    try:
        return f.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _wb_mgmt_get(path, timeout=15.0):
    key = wb_mgmt_key()
    if not key:
        raise HTTPException(400, "未找到 cliproxy/mgmt_key.txt（反代管理密钥）")
    with httpx.Client(timeout=timeout, trust_env=False) as cli:
        return cli.get(WB_MGMT_BASE + path,
                       headers={"X-Management-Key": key})


def _wb_auth_files():
    """插件 v0.8.5 起凭证按 uid 分文件存：workbuddy-<uid>.json；兼容旧版 workbuddy.json"""
    out = sorted(glob.glob(os.path.join(AG_AUTH_DIR, "workbuddy-*.json")))
    if os.path.exists(WB_AUTH_FILE):
        out.append(WB_AUTH_FILE)
    return out


def wb_auth_exists():
    return bool(_wb_auth_files())


def wb_account_info():
    """凭证文件的脱敏摘要（只取昵称/uid 等白名单字段，绝不外带 token）"""
    files = _wb_auth_files()
    if not files:
        return None
    try:
        with open(files[0], encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            acc, auth = d.get("account") or {}, d.get("auth") or {}
            info = {"nickname": acc.get("nickname"), "uid": acc.get("uid"),
                    "domain": auth.get("domain")}
            info = {k: v for k, v in info.items() if v}
            if info:
                return info
    except Exception:
        pass
    return {"file": os.path.basename(files[0])}


def wb_model_rates():
    """拉 CodeBuddy 官方目录的每模型积分倍率（只读目录接口，不消耗积分）。
    返回 {model_id: 倍率 float}，未标倍率的自动路由模型为 "auto"。"""
    files = _wb_auth_files()
    if not files:
        return {}
    with open(files[0], encoding="utf-8") as f:
        sa = json.load(f)
    auth, acc = sa.get("auth") or {}, sa.get("account") or {}
    tok = auth.get("accessToken") or ""
    if not tok:
        return {}
    headers = {
        "Authorization": "Bearer " + tok,
        "X-User-Id": acc.get("uid") or "",
        "X-Product": "SaaS",
        "X-IDE-Type": "CLI",
        "X-IDE-Name": "CLI",
        "Accept": "application/json",
        "Origin": "https://copilot.tencent.com",
        "Referer": "https://copilot.tencent.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CodeBuddy/1.0",
    }
    if auth.get("domain"):
        headers["X-Domain"] = auth["domain"]
    with httpx.Client(timeout=20.0, trust_env=False) as cli:
        r = cli.get("https://copilot.tencent.com/console/enterprises/personal/models",
                    headers=headers)
    r.raise_for_status()
    models = (r.json().get("data") or {}).get("models") or []
    rates = {}
    for m in models:
        mid, c = str(m.get("id") or ""), m.get("credits")
        if not mid:
            continue
        if not c:
            rates[mid] = "auto"          # 官方未标倍率 = 浮动 / 自动路由
            continue
        mm = re.match(r"x\s*([0-9]+(?:\.[0-9]+)?)", str(c).strip())
        rates[mid] = float(mm.group(1)) if mm else "unknown"
    return rates


@app.post("/api/wb/quota/refresh")
def wb_quota_refresh():
    """同步 CodeBuddy 积分（插件管理口实时查，不消耗积分）+ 每模型使用倍率。"""
    d = load_data()
    out = {"ok": False, "accounts": 0, "rates": 0}
    accounts = []
    try:
        r = _wb_mgmt_get("/plugins/workbuddy/credits", timeout=40.0)
        if r.status_code == 200:
            accounts = (r.json() or {}).get("accounts") or []
            out["ok"] = True
            out["accounts"] = len(accounts)
        else:
            out["error"] = "积分接口 HTTP %s：%s" % (r.status_code, r.text[:150])
    except HTTPException:
        raise
    except Exception as e:
        out["error"] = str(e)[:200]
    rates = {}
    try:
        rates = wb_model_rates()
        out["rates"] = len(rates)
    except Exception as e:
        out["rate_error"] = str(e)[:200]
    wq = d.setdefault("wb_quota", {"accounts": [], "rates": {}, "synced_at": ""})
    wq["accounts"] = accounts
    if rates:
        wq["rates"] = rates
    wq["synced_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_data(d)
    out["synced_at"] = wq["synced_at"]
    return out


def wb_models_count():
    """本地反代当前暴露的 wb 前缀模型数；拿不到 = 反代未跑或未登录"""
    try:
        with httpx.Client(timeout=8.0, trust_env=False) as cli:
            r = cli.get(CLIPROXY_BASE + "/v1/models",
                        headers={"Authorization": "Bearer " + ag_api_key()})
            if r.status_code == 200:
                items = r.json().get("data") or r.json().get("models") or []
                return len([m for m in items
                            if str(m.get("id", "")).startswith(WB_MODEL_PREFIXES)])
    except Exception:
        pass
    return 0


def _wb_refresh_hub_provider():
    """登录完成后，刷新 Hub 里 kind=wb 渠道的模型列表（只收 wb 前缀模型）"""
    d = load_data()
    hit = None
    for p in d.get("providers", []):
        if p.get("kind") == "wb":
            hit = p
            break
    if not hit:
        return {"refreshed": False, "reason": "hub 里没有 kind=wb 的渠道"}
    try:
        eff, ids, _ = fetch_models(hit["base_url"], hit.get("api_key") or ag_api_key())
    except Exception as e:
        return {"refreshed": False, "reason": str(e)[:200]}
    ids = [m for m in ids if str(m).startswith(WB_MODEL_PREFIXES)]
    hit["base_url"] = eff
    hit["models"] = ids
    hit["last_refresh"] = time.strftime("%Y-%m-%d %H:%M:%S")
    hit["fetch_error"] = None
    if ids and hit.get("active_model") not in ids:
        hit["active_model"] = ids[0]
    save_data(d)
    return {"refreshed": True, "count": len(ids)}


@app.get("/api/wb/status")
def wb_status():
    """WB 反代状态：插件是否装好 + 凭证是否已登录 + wb 模型数"""
    dll = CLIPROXY_DIR / "plugins" / "workbuddy.dll"
    return {"installed": dll.exists(),
            "logged_in": wb_auth_exists(),
            "account": wb_account_info(),
            "models_count": wb_models_count(),
            "mgmt_key_set": bool(wb_mgmt_key())}


@app.post("/api/wb/login")
def wb_login_start():
    if HUB_CLOUD: return {"ok": False, "message": "cloud mode: login locally then migrate auth files"}
    """向 CPA 管理口拿 WorkBuddy 登录链接（腾讯 CodeBuddy 登录页）。"""
    dll = CLIPROXY_DIR / "plugins" / "workbuddy.dll"
    if not dll.exists():
        raise HTTPException(400, "未检测到 cliproxy/plugins/workbuddy.dll 插件")
    if _wb_login.get("state") and _wb_login.get("status") == "wait":
        return {"ok": True, "already": True, "url": _wb_login.get("url"),
                "message": "WorkBuddy 登录已在进行中，请在打开的页面完成授权"}
    try:
        r = _wb_mgmt_get("/workbuddy-auth-url")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, "无法连接本地反代管理口（cli-proxy-api 未在运行？）：%r" % e)
    if r.status_code != 200:
        raise HTTPException(502, "管理口返回 %s：%s" % (r.status_code, r.text[:200]))
    data = r.json()
    url, state = data.get("url"), data.get("state")
    if not url or not state:
        raise HTTPException(502, "管理口未返回登录链接：" + r.text[:200])
    _wb_login.update({"state": state, "url": url,
                      "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                      "status": "wait", "detail": None, "restarted": False})
    return {"ok": True, "url": url, "state": state,
            "message": "已获取登录链接，请在新页面完成 CodeBuddy 登录（微信扫码 / 手机号 / 邮箱）"}


@app.get("/api/wb/login/status")
def wb_login_status():
    """前端轮询：查管理口登录进度；成功后刷新 Hub 的 wb 渠道模型列表。"""
    out = {"running": bool(_wb_login.get("state")) and _wb_login.get("status") == "wait",
           "started_at": _wb_login.get("started_at"),
           "logged_in": wb_auth_exists(),
           "models_count": wb_models_count(),
           "status": _wb_login.get("status"),
           "detail": _wb_login.get("detail")}
    if out["running"]:
        try:
            r = _wb_mgmt_get("/get-auth-status?state=" + _wb_login["state"])
            if r.status_code == 200:
                st = r.json()
                s = st.get("status")
                if s == "ok":
                    _wb_login["status"] = "ok"
                    out["status"] = "ok"
                    out["running"] = False
                    rf = _wb_refresh_hub_provider()
                    if not rf.get("count") and not _wb_login.get("restarted"):
                        # 模型没冒出来就重启一次反代兜底（与 AG 登录后处理一致）
                        _wb_login["restarted"] = True
                        _ag_restart_proxy()
                        out["proxy_restarted"] = True
                        rf = _wb_refresh_hub_provider()
                    out["hub_refresh"] = rf
                    out["models_count"] = wb_models_count()
                elif s == "error":
                    _wb_login["status"] = "error"
                    _wb_login["detail"] = (st.get("error") or st.get("message")
                                           or "登录失败")[:300]
                    out["status"] = "error"
                    out["detail"] = _wb_login["detail"]
                    out["running"] = False
                # "wait" → 继续等用户扫码
        except HTTPException:
            raise
        except Exception as e:
            out["poll_error"] = str(e)[:200]
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
    """鉴权：Hub 统一 key = 全池权限；号池 key = 仅该号池范围。
    返回命中的号池 dict（全池权限时为 None）。"""
    auth = req.headers.get("authorization") or ""
    key = auth[7:].strip() if auth.startswith("Bearer ") else ""
    if key and key == get_hub_key(d):
        return None
    if key:
        for pl in d.get("pools") or []:
            if pl.get("key") and key == pl["key"]:
                return pl
    raise HTTPException(status_code=401,
                        detail="未授权：请携带 Hub 统一 key（全池）或号池 key（Authorization: Bearer <key>）。")


@app.get("/v1/models")
def hub_models(req: Request):
    """统一模型清单：auto + 各渠道可对话模型 + 图像生成模型（供标准客户端拉列表，需 hub_key）。
    图像生成模型只在列表中可见、可显式指定调用；不进入 auto 轮询。"""
    d = load_data()
    scope = _require_hub_key(d, req)
    data = [{"id": "auto", "object": "model", "owned_by": "llm-hub"}]
    seen = {"auto"}
    if scope is not None:
        for p, m in pool_scope_candidates(d, scope, extra_cats={"图像生成"}):
            if m in seen:
                continue
            seen.add(m)
            data.append({"id": m, "object": "model",
                         "owned_by": scope.get("name") or "pool"})
        return {"object": "list", "data": data}
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
    scope = _require_hub_key(d, req)
    model = body.get("model") or "auto"
    stream = bool(body.get("stream"))

    # Harness 模型选择：锁定具体模型时，model=auto 的请求固定走该模型（不再轮询）。
    # 号池 key 的请求不受全局锁定影响（锁定是 Harness 全池面板的选择）。
    if model == "auto" and not req.headers.get("x-hub-provider") and scope is None:
        pin = (d.get("harness_model") or "auto").strip()
        if pin and pin != "auto":
            _pp = find_model_provider(d, pin)
            if _pp is None or model_disabled(_pp, pin):
                log_call({"source": "proxy", "provider": None,
                          "model_requested": "auto", "model_used": None,
                          "ok": False, "blocked": True, "latency_ms": 0,
                          "error": "锁定模型不可用：" + pin})
                return JSONResponse(status_code=409, content={"error": {
                    "message": "Harness 锁定模型 %s 已不可用（被删除或未在号池勾选）。请到 Harness 页重新选择，或切回 Auto。" % pin,
                    "type": "hub_pin_invalid"}})
            model = pin

    if req.headers.get("x-hub-provider"):
        p = _pick_provider(d, req)
        scoped = [(p, m) for m in (chat_candidates(d, p) if model == "auto" else [model])]
        if scope is not None:
            allowed = pool_scope_set(d, scope)
            scoped = [(p, m) for (p, m) in scoped if (p["id"], m) in allowed]
    elif model == "auto":
        scoped = pool_scope_candidates(d, scope) if scope is not None else all_chat_candidates(d)
    else:
        p = find_model_provider(d, model)
        if p is None:
            if scope is not None:
                raise HTTPException(404, "模型 %s 不在号池「%s」范围内" % (model, scope.get("name")))
            if not d["providers"]:
                raise HTTPException(400, "还没有任何渠道，请先打开 Hub 页面添加")
            p = d["providers"][0]
        scoped = [(p, model)]
        if scope is not None and (p["id"], model) not in pool_scope_set(d, scope, extra_cats={"图像生成"}):
            log_call({"source": "proxy", "provider": p.get("name"),
                      "model_requested": model, "model_used": model,
                      "ok": False, "blocked": True, "latency_ms": 0,
                      "error": "号池范围拦截：" + model})
            return JSONResponse(status_code=403, content={"error": {
                "message": "模型 %s 不在号池「%s」范围内，已拦截。" % (model, scope.get("name")),
                "type": "hub_pool_scope"}})

    if model == "auto":
        if not scoped:
            log_call({"source": "proxy", "provider": None,
                      "model_requested": "auto", "model_used": None,
                      "ok": False, "blocked": True, "latency_ms": 0,
                      "error": "没有可调用的免费额度模型"})
            if scope is not None:
                return JSONResponse(status_code=403, content={"error": {
                    "message": "号池「%s」内没有可调用的模型（未勾选模型 / 全部无免费额度 / 渠道为空）。" % scope.get("name"),
                    "type": "hub_pool_empty"}})
            return JSONResponse(status_code=403, content={"error": {
                "message": "没有可调用的免费额度模型（全部无额度/耗尽/过期）。如确认付费调用，请在 Hub 页面勾选「允许付费」。",
                "type": "hub_no_free_model"}})
    else:
        if model_disabled(p, model):
            log_call({"source": "proxy", "provider": p.get("name"),
                      "model_requested": model, "model_used": model,
                      "ok": False, "blocked": True, "latency_ms": 0,
                      "error": "号池未勾选拦截"})
            return JSONResponse(status_code=403, content={"error": {
                "message": "模型 %s 未在号池中勾选，已拦截。请到渠道管理页勾选后再调用。" % model,
                "type": "hub_model_disabled"}})
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


# ---------------- Claude Code 适配（Anthropic Messages API） ----------------

def _require_hub_key_anth(d, req):
    """同 _require_hub_key，另接受 x-api-key；返回号池 scope（全池为 None）。"""
    auth = req.headers.get("authorization") or ""
    xkey = req.headers.get("x-api-key") or ""
    key = auth[7:].strip() if auth.startswith("Bearer ") else xkey.strip()
    if key and key == get_hub_key(d):
        return None
    if key:
        for pl in d.get("pools") or []:
            if pl.get("key") and key == pl["key"]:
                return pl
    raise HTTPException(status_code=401, detail={
        "type": "error", "error": {"type": "authentication_error",
        "message": "invalid x-api-key / bearer token（用 Hub 统一 key（全池）或号池 key）"}})


def _anth_to_openai(body):
    """Anthropic Messages 请求 -> OpenAI chat.completions 请求"""
    msgs = []
    sys = body.get("system")
    if isinstance(sys, str) and sys:
        msgs.append({"role": "system", "content": sys})
    elif isinstance(sys, list):
        txt = "".join(b.get("text", "") for b in sys if isinstance(b, dict) and b.get("type") == "text")
        if txt:
            msgs.append({"role": "system", "content": txt})
    for m in body.get("messages") or []:
        role = m.get("role") or "user"
        c = m.get("content")
        if isinstance(c, str):
            msgs.append({"role": role, "content": c})
            continue
        texts, tool_calls, tool_results = [], [], []
        for b in c or []:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                texts.append(b.get("text", ""))
            elif t == "tool_use":
                tool_calls.append({"id": b.get("id") or ("call_" + uuid.uuid4().hex[:16]),
                                   "type": "function",
                                   "function": {"name": b.get("name"),
                                                "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}})
            elif t == "tool_result":
                tool_results.append(b)
        for tr in tool_results:
            cont = tr.get("content")
            if isinstance(cont, list):
                cont = "".join(x.get("text", "") for x in cont if isinstance(x, dict) and x.get("type") == "text")
            msgs.append({"role": "tool",
                         "tool_call_id": tr.get("tool_use_id") or "",
                         "content": cont if isinstance(cont, str) else json.dumps(cont, ensure_ascii=False)})
        if texts or tool_calls:
            msg = {"role": role, "content": "\n".join(texts) if texts else None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            msgs.append(msg)
    out = {"model": body.get("model") or "auto", "messages": msgs,
           "stream": bool(body.get("stream"))}
    if body.get("max_tokens"):
        out["max_tokens"] = body["max_tokens"]
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        out["top_p"] = body["top_p"]
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]
    tools = body.get("tools")
    if tools:
        out["tools"] = [{"type": "function",
                         "function": {"name": t.get("name"),
                                      "description": t.get("description") or "",
                                      "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
                        for t in tools]
        tc = body.get("tool_choice")
        if isinstance(tc, dict):
            if tc.get("type") == "tool":
                out["tool_choice"] = {"type": "function", "function": {"name": tc.get("name")}}
            elif tc.get("type") == "any":
                out["tool_choice"] = "required"
            elif tc.get("type") == "auto":
                out["tool_choice"] = "auto"
    return out


def _openai_to_anth(resp, model):
    """OpenAI chat.completions 响应 -> Anthropic Messages 响应"""
    ch = (resp.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    content = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {}
        content.append({"type": "tool_use", "id": tc.get("id") or ("toolu_" + uuid.uuid4().hex[:16]),
                        "name": fn.get("name"), "input": args})
    fr = ch.get("finish_reason")
    stop_reason = {"stop": "end_turn", "length": "max_tokens",
                   "tool_calls": "tool_use", "content_filter": "refusal"}.get(fr, "end_turn")
    u = resp.get("usage") or {}
    return {"id": resp.get("id") or ("msg_" + uuid.uuid4().hex[:24]),
            "type": "message", "role": "assistant", "content": content,
            "model": model, "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": u.get("prompt_tokens") or 0,
                      "output_tokens": u.get("completion_tokens") or 0}}


def _anth_err(status, message):
    return JSONResponse(status_code=status,
                        content={"type": "error", "error": {"type": "api_error", "message": message}})


async def _anth_forward_stream(p, body, model, model_requested):
    """流式：上游 OpenAI SSE -> Anthropic SSE 事件序列"""
    b = dict(body)
    b["model"] = model
    b["stream"] = True
    headers = {"Authorization": "Bearer " + p["api_key"], "Content-Type": "application/json"}
    url = p["base_url"].rstrip("/") + "/chat/completions"
    t0 = time.time()
    client = httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=20.0), trust_env=False)
    request = client.build_request("POST", url, json=b, headers=headers)
    resp = await client.send(request, stream=True)

    async def gen():
        msg_id = "msg_" + uuid.uuid4().hex[:24]
        blocks = {}          # 打开的块: "text" / "tcN" -> anth 块 index
        block_open = None
        next_idx = 0
        stop_reason = "end_turn"
        out_tokens = 0

        def sse(ev, data):
            return ("event: %s\ndata: %s\n\n" % (ev, json.dumps(data, ensure_ascii=False))).encode()

        try:
            if resp.status_code != 200:
                raw = await resp.aread()
                yield sse("error", {"type": "error", "error": {"type": "api_error",
                          "message": "upstream HTTP %d: %s" % (resp.status_code, raw[:300].decode("utf-8", "replace"))}})
                return
            yield sse("message_start", {"type": "message_start", "message": {
                "id": msg_id, "type": "message", "role": "assistant", "content": [],
                "model": model_requested or model, "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0}}})
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    if payload == "[DONE]":
                        break
                    continue
                try:
                    chunk = json.loads(payload)
                except Exception:
                    continue
                if isinstance(chunk.get("usage"), dict):
                    out_tokens = chunk["usage"].get("completion_tokens") or out_tokens
                ch = (chunk.get("choices") or [{}])[0]
                delta = ch.get("delta") or {}
                fr = ch.get("finish_reason")
                txt = delta.get("content")
                if txt:
                    if block_open != "text":
                        if block_open is not None:
                            yield sse("content_block_stop", {"type": "content_block_stop", "index": blocks[block_open]})
                        blocks["text"] = next_idx
                        next_idx += 1
                        block_open = "text"
                        yield sse("content_block_start", {"type": "content_block_start",
                                  "index": blocks["text"], "content_block": {"type": "text", "text": ""}})
                    yield sse("content_block_delta", {"type": "content_block_delta",
                              "index": blocks["text"], "delta": {"type": "text_delta", "text": txt}})
                for tc in delta.get("tool_calls") or []:
                    key = "tc%d" % (tc.get("index") or 0)
                    fn = tc.get("function") or {}
                    if key not in blocks:
                        if block_open is not None:
                            yield sse("content_block_stop", {"type": "content_block_stop", "index": blocks[block_open]})
                        blocks[key] = next_idx
                        next_idx += 1
                        block_open = key
                        yield sse("content_block_start", {"type": "content_block_start",
                                  "index": blocks[key],
                                  "content_block": {"type": "tool_use",
                                                    "id": tc.get("id") or ("toolu_" + uuid.uuid4().hex[:16]),
                                                    "name": fn.get("name") or "", "input": {}}})
                    if fn.get("arguments"):
                        yield sse("content_block_delta", {"type": "content_block_delta",
                                  "index": blocks[key],
                                  "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]}})
                if fr:
                    stop_reason = {"stop": "end_turn", "length": "max_tokens",
                                   "tool_calls": "tool_use", "content_filter": "refusal"}.get(fr, "end_turn")
            if block_open is not None:
                yield sse("content_block_stop", {"type": "content_block_stop", "index": blocks[block_open]})
            yield sse("message_delta", {"type": "message_delta",
                      "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                      "usage": {"output_tokens": out_tokens}})
            yield sse("message_stop", {"type": "message_stop"})
        finally:
            ms = int((time.time() - t0) * 1000)
            log_call({"source": "proxy-anthropic", "provider": p.get("name"),
                      "model_requested": model_requested or model, "model_used": model,
                      "ok": resp.status_code == 200, "status": resp.status_code,
                      "latency_ms": ms, "stream": True,
                      "error": None if resp.status_code == 200 else "stream HTTP %d" % resp.status_code})
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(gen(), status_code=200, media_type="text/event-stream",
                             headers={"X-Hub-Model": model, "X-Hub-Provider": p.get("id")})


@app.post("/v1/messages")
async def anth_messages(req: Request):
    """Claude Code / Anthropic Messages API 统一入口。
    鉴权：x-api-key 或 Authorization: Bearer（均为 Hub 统一 key）。
    model 不在任何渠道列表时自动落到 auto（跨渠道轮动）；支持 tools / system / 流式。"""
    body = await req.json()
    d = load_data()
    scope = _require_hub_key_anth(d, req)
    model = body.get("model") or "auto"
    if find_model_provider(d, model) is None:
        model = "auto"
    if model == "auto" and not req.headers.get("x-hub-provider") and scope is None:
        pin = (d.get("harness_model") or "auto").strip()
        if pin and pin != "auto":
            _pp = find_model_provider(d, pin)
            if _pp is None or model_disabled(_pp, pin):
                return _anth_err(409, "Harness 锁定模型 %s 已不可用，请到 Harness 页重新选择或切回 Auto。" % pin)
            model = pin
    obody = _anth_to_openai(body)
    obody["model"] = model
    stream = obody["stream"]

    if req.headers.get("x-hub-provider"):
        p = _pick_provider(d, req)
        scoped = [(p, m) for m in (chat_candidates(d, p) if model == "auto" else [model])]
        if scope is not None:
            allowed = pool_scope_set(d, scope)
            scoped = [(p, m) for (p, m) in scoped if (p["id"], m) in allowed]
    elif model == "auto":
        scoped = pool_scope_candidates(d, scope) if scope is not None else all_chat_candidates(d)
    else:
        p = find_model_provider(d, model)
        scoped = [(p, model)]
        if scope is not None and (p["id"], model) not in pool_scope_set(d, scope, extra_cats={"图像生成"}):
            return _anth_err(403, "模型 %s 不在号池「%s」范围内，已拦截。" % (model, scope.get("name")))
    if not scoped:
        log_call({"source": "proxy-anthropic", "provider": None,
                  "model_requested": model, "model_used": None,
                  "ok": False, "blocked": True, "latency_ms": 0,
                  "error": "没有可调用的免费额度模型"})
        return _anth_err(403, "没有可调用的免费额度模型（全部无额度/耗尽/过期）。")
    if model != "auto":
        p0 = scoped[0][0]
        if model_disabled(p0, model):
            return _anth_err(403, "模型 %s 未在号池中勾选，已拦截。" % model)
        if is_ag_provider(p0):
            agst, _r5, _rw, cd = ag_group_state(d, model)
            if agst in ("empty", "cooldown"):
                gname = AG_GROUP_TITLES.get(ag_model_group(model), "额度组")
                msg = ("%s额度已耗尽，正在冷却至 %s" % (gname, cd)) if agst == "cooldown" else                       ("%s的 5 小时 / 每周额度已耗尽，等待重置后自动恢复" % gname)
                log_call({"source": "proxy-anthropic", "provider": p0.get("name"),
                          "model_requested": model, "model_used": model,
                          "ok": False, "blocked": True, "latency_ms": 0,
                          "error": "AG 组额度拦截：" + msg})
                return _anth_err(429, "模型 %s 所属%s。" % (model, msg))

    if stream:
        p0, m0 = scoped[0]
        return await _anth_forward_stream(p0, obody, m0, model)

    last = None
    first_model = scoped[0][1]
    for p, m in scoped:
        code, resp, ms = await _forward_chat(p, obody, m)
        usage = resp.get("usage") if code == 200 and isinstance(resp, dict) else None
        u = usage or {}
        log_call({"source": "proxy-anthropic", "provider": p.get("name"),
                  "model_requested": body.get("model") or "auto", "model_used": m,
                  "ok": code == 200, "status": code, "latency_ms": ms,
                  "prompt_tokens": u.get("prompt_tokens"),
                  "completion_tokens": u.get("completion_tokens"),
                  "total_tokens": u.get("total_tokens"),
                  "rotated": (model == "auto" and m != first_model),
                  "error": None if code == 200 else
                           (json.dumps(resp, ensure_ascii=False)[:300] if isinstance(resp, dict) else str(resp)[:300])})
        if code == 200:
            return JSONResponse(content=_openai_to_anth(resp, m),
                                headers={"X-Hub-Model": m, "X-Hub-Provider": p.get("id")})
        last = (p, m, code, resp)
        if not _quota_like(code, resp):
            break
    p, m, code, resp = last
    emsg = ""
    if isinstance(resp, dict):
        emsg = (resp.get("error") or {}).get("message") or json.dumps(resp, ensure_ascii=False)
    else:
        emsg = str(resp)
    return JSONResponse(status_code=code if 100 <= code <= 599 else 502,
                        content={"type": "error", "error": {"type": "api_error",
                                 "message": emsg[:500]}},
                        headers={"X-Hub-Model": m, "X-Hub-Provider": p.get("id")})


@app.post("/v1/messages/count_tokens")
async def anth_count_tokens(req: Request):
    """Claude Code 启动时探测用；按字符粗略估算（~4 字符 1 token）。"""
    body = await req.json()
    d = load_data()
    _require_hub_key_anth(d, req)
    n = len(json.dumps(body.get("messages") or [], ensure_ascii=False))
    n += len(json.dumps(body.get("system") or "", ensure_ascii=False))
    n += len(json.dumps(body.get("tools") or [], ensure_ascii=False))
    return {"input_tokens": max(1, n // 4)}


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




# ---------------- DeepSeek Harness（dsh）子窗口管理 ----------------

DSH_PORT = 3080
# DSH HOME: 优先环境变量 DSH_HOME，否则 F 盘迁移目标，否则回落到本机 ~/.dsh（兼容老位置）
_DSH_HOME_FALLBACK = Path(r"F:\deepseek-harness\home")
DSH_HOME = Path(os.environ.get("DSH_HOME") or (_DSH_HOME_FALLBACK if _DSH_HOME_FALLBACK.exists() else (Path.home() / ".dsh")))
DSH_SETTINGS = DSH_HOME / "settings.yaml"
DSH_PID_FILE = BASE_DIR / "dsh.pid"
DSH_LOG_FILE = BASE_DIR / "logs" / "dsh.log"

_DSH_HUB_BASE = "http://127.0.0.1:8787/v1"

# 渠道分组号池（2026-09-21 用户拍板：dsh 模型面板 = Auto + 五大渠道区块，方便管理）。
# 每区块一个模型粒度号池，池内模型随渠道勾选/额度自动同步；顺序即 dsh 面板顺序。
CHANNEL_POOLS = [
    ("pool_ch_deepseek", "DeepSeek 原生", "deepseek01"),
    ("pool_ch_ag", "反重力 Antigravity", "eb00387935"),
    ("pool_ch_copilot", "GitHub Copilot", "83c06be0ee"),
    ("pool_ch_wb", "WorkBuddy", "wb001"),
    ("pool_ch_bailian", "百炼", "bailian001"),
]


def _ensure_channel_pools(d):
    """确保五个渠道分组号池存在，并把池内模型同步为渠道当前可对话模型（模型粒度号池）。"""
    pools = d.setdefault("pools", [])
    by_id = {pl.get("id"): pl for pl in pools}
    provs = {p.get("id"): p for p in d.get("providers") or []}
    changed = False
    for pool_id, name, pid in CHANNEL_POOLS:
        p = provs.get(pid)
        if not p:
            continue
        pl = by_id.get(pool_id)
        if not pl:
            pl = {"id": pool_id, "name": name, "created_at": now_str(),
                  "key": "", "channel_pid": pid, "models": []}
            pools.append(pl)
            by_id[pool_id] = pl
            changed = True
        pl["channel_pid"] = pid
        want = [pid + "::" + m for m in chat_candidates(d, p)]
        if pl.get("models") != want:
            pl["models"] = want
            changed = True
    if changed:
        for pl in pools:
            if not pl.get("key"):
                pl["key"] = _pool_new_key()
        save_data(d)
    return changed


def _dsh_route_key(pl):
    """号池在 dsh settings.yaml 里的 provider 键（小写连字符文法）。"""
    base = re.sub(r"[^a-z0-9]+", "-", str(pl.get("id") or "pool").lower()).strip("-")
    return "llm-hub-" + (base or "pool")


def _dsh_env_name(pl):
    """号池 key 注入 dsh 进程的环境变量名。"""
    return "HUB_POOL_KEY_" + re.sub(r"[^A-Z0-9]+", "_", str(pl.get("id") or "POOL").upper()).strip("_")


def _dsh_read_old_settings():
    try:
        old = yaml.safe_load(DSH_SETTINGS.read_text(encoding="utf-8"))
        return old if isinstance(old, dict) else {}
    except Exception:
        return {}


def _dsh_render_settings(d):
    """按号池分组重写 dsh settings.yaml：
    顶部 llm-hub = 全部号池共用的 key/URL（dsh 下拉「Hub · 全部号池」组）；
    其余每个号池一个分组（各自 key、各自 auto 轮询）。
    保留用户在 dsh 侧的其它 provider / 默认模型选择 / 顶层键；
    llm-hub* 前缀的 provider 由 Hub 管理，手动改动会在同步时被覆盖。"""
    _ensure_channel_pools(d)
    old = _dsh_read_old_settings()
    lpa_old = old.get("llm-pi-ai")
    provs_old = (lpa_old.get("providers") or {}) if isinstance(lpa_old, dict) else {}
    compat = {"supportsDeveloperRole": False, "maxTokensField": "max_tokens"}
    providers = {}
    # 顶部 Auto：全池 key，只有一个 auto（轮询 Hub 全部渠道）
    providers["llm-hub"] = {
        "api": "openai-completions",
        "baseURL": _DSH_HUB_BASE,
        "apiKeyEnv": "HUB_API_KEY",
        "displayName": "Auto · 全部渠道（轮询）",
        "compat": compat,
        "models": [{"id": "auto"}],
    }
    used = {"llm-hub"}
    # 五大渠道区块：各自 key、组内 auto + 详细模型列表；顺序按 CHANNEL_POOLS
    order = [cp[0] for cp in CHANNEL_POOLS]
    ch_pools = [pl for pl in (d.get("pools") or []) if pl.get("channel_pid")]
    ch_pools.sort(key=lambda pl: order.index(pl["id"]) if pl.get("id") in order else 99)
    for i, pl in enumerate(ch_pools):
        rk = _dsh_route_key(pl)
        while rk in used:
            rk += "-%d" % i
        used.add(rk)
        seen_m, mlist = set(), []
        for _p, m in pool_scope_candidates(d, pl):
            if m in seen_m:
                continue
            seen_m.add(m)
            mlist.append({"id": m})
        providers[rk] = {
            "api": "openai-completions",
            "baseURL": _DSH_HUB_BASE,
            "apiKeyEnv": _dsh_env_name(pl),
            "displayName": pl.get("name") or pl.get("id") or "",
            "compat": compat,
            "models": [{"id": "auto"}] + mlist,
        }
    for k, v in provs_old.items():
        if not str(k).startswith("llm-hub"):
            providers[k] = v
    out = dict(old)
    out["llm-pi-ai"] = {"providers": providers}
    # 默认模型：尽量保留用户选择；模型已不在原分组则改指到包含它的分组，都没有回退全池 auto
    adm = old.get("agent-default-model") or {}
    am, ap = adm.get("model"), adm.get("provider")
    def _ids(pv):
        return [m.get("id") if isinstance(m, dict) else m for m in (pv.get("models") or [])]
    valid = ap in providers and am in _ids(providers[ap])
    if not valid and am:
        for rk, pv in providers.items():
            if am in _ids(pv):
                ap, valid = rk, True
                break
    if not valid:
        ap, am = "llm-hub", "auto"
    out["agent-default-model"] = {"provider": ap, "model": am}
    header = ("# 由 LLM Key Hub 自动生成（Auto + 渠道区块版，2026-09-21）。\n"
              "# 顶部 llm-hub = Auto · 全部渠道（全池 key，仅 auto）；\n"
              "# 其下五个渠道区块各用渠道号池 key（组内 auto 轮询 + 详细模型列表）。\n"
              "# llm-hub* 前缀的 provider 由 Hub 管理并在同步时被重写；其余键原样保留。\n")
    DSH_HOME.mkdir(parents=True, exist_ok=True)
    text = header + yaml.safe_dump(out, allow_unicode=True, sort_keys=False)
    DSH_SETTINGS.write_text(text, encoding="utf-8")
    return True


def _dsh_settings_refresh(d):
    """仅重写 settings.yaml（dsh 按请求重读，无需重启）；用于模型勾选等不影响 key 的变更。"""
    try:
        _dsh_render_settings(d)
    except Exception as e:
        print("[dsh-sync] settings refresh failed: %r" % e)


def _dsh_sync_pools(d):
    """号池/渠道结构变更后调用：重写 settings.yaml；dsh 在运行则重启注入新号池 key。"""
    try:
        _dsh_render_settings(d)
    except Exception as e:
        return {"ok": False, "message": "写入 %s 失败：%r" % (DSH_SETTINGS, e)}
    if _dsh_http_up():
        _dsh_stop()
        time.sleep(0.5)
        r = _dsh_start()
        r.setdefault("ok", True)
        r["restarted"] = True
        return r
    return {"ok": True, "restarted": False}


def _dsh_http_up():
    if HUB_CLOUD: return False
    try:
        r = httpx.get("http://127.0.0.1:%d/" % DSH_PORT, timeout=1.5)
        # 401 = dsh 活着且要求 token(默认鉴权),也算运行中
        return r.status_code in (200, 401)
    except Exception:
        return False


def _dsh_token_url():
    """从 dsh.log 解析最后一次启动的带 token URL。"""
    fallback = "http://127.0.0.1:%d/" % DSH_PORT
    try:
        text = DSH_LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return fallback
    url = None
    for ln in text.splitlines():
        if "token=" in ln and "http" in ln:
            i = ln.find("http")
            url = ln[i:].strip().split()[0]
    return url or fallback


def _dsh_pid_by_port():
    try:
        out = subprocess.run("netstat -ano | findstr :%d" % DSH_PORT, shell=True,
                             capture_output=True, text=True, timeout=5).stdout or ""
    except Exception:
        return None
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) >= 5 and ":%d" % DSH_PORT in parts[1] and parts[3] == "LISTENING":
            try:
                return int(parts[-1])
            except ValueError:
                pass
    return None


def _dsh_status():
    return {
        "running": _dsh_http_up(),
        "port": DSH_PORT,
        "url": _dsh_token_url(),
        "settings_ok": DSH_SETTINGS.exists(),
        "settings_path": str(DSH_SETTINGS),
    }


def _dsh_start():
    if HUB_CLOUD: return {"ok": False, "message": "cloud mode: dsh runs locally only"}
    if _dsh_http_up():
        return {"ok": True, "started": False, "message": "已在运行"}
    d = load_data()
    _ensure_pool_keys(d)
    try:
        _dsh_render_settings(d)
    except Exception as e:
        return {"ok": False, "message": "写入 %s 失败：%r" % (DSH_SETTINGS, e)}
    dsh = shutil.which("dsh")
    if not dsh:
        cand = r"F:\deepseek-harness\app\node_modules\.bin\dsh.CMD"
        if os.path.exists(cand):
            dsh = cand
    if not dsh:
        return {"ok": False, "message": "未找到 dsh 命令，请先在 F:\\deepseek-harness\\app 执行 npm install @deepseek-ai/dsh"}
    env = dict(os.environ)
    env["HUB_API_KEY"] = get_hub_key(d)
    env["DSH_HOME"] = str(DSH_HOME).strip()
    for pl in d.get("pools") or []:
        if pl.get("key"):
            env[_dsh_env_name(pl)] = pl["key"]
    # 直接以列表形式启动 node.exe 跑 dsh 的 bin.js,绕开 .CMD 与 cmd.exe 两层包装。
    # 实测:DETACHED + cmd.exe 的 `>>` 重定向链路里,cmd 内部命令(echo)能落盘,
    # 但 node 的 stdout 完全不落盘,dsh web 的 token URL 永远进不了 dsh.log,
    # /harness 页 iframe 因此 401。改由 Python 自己 open 日志句柄交给子进程继承。
    node_exe = shutil.which("node")
    if not node_exe:
        cand = r"C:\Program Files\nodejs\node.exe"
        if os.path.exists(cand):
            node_exe = cand
    if not node_exe:
        return {"ok": False, "message": "未找到 node.exe"}
    bin_js = r"F:\deepseek-harness\app\node_modules\@deepseek-ai\dsh\lib\bin.js"
    if not os.path.exists(bin_js) and dsh:
        bin_js = os.path.normpath(os.path.join(
            os.path.dirname(str(dsh)), "..", "@deepseek-ai", "dsh", "lib", "bin.js"))
    if not os.path.exists(bin_js):
        return {"ok": False, "message": "未找到 dsh 入口 bin.js"}
    try:
        DSH_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(DSH_LOG_FILE, "ab")
        try:
            proc = subprocess.Popen(
                [node_exe, bin_js, "web", "--no-open"],
                cwd=str(BASE_DIR), env=env,
                stdout=log_fh, stderr=subprocess.STDOUT,
                creationflags=0x00000008 | 0x00000200)
        finally:
            log_fh.close()
    except Exception as e:
        return {"ok": False, "message": "启动失败：%r" % e}
    time.sleep(0.3)
    real_pid = _dsh_pid_by_port()
    if real_pid:
        try:
            DSH_PID_FILE.write_text(str(real_pid), encoding="utf-8")
        except Exception:
            pass
    for _ in range(60):
        if _dsh_http_up():
            return {"ok": True, "started": True, "message": "dsh web 已启动"}
        time.sleep(0.5)
    return {"ok": False, "message": "dsh 已拉起但端口 %d 在 30 秒内未就绪，详见 logs/dsh.log" % DSH_PORT}


def _dsh_stop():
    if HUB_CLOUD: return {"ok": False, "message": "cloud mode: dsh runs locally only"}
    pid = _dsh_pid_by_port()
    if not pid:
        try:
            pid = int(DSH_PID_FILE.read_text(encoding="utf-8").strip())
        except Exception:
            pid = None
    if not pid:
        return {"ok": True, "message": "未在运行"}
    subprocess.run("taskkill /PID %d /T /F" % pid, shell=True, capture_output=True)
    time.sleep(1)
    if _dsh_http_up():
        return {"ok": False, "message": "停止失败（pid %d），见 logs/dsh.log" % pid}
    return {"ok": True, "message": "已停止"}


@app.get("/static/{filename}")
def static_files(filename: str):
    fn = (STATIC_DIR / filename).resolve()
    try:
        fn.relative_to(STATIC_DIR.resolve())
    except ValueError:
        raise HTTPException(404)
    if not fn.is_file():
        raise HTTPException(404)
    return FileResponse(fn)


@app.get("/harness")
def harness_page():
    return FileResponse(STATIC_DIR / "harness.html")


@app.get("/api/harness/status")
def api_harness_status():
    return _dsh_status()


@app.post("/api/harness/start")
def api_harness_start():
    return _dsh_start()


@app.post("/api/harness/stop")
def api_harness_stop():
    return _dsh_stop()


@app.post("/api/harness/dsh-sync")
def api_harness_dsh_sync():
    """按号池分组重建 dsh settings.yaml（顶部全池 + 每号池一组）；dsh 在跑则重启注入 key。"""
    return _dsh_sync_pools(load_data())


# ---------------- Copilot 反代（copilot-api，端口 4141） ----------------

COPILOT_DIR = BASE_DIR / "copilot_api"
COPILOT_MAIN = COPILOT_DIR / "node_modules" / "copilot-api" / "dist" / "main.js"
COPILOT_PORT = 4141
COPILOT_LOG_FILE = BASE_DIR / "logs" / "copilot.log"


def _copilot_http_up():
    if HUB_CLOUD:
        try:
            r = httpx.get(COPILOT_BASE + "/v1/models", timeout=2.0)
            return r.status_code < 500
        except Exception:
            return False
    try:
        r = httpx.get("http://127.0.0.1:%d/v1/models" % COPILOT_PORT,
                      timeout=3.0, trust_env=False)
        return r.status_code == 200
    except Exception:
        return False


def _copilot_start():
    if HUB_CLOUD: return {"ok": False, "message": "cloud mode: copilot-api managed by container"}
    """拉起 copilot-api server（GitHub Copilot 反代，OpenAI/Anthropic 兼容出口）。"""
    if _copilot_http_up():
        return {"ok": True, "started": False, "message": "已在运行"}
    if not COPILOT_MAIN.exists():
        return {"ok": False, "message": "未找到 copilot-api（F:\\llm_hub\\copilot_api），先执行 npm install copilot-api"}
    node_exe = shutil.which("node")
    if not node_exe:
        cand = r"C:\Program Files\nodejs\node.exe"
        if os.path.exists(cand):
            node_exe = cand
    if not node_exe:
        return {"ok": False, "message": "未找到 node.exe"}
    try:
        COPILOT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(COPILOT_LOG_FILE, "ab")
        try:
            subprocess.Popen(
                [node_exe, str(COPILOT_MAIN), "start"],
                cwd=str(COPILOT_DIR),
                stdout=log_fh, stderr=subprocess.STDOUT,
                creationflags=0x00000008 | 0x00000200)
        finally:
            log_fh.close()
    except Exception as e:
        return {"ok": False, "message": "启动失败：%r" % e}
    for _ in range(60):
        if _copilot_http_up():
            return {"ok": True, "started": True, "message": "copilot-api 已启动（端口 %d）" % COPILOT_PORT}
        time.sleep(0.5)
    return {"ok": False, "message": "copilot-api 已拉起但端口 %d 在 30 秒内未就绪，见 logs/copilot.log" % COPILOT_PORT}


@app.get("/api/copilot/status")
def copilot_status():
    """Copilot 反代状态：是否安装 / 是否在跑 / 最近一次配额快照"""
    d = load_data()
    return {"installed": COPILOT_MAIN.exists(),
            "up": _copilot_http_up(),
            "quota": d.get("copilot_quota") or {}}


@app.post("/api/copilot/start")
def copilot_start():
    return _copilot_start()


@app.post("/api/copilot/quota/refresh")
def copilot_quota_refresh():
    """同步 Copilot 配额：跑 copilot-api check-usage（只读接口，不耗 premium 配额）。"""
    d = load_data()
    out = {"ok": False}
    if not COPILOT_MAIN.exists():
        out["error"] = "未安装 copilot-api（F:\\llm_hub\\copilot_api）"
        return out
    node_exe = shutil.which("node")
    if not node_exe:
        cand = r"C:\Program Files\nodejs\node.exe"
        if os.path.exists(cand):
            node_exe = cand
    if not node_exe:
        out["error"] = "未找到 node.exe"
        return out
    try:
        r = subprocess.run(
            [node_exe, str(COPILOT_MAIN), "check-usage"],
            capture_output=True, timeout=90, cwd=str(COPILOT_DIR))
        clean = re.sub(r"\x1b\[[0-9;]*m", "", r.stdout.decode("utf-8", "replace"))
        info = {}
        m = re.search(r"Logged in as\s+(\S+)", clean)
        info["login"] = m.group(1) if m else ""
        m = re.search(r"plan:\s*([A-Za-z_-]+)\)", clean)
        info["plan"] = m.group(1) if m else ""
        m = re.search(r"Quota resets:\s*([0-9-]+)", clean)
        info["resets"] = m.group(1) if m else ""
        for key in ("Premium", "Chat", "Completions"):
            m = re.search(key + r":\s*(\d+)/(\d+)\s*used", clean)
            if m:
                info[key.lower()] = {"used": int(m.group(1)), "total": int(m.group(2))}
        if not info["login"]:
            lines = [l.strip() for l in clean.strip().splitlines() if l.strip()]
            out["error"] = "未解析到登录信息" + (("：" + lines[-1][:150]) if lines else "（check-usage 无输出）")
            return out
        out["ok"] = True
        out["quota"] = info
        cq = d.setdefault("copilot_quota", {})
        cq.update(info)
        cq["synced_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_data(d)
        out["synced_at"] = cq["synced_at"]
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


_lan_ip_cache = {"ip": None, "t": 0.0}


def _lan_ip():
    """探测本机局域网 IP：优先 ipconfig 枚举真实私网段（排除 TUN fake-IP / Tailscale CGNAT / APIPA），30s 缓存"""
    import re as _re
    import subprocess as _sp
    import time as _time

    def _priv(ip):
        m = _re.match(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$", ip)
        if not m:
            return False
        a, b = int(m.group(1)), int(m.group(2))
        if a in (0, 127, 169, 198, 224, 255):
            return False
        if a == 100 and 64 <= b <= 127:  # CGNAT（Tailscale 等）
            return False
        if a == 192 and b == 168:
            return True
        if a == 10:
            return True
        if a == 172 and 16 <= b <= 31:
            return True
        return False

    now = _time.time()
    if _lan_ip_cache["ip"] and now - _lan_ip_cache["t"] < 30:
        return _lan_ip_cache["ip"]
    ip = "127.0.0.1"
    try:
        out = _sp.run(["ipconfig"], capture_output=True, text=True, timeout=5).stdout or ""
        for cand in _re.findall(r"IPv4[^\r\n]*?(\d{1,3}(?:\.\d{1,3}){3})", out):
            if _priv(cand):
                ip = cand
                break
    except Exception:
        pass
    if ip == "127.0.0.1":
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            cand = s.getsockname()[0]
            if _priv(cand):
                ip = cand
        except Exception:
            pass
        finally:
            try:
                if s is not None:
                    s.close()
            except Exception:
                pass
    if ip == "127.0.0.1":
        try:
            for cand in {i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}:
                if _priv(cand):
                    ip = cand
                    break
        except Exception:
            pass
    _lan_ip_cache["ip"] = ip
    _lan_ip_cache["t"] = now
    return ip


@app.post("/api/call-config")
async def api_call_config(req: Request):
    """调用地址总开关：mode=lan（本机/局域网）/ public（公网），public_host 为公网地址"""
    body = await req.json()
    d = load_data()
    cc = d.get("call_config") or {}
    cc.setdefault("mode", "lan")
    cc.setdefault("public_host", "")
    if "mode" in body:
        m = str(body.get("mode") or "")
        if m not in ("lan", "public"):
            raise HTTPException(400, "mode 只能是 lan / public")
        cc["mode"] = m
    if "public_host" in body:
        v = str(body.get("public_host") or "").strip().rstrip("/")
        if not v and cc.get("public_host") and not body.get("clear_public_host"):
            # 防误触（2026-09-21 用户拍板）：空值不清除已保存的公网地址，
            # 需前端二次确认后带 clear_public_host=true 才会清空。
            pass
        else:
            cc["public_host"] = v
    d["call_config"] = cc
    save_data(d)
    return {"ok": True, "call_config": cc, "lan_ip": _lan_ip()}


def _autostart_copilot_worker():
    """Hub 启动后按配置自动拉起 copilot-api（GitHub Copilot 反代，端口 4141）。"""
    for _ in range(60):
        try:
            r = httpx.get("http://127.0.0.1:8787/api/state", timeout=1.5)
            if r.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        return
    try:
        d = load_data()
        if not d.get("autostart_copilot", True):
            return
        if not COPILOT_MAIN.exists():
            return
        time.sleep(0.5)
        r = _copilot_start()
        print("[autostart] copilot-api: %s" % (r.get("message") or r))
    except Exception as e:
        print("[autostart] copilot-api failed: %r" % e)


def _autostart_harness_worker():
    """Hub 启动后按配置自动拉起 dsh（DeepSeek Harness），与渠道管理联动。"""
    for _ in range(60):
        try:
            r = httpx.get("http://127.0.0.1:8787/api/state", timeout=1.5)
            if r.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        return
    try:
        d = load_data()
        if not d.get("autostart_harness", True):
            return
        time.sleep(0.5)
        r = _dsh_start()
        print("[autostart] dsh: %s" % (r.get("message") or r))
    except Exception as e:
        print("[autostart] dsh failed: %r" % e)


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
    print("LLM Key Hub 已启动: http://127.0.0.1:8787  (已监听 0.0.0.0，局域网设备可用 http://%s:8787 访问)" % _lan_ip())
    if not HUB_CLOUD:
        threading.Thread(target=_autostart_harness_worker, daemon=True).start()
        threading.Thread(target=_autostart_copilot_worker, daemon=True).start()
    try:
        uvicorn.run(app, host="0.0.0.0", port=8787, log_level="warning")
    except OSError as e:
        print("启动失败：%r" % e)
        sys.exit(1)
