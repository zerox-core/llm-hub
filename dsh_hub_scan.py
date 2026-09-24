# -*- coding: utf-8 -*-
"""dsh_hub_scan.py - hub 号池模型扫描器（R42, 2026-09-24）

每次运行：
1. 读 F:\\llm_hub\\data.json 里所有 channel_pid 号池（id/name/key）——与 dsh_launch.ps1
   注入 HUB_POOL_KEY_* 环境变量用的是同一来源，保证 settings.yaml 与 dsh 进程环境天然一致；
2. 用各号池 key 调云端 GET https://hub.zeroxcore.tech/v1/models，拿「活」模型列表
   （上游号池模型随额度/筛选实时变化，这是唯一实时真相）；
3. 按 dsh settings.yaml 结构重渲染：顶部 llm-hub = Auto 全池；每个号池一个区块
   （auto + 明细模型）；保留非 llm-hub* 的 provider 与所有其它顶层键；默认模型尽量保留；
4. 内容有变化才原子写入。号池区块（provider 键）集合变化时写 logs/.dsh_restart_needed
   旗标，由 hub_watchdog 重启 dsh 注入新环境变量；仅模型列表变化无需重启
   （dsh 按请求重读 settings.yaml）。
失败保底：单个号池扫描失败沿用该区块旧内容；全部失败则不写文件、退出码 2。
由 hub_watchdog.ps1 每 3 分钟拉起；自身用 lock 文件防重入（10 分钟过期）。
"""
import io, json, os, re, subprocess, sys, time
from pathlib import Path

import yaml

BASE = Path(r"F:\llm_hub")
DATA = BASE / "data.json"
LOGS = BASE / "logs"
LOG = LOGS / "dsh_hub_scan.log"
LOCK = LOGS / ".dsh_hub_scan.lock"
RESTART_FLAG = LOGS / ".dsh_restart_needed"
DSH_SETTINGS = Path(os.environ.get("DSH_HOME") or r"F:\deepseek-harness\home") / "settings.yaml"
HUB_BASE = "https://hub.zeroxcore.tech/v1"
CURL_TIMEOUT = 20
LOCK_STALE_SECONDS = 600

# 与 server.py CHANNEL_POOLS 同序；未知名池排其后按名称排序
CHANNEL_ORDER = ["pool_ch_deepseek", "pool_ch_ag", "pool_ch_copilot", "pool_ch_wb", "pool_ch_bailian"]

HEADER = ("# 由 dsh_hub_scan.py 自动生成（R42 扫描版，2026-09-24）。\n"
          "# 模型列表 = 云端 hub /v1/models 实时扫描；号池目录 = 本地 data.json（channel_pid 号池）。\n"
          "# llm-hub* 前缀的 provider 由扫描器管理并在每次扫描时重写；其余键原样保留。\n")


def log(msg):
    LOGS.mkdir(parents=True, exist_ok=True)
    try:
        if LOG.exists() and LOG.stat().st_size > 1024 * 1024:
            lines = io.open(str(LOG), encoding="utf-8", errors="replace").read().splitlines()[-2000:]
            LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass
    with io.open(str(LOG), "a", encoding="utf-8") as f:
        f.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg))


def route_key(pool_id):
    base = re.sub(r"[^a-z0-9]+", "-", str(pool_id or "pool").lower()).strip("-")
    return "llm-hub-" + (base or "pool")


def env_name(pool_id):
    return "HUB_POOL_KEY_" + re.sub(r"[^A-Z0-9]+", "_", str(pool_id or "POOL").upper()).strip("_")


def fetch_models(key):
    """GET /v1/models（池作用域）。返回 (ids, err)；ids 为 None 表示失败。"""
    try:
        out = subprocess.run(
            ["curl.exe", "-sS", "-m", str(CURL_TIMEOUT), "-w", "\n%{http_code}",
             "-H", "Authorization: Bearer " + key, HUB_BASE + "/models"],
            capture_output=True, text=True, timeout=CURL_TIMEOUT + 15)
    except Exception as e:
        return None, "curl spawn: %r" % e
    body = (out.stdout or "").rstrip("\n")
    if "\n" in body:
        payload, code = body.rsplit("\n", 1)
    else:
        payload, code = body, ""
    if code != "200":
        return None, "http %s: %s%s" % (code or "???", (out.stderr or "")[:120], payload[:120])
    try:
        j = json.loads(payload)
        return [m.get("id") for m in (j.get("data") or []) if m.get("id")], None
    except Exception as e:
        return None, "parse: %r body=%s" % (e, payload[:120])


def main():
    # --- lock 防重入 ---
    try:
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        age = time.time() - LOCK.stat().st_mtime
        if age > LOCK_STALE_SECONDS:
            try:
                LOCK.unlink()
            except OSError:
                pass
            log("stale lock removed (age %ds)" % age)
        else:
            return 0
    try:
        return run()
    finally:
        try:
            LOCK.unlink()
        except OSError:
            pass


def run():
    d = json.loads(io.open(str(DATA), encoding="utf-8").read())
    pools = [pl for pl in (d.get("pools") or []) if pl.get("channel_pid") and pl.get("key")]
    pools.sort(key=lambda pl: (CHANNEL_ORDER.index(pl["id"]) if pl.get("id") in CHANNEL_ORDER else 99,
                               str(pl.get("name") or pl.get("id"))))
    if not pools:
        log("no channel pools in data.json - nothing to do")
        return 0

    try:
        old_text = DSH_SETTINGS.read_text(encoding="utf-8")
        old = yaml.safe_load(old_text)
        old = old if isinstance(old, dict) else {}
    except Exception:
        old_text, old = "", {}
    lpa_old = old.get("llm-pi-ai")
    provs_old = (lpa_old.get("providers") or {}) if isinstance(lpa_old, dict) else {}

    compat = {"supportsDeveloperRole": False, "maxTokensField": "max_tokens"}
    providers = {"llm-hub": {
        "api": "openai-completions", "baseURL": HUB_BASE, "apiKeyEnv": "HUB_API_KEY",
        "displayName": "Auto · 全部渠道（轮询）", "compat": compat,
        "models": [{"id": "auto"}]}}
    used = {"llm-hub"}
    failed, scanned, reused = [], 0, 0
    for i, pl in enumerate(pools):
        pid = pl.get("id")
        rk = route_key(pid)
        while rk in used:
            rk += "-%d" % i
        used.add(rk)
        ids, err = fetch_models(str(pl.get("key")))
        if ids is None:
            failed.append((pid, err))
            old_block = provs_old.get(rk)
            if old_block:
                providers[rk] = old_block
                reused += 1
                log("scan FAIL %s (%s) - kept old block" % (pid, err))
            else:
                log("scan FAIL %s (%s) - no old block, pool skipped" % (pid, err))
            continue
        scanned += 1
        seen, mlist = {"auto"}, []
        for m in ids:
            if m not in seen:
                seen.add(m)
                mlist.append({"id": m})
        providers[rk] = {
            "api": "openai-completions", "baseURL": HUB_BASE, "apiKeyEnv": env_name(pid),
            "displayName": str(pl.get("name") or pid), "compat": compat,
            "models": [{"id": "auto"}] + mlist}
    if pools and scanned == 0 and reused == 0:
        log("ALL pools failed to scan and no old blocks - aborting without write")
        return 2

    # 保留非 llm-hub* 的旧 provider
    for k, v in provs_old.items():
        if not str(k).startswith("llm-hub"):
            providers[k] = v
    out = dict(old)
    out["llm-pi-ai"] = {"providers": providers}

    # 默认模型：尽量保留用户选择；不在原分组则改指到包含它的分组，都没有回退全池 auto
    adm = old.get("agent-default-model") or {}
    am, ap = adm.get("model"), adm.get("provider")
    def _ids(pv):
        return [m.get("id") if isinstance(m, dict) else m for m in (pv.get("models") or [])]
    valid = ap in providers and am in _ids(providers.get(ap) or {})
    if not valid and am:
        for rk, pv in providers.items():
            if am in _ids(pv):
                ap, valid = rk, True
                break
    if not valid:
        ap, am = "llm-hub", "auto"
    out["agent-default-model"] = {"provider": ap, "model": am}

    new_text = HEADER + yaml.safe_dump(out, allow_unicode=True, sort_keys=False)
    if new_text == old_text:
        log("no change (pools=%d scanned=%d reused=%d failed=%d)" % (len(pools), scanned, reused, len(failed)))
        return 0

    DSH_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = DSH_SETTINGS.with_suffix(".yaml.tmp_scan")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(str(tmp), str(DSH_SETTINGS))

    old_keys = {k for k in provs_old if str(k).startswith("llm-hub")}
    new_keys = set(providers) - {k for k in provs_old if not str(k).startswith("llm-hub")}
    if old_keys != new_keys:
        RESTART_FLAG.write_text("pool set changed %s -> %s at %s" % (
            sorted(old_keys), sorted(new_keys), time.strftime("%Y-%m-%d %H:%M:%S")), encoding="utf-8")
        log("settings.yaml rewritten; POOL SET changed %s -> %s; restart flag set" % (sorted(old_keys), sorted(new_keys)))
    else:
        log("settings.yaml rewritten (models updated; pools=%d scanned=%d reused=%d failed=%d)"
            % (len(pools), scanned, reused, len(failed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
