# -*- coding: utf-8 -*-
"""plugin_deploy.py — 把 llm_hub/dsh-plugins/<name>/lib 同步到 dsh 运行时副本。

背景：dsh 通过 `@dsh-local/<name>` 插件协议加载插件，但实际执行体是 dsh 自己的
node_modules 里的一个拷贝（`profiles/web/node_modules/@dsh-local/<name>/lib/...`），
不是 `F:\llm_hub\dsh-plugins\<name>\lib\...`（那是开发母版）。
改完母版只重启 dsh 不会自动同步——dsh 启动时不会把母版拷过去（验证 R34）。

本脚本做的事：
    对 `F:\llm_hub\dsh-plugins\<name>\lib\` 下存在 + 目标副本已存在的插件，
    把母版下所有文件（含 .bak 等附属）覆盖拷贝到
    `F:\deepseek-harness\home\profiles\web\node_modules\@dsh-local\<name>\lib\`。

用法：
    py plugin_deploy.py                     # 同步全部已部署的插件
    py plugin_deploy.py remote-agent          # 仅同步指定插件
    py plugin_deploy.py --dry-run           # 仅打印将拷贝哪些文件，不实际写

幂等：每次运行都做全量覆盖，不会出现"漏拷"；版本兼容也由 dsh 自己决定
（菜单动态包 vs 静态包由 dsh 内部按 package.json 处理）。
"""
import glob
import os
import shutil
import sys

MASTER_ROOT = r"F:\llm_hub\dsh-plugins"
DEPLOY_ROOT = r"F:\deepseek-harness\home\profiles\web\node_modules\@dsh-local"

# 跳过这些辅助文件，避免把开发期的 .bak 之类塞进运行时目录
SKIP_PATTERNS = (".bak_r", ".bak-hub")


def list_installed_plugins() -> list[str]:
    """枚举 dsh 已安装（部署副本已存在）的 @dsh-local/* 插件。"""
    out = []
    if not os.path.isdir(DEPLOY_ROOT):
        return out
    for name in sorted(os.listdir(DEPLOY_ROOT)):
        if os.path.isdir(os.path.join(DEPLOY_ROOT, name)):
            out.append(name)
    return out


def sync_one(name: str, dry_run: bool) -> list[str]:
    src_lib = os.path.join(MASTER_ROOT, name, "lib")
    dst_lib = os.path.join(DEPLOY_ROOT, name, "lib")
    if not os.path.isdir(src_lib):
        return ["MISS master %s (跳过)" % src_lib]
    if not os.path.isdir(dst_lib):
        return ["SKIP %s (目标副本未安装,跳过)" % name]
    notes = []
    for fname in sorted(os.listdir(src_lib)):
        if any(pat in fname for pat in SKIP_PATTERNS):
            notes.append("skip   %s/lib/%s (matched skip pattern)" % (name, fname))
            continue
        s = os.path.join(src_lib, fname)
        d = os.path.join(dst_lib, fname)
        if not os.path.isfile(s):
            continue
        if dry_run:
            notes.append("would  %s/lib/%s (%d bytes)" % (name, fname, os.path.getsize(s)))
            continue
        # 备份目标中现有同名文件（仅当目标有且不与母版同大小）
        if os.path.exists(d) and os.path.getsize(d) != os.path.getsize(s):
            bak = d + ".bak_pre_plugin_deploy"
            if not os.path.exists(bak):
                shutil.copy2(d, bak)
                notes.append("backed up %s -> %s" % (os.path.basename(d), bak))
        shutil.copy2(s, d)
        notes.append("copied %s/lib/%s (%d bytes)" % (name, fname, os.path.getsize(s)))
    return notes


def main() -> int:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    targets = [a for a in args if not a.startswith("--")]
    if not targets:
        installed = list_installed_plugins()
        if not installed:
            print("ERROR: 部署副本目录无插件：%s" % DEPLOY_ROOT)
            return 1
        targets = installed

    mode = "DRY-RUN" if dry_run else "APPLY"
    print("plugin-deploy [%s] targets: %s" % (mode, ", ".join(targets)))
    rc = 0
    for name in targets:
        notes = sync_one(name, dry_run)
        for ln in notes:
            print("  " + ln)
        # 任何 MISS 算失败
        if any("MISS" in n or "SKIP " in n and "跳过" in n for n in notes):
            rc = 1
    print("DONE rc=%d" % rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())