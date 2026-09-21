# -*- coding: utf-8 -*-
# \u540e\u53f0\u542f\u52a8 copilot-api server\uff084141\uff09\uff1a\u53cc\u51fb\u6216 py \u8fd0\u884c\u672c\u6587\u4ef6\u5373\u53ef
import subprocess, os
CWD = os.path.dirname(os.path.abspath(__file__))
log = open(os.path.join(CWD, "server.log"), "ab")
p = subprocess.Popen(
    ["node", os.path.join(CWD, "node_modules", "copilot-api", "dist", "main.js"), "start"],
    cwd=CWD, stdout=log, stderr=subprocess.STDOUT,
    creationflags=0x00000008 | 0x00000200)
print("copilot-api server started, pid=%d, port=4141" % p.pid)
