# -*- coding: utf-8 -*-
"""patch_dsh.py — 把 Hub 注入块打进 dsh 前端 index.html（幂等，可重复运行）。

用法：
    py patch_dsh.py            # 注入 / 更新注入 + 修补 cordis.patch.yml
    py patch_dsh.py --restore  # 移除全部前端注入，恢复原样（不动 cordis）

注入块（BLOCKS 顺序即注入顺序）：
    hub_ui_kit.js       手动更新按钮 + 侧栏箭头收起/边缘弹出 + 页面全铺开
    hub_session_zone.js 会话区（时间分区创建 + 删除双选项弹窗 + Codex 风格
                        扁平会话列表 + 隐藏工作区文件夹体系）

2026-09-23 R24：移除 hub_model_card.js（页内旧版模型切换卡片）——dsh 输入栏
已有原生模型选择器；旧标记保留在 STRIP_BLOCKS 里保证剥离干净。

同时修补 dsh-base/cordis.patch.yml（幂等）：
  1) 摘掉 session-title-llm（LLM 生成标题提供方）——会话标题直接用首条消息
     文本（确定性回退），也省掉每次新会话的一次 LLM 调用；
  2) 放宽回退长度：fallbackMaxWords 12 / fallbackMaxBytes 117 /
     maxTitleBytes 120（中文首句约可放 39 字）。

原理：dsh 的 SPA 静态服务每个请求都会读一次 dist/index.html，
前端补丁无需重启 dsh，刷新页面即生效；cordis.patch.yml 是启动期组合，
改完需要重启 dsh 才生效。dsh 包升级会覆盖 dist 与 node_modules，
升级后重新跑一遍本脚本即可（_dsh_restarter.py 会自动重跑）。
"""
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# 当前实际注入的块（2026-09-23 起不含模型卡片）
BLOCKS = [
    ("hub_ui_kit.js", "<!-- HUB-UI-KIT BEGIN -->", "<!-- HUB-UI-KIT END -->"),
    ("hub_session_zone.js", "<!-- HUB-SESSION-ZONE BEGIN -->", "<!-- HUB-SESSION-ZONE END -->"),
]

# 所有历史注入标记：剥离时一律清掉（含已下线的模型卡片）
STRIP_MARKERS = [
    ("<!-- HUB-MODEL-CARD BEGIN -->", "<!-- HUB-MODEL-CARD END -->"),
    ("<!-- HUB-UI-KIT BEGIN -->", "<!-- HUB-UI-KIT END -->"),
    ("<!-- HUB-SESSION-ZONE BEGIN -->", "<!-- HUB-SESSION-ZONE END -->"),
]

CANDIDATES = [
    r"F:\deepseek-harness\app\node_modules\@deepseek-ai\dsh-web-frontend\dist\index.html",
    r"F:\deepseek-harness\home\profiles\node_modules\@deepseek-ai\dsh-web-frontend\dist\index.html",
]
# 兜底：版本目录结构变化时全盘补搜
CANDIDATES += [p for p in glob.glob(
    r"F:\deepseek-harness\**\node_modules\@deepseek-ai\dsh-web-frontend\dist\index.html",
    recursive=True) if p not in CANDIDATES]

CORDIS_CANDIDATES = [
    r"F:\deepseek-harness\app\node_modules\@deepseek-ai\dsh-base\cordis.patch.yml",
    r"F:\deepseek-harness\home\profiles\node_modules\@deepseek-ai\dsh-base\cordis.patch.yml",
]
CORDIS_CANDIDATES += [p for p in glob.glob(
    r"F:\deepseek-harness\**\node_modules\@deepseek-ai\dsh-base\cordis.patch.yml",
    recursive=True) if p not in CORDIS_CANDIDATES]


def strip_injected(html: str) -> str:
    for begin, end in STRIP_MARKERS:
        while begin in html and end in html:
            a = html.index(begin)
            b = html.index(end) + len(end)
            html = html[:a] + html[b:]
    return html


def patch(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        html = f.read()
    html = strip_injected(html)
    bak = path + ".bak_hub"
    if not os.path.exists(bak):
        with open(bak, "w", encoding="utf-8") as f:
            f.write(html)
    block = ""
    for js_file, begin, end in BLOCKS:
        with open(os.path.join(HERE, js_file), encoding="utf-8") as f:
            js = f.read()
        block += "\n    %s\n    <script>\n%s\n    </script>\n    %s\n" % (begin, js, end)
    if "</body>" in html:
        html = html.replace("</body>", block + "  </body>", 1)
    else:
        html += block
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return "patched %d blocks (backup: %s)" % (len(BLOCKS), bak)


def restore(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        html = f.read()
    if not any(begin in html for begin, _end in STRIP_MARKERS):
        return "no injection found, skipped"
    html = strip_injected(html)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return "restored (injection removed)"


def patch_cordis(path: str) -> str:
    """摘掉 LLM 标题提供方 + 放宽首句标题回退长度（幂等）。"""
    with open(path, encoding="utf-8") as f:
        orig = f.read()
    lines = orig.splitlines(keepends=True)
    out = []
    skip = False
    for ln in lines:
        if re.match(r"^\s*- id: session-title-llm\s*$", ln):
            skip = True
            continue
        if skip and re.match(r"^\s*- id: ", ln):
            skip = False
        if not skip:
            out.append(ln)
    txt = "".join(out)
    txt = txt.replace("fallbackMaxWords: 5", "fallbackMaxWords: 12")
    txt = txt.replace("fallbackMaxBytes: 40", "fallbackMaxBytes: 117")
    txt = txt.replace("maxTitleBytes: 80", "maxTitleBytes: 120")
    if txt == orig:
        return "cordis already patched, skipped"
    bak = path + ".bak_hub"
    if not os.path.exists(bak):
        with open(bak, "w", encoding="utf-8") as f:
            f.write(orig)
    with open(path, "w", encoding="utf-8") as f:
        f.write(txt)
    return "cordis patched (title-llm removed, fallback widened; backup: %s)" % bak


def main() -> int:
    mode_restore = "--restore" in sys.argv
    found = 0
    for path in CANDIDATES:
        if not os.path.exists(path):
            print("MISS  %s" % path)
            continue
        found += 1
        try:
            msg = restore(path) if mode_restore else patch(path)
            print("OK    %s -> %s" % (path, msg))
        except Exception as e:
            print("FAIL  %s -> %r" % (path, e))
    if found == 0:
        print("ERROR: no dsh dist index.html found")
        return 1
    if not mode_restore:
        for path in CORDIS_CANDIDATES:
            if not os.path.exists(path):
                print("MISS  %s" % path)
                continue
            try:
                print("OK    %s -> %s" % (path, patch_cordis(path)))
            except Exception as e:
                print("FAIL  %s -> %r" % (path, e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
