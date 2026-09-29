# -*- coding: utf-8 -*-
"""hub_channel_agent.py - 本地通道 agent（R75, 2026-09-29）

云端 hub（hub.zeroxcore.tech）与用户本机之间的常驻命令桥：
- 注册：读本地 data.json 的 hub_key → POST /api/channel/register 换通道 token（通道须已在
  云端面板打开）；通道关闭/未开时 poll 返回 401，agent 自动退回重试注册，无需人工干预。
- 轮询：每 5s POST /api/channel/poll 领命令并执行、POST /api/channel/report 回报。
- 命令：
    scan         跑 dsh_hub_scan.py 重扫 dsh 模型列表（渲染 settings.yaml）
    pull_data    GET /api/channel/data → 云端→本地 base_url 映射 → 备份后覆盖本地
                 data.json → 立即 scan（让 dsh 面板出现云端全部号池）
    push_data    读本地 data.json → 本地→云端 base_url 映射 → POST /api/channel/data
                 （云端自动备份为 data.json.bak_chan_* 后覆盖）
    restart_dsh  杀占用 3080 的 dsh 进程 → 跑 dsh_launch.ps1 注入最新号池 key 重启
    start_panel  启动本地面板（127.0.0.1:8787，HUB_NO_DSH=1），供云端页面内嵌打开
- 单实例：lock 文件防重入（主循环每轮 touch；>60s 无心跳视为僵死自动接管）。
- 由 start_dsh.bat 拉起；网络/通道异常自动退避重试，不弹窗不退出。
"""
import io, json, os, subprocess, sys, time, urllib.request, urllib.error
from pathlib import Path

BASE = Path(r"F:\llm_hub")
DATA = BASE / "data.json"
LOGS = BASE / "logs"
LOG = LOGS / "channel_agent.log"
LOCK = LOGS / ".channel_agent.lock"
SCAN = BASE / "dsh_hub_scan.py"
LAUNCH = BASE / "dsh_launch.ps1"
CLOUD = "https://hub.zeroxcore.tech"
POLL_SEC = 5.0
HTTP_TIMEOUT = 25
AGENT_NAME = "channel-agent-r75"
DSH_PORT = 3080
PANEL_PORT = 8787
CLIPROXY_PORT = 8317
CLIPROXY_EXE = BASE / "cliproxy" / "cli-proxy-api.exe"
SERVER_PY = BASE / "server.py"

# base_url 双向映射：本机反代端口 ↔ 云端容器网络名（路径保留）
LOCAL2CLOUD = {
    "http://127.0.0.1:4141": "http://copilot-api:4141",
    "http://localhost:4141": "http://copilot-api:4141",
    "http://127.0.0.1:8317": "http://cliproxy:8317",
    "http://localhost:8317": "http://cliproxy:8317",
}
CLOUD2LOCAL = {v: k for k, v in LOCAL2CLOUD.items()}


def log(msg):
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > 512 * 1024:
            lines = io.open(str(LOG), encoding="utf-8", errors="replace").read().splitlines()[-2000:]
            LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with io.open(str(LOG), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg))
    except Exception:
        pass


def http(method, path, token=None, key=None, body=None, timeout=HTTP_TIMEOUT, headers=None):
    """极简 JSON HTTP（urllib + 禁系统代理，与扫描器同款，避开 curl/schannel 怪癖）。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(CLOUD + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for hk, hv in (headers or {}).items():
        req.add_header(hk, hv)
    cred = token or key
    if cred:
        req.add_header("Authorization", "Bearer " + cred)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def remap_urls(obj, table):
    """递归替换 dict/list/str 中的已知 base_url 前缀（精确等于或前缀匹配，路径保留）。"""
    if isinstance(obj, dict):
        return {k: remap_urls(v, table) for k, v in obj.items()}
    if isinstance(obj, list):
        return [remap_urls(v, table) for v in obj]
    if isinstance(obj, str):
        for a, b in table.items():
            if obj == a:
                return b
            if obj.startswith(a + "/"):
                return b + obj[len(a):]
        return obj
    return obj


# ---------------- 命令实现 ----------------

def cmd_scan(token=None):
    p = subprocess.run([sys.executable, str(SCAN)], capture_output=True, text=True, timeout=300)
    out = ((p.stdout or "") + (p.stderr or ""))[-1200:]
    return p.returncode == 0, "exit=%d %s" % (p.returncode, out.strip())


def cmd_pull_data(token):
    r = http("GET", "/api/channel/data", token=token, timeout=60)
    nd = remap_urls(r.get("data") or {}, CLOUD2LOCAL)
    provs = nd.get("providers") or []
    if not provs:
        return False, "云端数据 providers 为空，拒绝覆盖本地"
    bak = DATA.with_name("data.json.bak_pull_" + time.strftime("%Y%m%d_%H%M%S"))
    try:
        bak.write_text(DATA.read_text(encoding="utf-8"), encoding="utf-8")
        DATA.write_text(json.dumps(nd, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        return False, "写入本地 data.json 失败：%r" % e
    ok, msg = cmd_scan()
    return True, "已拉取云端数据（%d 渠道 / %d 号池），本地备份 %s；扫描 %s：%s" % (
        len(provs), len(nd.get("pools") or []), bak.name, "成功" if ok else "失败", (msg or "")[-300:])


def cmd_push_data(token):
    d = json.loads(io.open(str(DATA), encoding="utf-8").read())
    if not (d.get("providers") or []):
        return False, "本地 data.json providers 为空，拒绝上传"
    nd = remap_urls(d, LOCAL2CLOUD)
    r = http("POST", "/api/channel/data", token=token, body={"data": nd}, timeout=60)
    return True, json.dumps(r, ensure_ascii=False)[:500]


def _port_pid(port):
    try:
        out = subprocess.run("netstat -ano | findstr :%d" % port, shell=True,
                             capture_output=True, text=True, timeout=8).stdout or ""
    except Exception:
        return None
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) >= 5 and ":%d" % port in parts[1] and parts[3] == "LISTENING":
            try:
                return int(parts[-1])
            except ValueError:
                pass
    return None


def cmd_restart_dsh(token=None):
    pid = _port_pid(DSH_PORT)
    if pid:
        subprocess.run("taskkill /PID %d /T /F" % pid, shell=True, capture_output=True)
        time.sleep(1.5)
    p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", str(LAUNCH)], capture_output=True, text=True, timeout=90)
    up = False
    for _ in range(30):
        if _port_pid(DSH_PORT):
            up = True
            break
        time.sleep(1)
    return up, ("dsh 已重启（旧 pid %s，注入最新号池 key）" % pid if up
                else "dsh 已拉起但端口 %d 30 秒内未就绪：%s" % (DSH_PORT, (p.stderr or "")[-300:]))



def _start_detached(argv, cwd=None, env_extra=None, out_name="panel_out.log", err_name="panel_err.log"):
    """后台脱离启动（关窗口不死），输出进 logs。"""
    env = dict(os.environ)
    env.update(env_extra or {})
    out_f = open(str(LOGS / out_name), "ab")
    err_f = open(str(LOGS / err_name), "ab")
    subprocess.Popen(argv, cwd=cwd, env=env, stdout=out_f, stderr=err_f,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                                  | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def cmd_start_panel(token=None):
    """启动本地面板（仅本机回环 8787，HUB_NO_DSH=1 不耦合 dsh）；已在跑则直接回报。"""
    if _port_pid(PANEL_PORT):
        return True, "本地面板已在运行：http://127.0.0.1:%d" % PANEL_PORT
    if not _port_pid(CLIPROXY_PORT) and CLIPROXY_EXE.exists():
        _start_detached([str(CLIPROXY_EXE), "-config", "config.yaml"], cwd=str(CLIPROXY_EXE.parent),
                        out_name="cliproxy_out.log", err_name="cliproxy_err.log")
        time.sleep(1.0)
    _start_detached([sys.executable, str(SERVER_PY)], cwd=str(BASE),
                    env_extra={"HUB_NO_DSH": "1", "HUB_BIND_HOST": "127.0.0.1"})
    for _ in range(25):
        if _port_pid(PANEL_PORT):
            return True, "本地面板已启动：http://127.0.0.1:%d（仅本机回环可访问）" % PANEL_PORT
        time.sleep(1)
    return False, "面板进程已拉起但 25 秒内端口 %d 未就绪，详见 logs\\panel_err.log" % PANEL_PORT


DISPATCH = {"scan": cmd_scan, "pull_data": cmd_pull_data,
            "push_data": cmd_push_data, "restart_dsh": cmd_restart_dsh,
            "start_panel": cmd_start_panel}


# ---------------- 主循环 ----------------

def acquire_lock():
    try:
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            age = time.time() - LOCK.stat().st_mtime
        except OSError:
            return False
        if age > 60:
            try:
                LOCK.unlink()
            except OSError:
                return False
            try:
                fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                log("stale lock taken over (age %ds)" % age)
                return True
            except FileExistsError:
                return False
        return False


def register():
    d = json.loads(io.open(str(DATA), encoding="utf-8").read())
    key = d.get("hub_key") or ""
    if not key:
        raise RuntimeError("本地 data.json 无 hub_key")
    r = http("POST", "/api/channel/register", key=key, body={})
    tok = r.get("token") or ""
    if not tok:
        raise RuntimeError("register 未返回 token")
    return tok


def main():
    if not acquire_lock():
        print("another agent instance is running - exit")
        return 0
    log("agent start pid=%d cloud=%s" % (os.getpid(), CLOUD))
    token = None
    while True:
        try:
            if token is None:
                token = register()
                log("registered, token acquired")
            r = http("POST", "/api/channel/poll", token=token, body={},
                     headers={"X-Channel-Agent": AGENT_NAME})
            for c in r.get("commands") or []:
                cid, cmd = c.get("id"), c.get("cmd")
                fn = DISPATCH.get(cmd)
                if fn is None:
                    ok, outp = False, "unknown command: %s" % cmd
                else:
                    try:
                        ok, outp = fn(token)
                    except Exception as e:
                        ok, outp = False, "EXC %r" % e
                try:
                    http("POST", "/api/channel/report", token=token,
                         body={"id": cid, "ok": bool(ok), "output": str(outp)[:1500]})
                except Exception as e:
                    log("report %s failed: %r" % (cid, e))
                log("cmd %s %s -> %s %s" % (cid, cmd, ok, str(outp)[:250]))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                token = None
                log("poll 401 - channel closed or token invalid, will re-register")
                time.sleep(15)
                continue
            log("poll http %s" % e.code)
        except Exception as e:
            log("poll error %r" % e)
        try:
            LOCK.touch()
        except OSError:
            pass
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    sys.exit(main())
