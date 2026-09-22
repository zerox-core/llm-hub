# -*- coding: utf-8 -*-
"""patch_dsh.py — 把 Hub 注入块打进 dsh 前端 index.html（幂等，可重复运行）。

用法：
    py patch_dsh.py            # 注入 / 更新注入
    py patch_dsh.py --restore  # 移除全部注入，恢复原样

注入块（BLOCKS 顺序即注入顺序）：
    hub_model_card.js  模型切换卡片
    hub_ui_kit.js      手动更新按钮 + 侧栏箭头收起/边缘弹出 + 页面全铺开

原理：dsh 的 SPA 静态服务每个请求都会读一次 dist/index.html，
所以补丁无需重启 dsh，刷新页面即生效。dsh 包升级会覆盖 dist，
升级后重新跑一遍本脚本即可（_dsh_restarter.py 会自动重跑）。
"""
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

BLOCKS = [
    ("hub_model_card.js", "<!-- HUB-MODEL-CARD BEGIN -->", "<!-- HUB-MODEL-CARD END -->"),
    ("hub_ui_kit.js", "<!-- HUB-UI-KIT BEGIN -->", "<!-- HUB-UI-KIT END -->"),
]

CANDIDATES = [
    r"F:\deepseek-harness\app\node_modules\@deepseek-ai\dsh-web-frontend\dist\index.html",
    r"F:\deepseek-harness\home\profiles\node_modules\@deepseek-ai\dsh-web-frontend\dist\index.html",
]
# 兜底：版本目录结构变化时全盘补搜
CANDIDATES += [p for p in glob.glob(
    r"F:\deepseek-harness\**\node_modules\@deepseek-ai\dsh-web-frontend\dist\index.html",
    recursive=True) if p not in CANDIDATES]


def strip_injected(html: str) -> str:
    for _js, begin, end in BLOCKS:
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
    if not any(begin in html for _js, begin, _end in BLOCKS):
        return "no injection found, skipped"
    html = strip_injected(html)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return "restored (injection removed)"


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
    return 0


if __name__ == "__main__":
    sys.exit(main())
