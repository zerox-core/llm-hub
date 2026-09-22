# -*- coding: utf-8 -*-
"""_dsh_restarter.py — dsh 自我重启器（由 @dsh-local/dsh-updater 插件 detached 拉起）。

流程：等旧 dsh 退出 -> 需要时 npm install 目标版本 -> 重跑 patch_dsh.py ->
import server 调 _dsh_start() 拉起新 dsh -> 等新 token 写回 _hb_tok.txt -> 落结果 JSON。
日志：F:\\llm_hub\\logs\\dsh_update.log；结果：F:\\llm_hub\\_push\\dsh_update_result.json。
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import traceback

HUB_DIR = r"F:\llm_hub"
APP_DIR = r"F:\deepseek-harness\app"
PATCH = os.path.join(HUB_DIR, "dsh_patch", "patch_dsh.py")
LOG = os.path.join(HUB_DIR, "logs", "dsh_update.log")
DSH_LOG = os.path.join(HUB_DIR, "logs", "dsh.log")
TOK = os.path.join(HUB_DIR, "logs", "_hb_tok.txt")
RESULT = os.path.join(HUB_DIR, "_push", "dsh_update_result.json")
PORT = 3080


def log(*a):
    msg = "[%s] %s" % (time.strftime("%H:%M:%S"), " ".join(str(x) for x in a))
    print(msg, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def port_open():
    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect(("127.0.0.1", PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def wait_port_free(timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not port_open():
            return True
        time.sleep(1)
    return False


def installed_version():
    try:
        p = os.path.join(APP_DIR, "node_modules", "@deepseek-ai", "dsh", "package.json")
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("version")
    except Exception:
        return None


def npm_install(target):
    cmd = "npm install @deepseek-ai/dsh@%s" % target
    log("RUN", cmd)
    p = subprocess.run(cmd, cwd=APP_DIR, shell=True, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=600)
    log("npm exit", p.returncode)
    if p.stdout:
        log("npm out:", p.stdout[-1500:])
    if p.stderr:
        log("npm err:", p.stderr[-1500:])
    return p.returncode == 0


def run_patch():
    p = subprocess.run([sys.executable, PATCH], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    log("patch exit", p.returncode, (p.stdout or "")[-800:])
    return p.returncode == 0


def start_dsh():
    sys.path.insert(0, HUB_DIR)
    import server
    r = server._dsh_start()
    log("_dsh_start ->", r)
    return r


def wait_up(timeout=90):
    import urllib.request
    import urllib.error
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/" % PORT, timeout=3)
            return True
        except urllib.error.HTTPError as e:
            if e.code in (200, 401, 403, 404):
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def rewrite_token(old_tok, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with open(DSH_LOG, encoding="utf-8", errors="replace") as f:
                toks = re.findall(r"127\.0\.0\.1:%d/\?token=([A-Za-z0-9_\-]+)" % PORT, f.read())
            if toks and toks[-1] != old_tok:
                with open(TOK, "w", encoding="utf-8") as f:
                    f.write(toks[-1])
                log("token rewritten")
                return toks[-1]
        except Exception:
            pass
        time.sleep(2)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=None)
    args = ap.parse_args()
    steps = {"target": args.target, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    old_tok = ""
    try:
        if os.path.exists(TOK):
            old_tok = open(TOK, encoding="utf-8").read().strip()
    except Exception:
        pass
    try:
        log("=== restarter start target=%s ===" % args.target)
        steps["port_free"] = wait_port_free()
        log("port free:", steps["port_free"])
        if not steps["port_free"]:
            raise RuntimeError("old dsh did not exit within 90s")
        cur = installed_version()
        steps["before"] = cur
        if args.target and args.target != cur:
            steps["npm"] = npm_install(args.target)
            if not steps["npm"]:
                raise RuntimeError("npm install failed")
        else:
            steps["npm"] = "skipped"
        steps["after"] = installed_version()
        steps["patch"] = run_patch()
        steps["start"] = start_dsh()
        steps["http_up"] = wait_up()
        steps["token"] = bool(rewrite_token(old_tok))
        steps["ok"] = bool(steps["http_up"])
        log("=== restarter done ok=%s ===" % steps["ok"])
    except Exception as e:
        steps["ok"] = False
        steps["error"] = repr(e)
        log("ERROR", traceback.format_exc())
    try:
        os.makedirs(os.path.dirname(RESULT), exist_ok=True)
        with open(RESULT, "w", encoding="utf-8") as f:
            json.dump(steps, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


if __name__ == "__main__":
    main()
