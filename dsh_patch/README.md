# dsh_patch — DeepSeek Harness 面板定制补丁

把 LLM Key Hub 的模型切换卡片注入 dsh（DeepSeek Harness）Web 面板，
挂载在输入区「标准模式」下拉旁。dsh 的前端是本地文件
（`@deepseek-ai/dsh-web-frontend/dist`），dsh 的静态服务每个请求都会
重新读盘，所以补丁刷新页面即生效，无需重启 dsh。

## 文件

- `patch_dsh.py` — 幂等补丁器。`py patch_dsh.py` 注入/更新；
  `py patch_dsh.py --restore` 还原。改动以 `<!-- HUB-MODEL-CARD BEGIN/END -->`
  标记包裹，重复运行先剥后插；原文件备份为 `index.html.bak_hub`（每份 dist 一次）。
  dsh 升级会覆盖 dist，升级后重跑本脚本即可。
- `hub_model_card.js` — 注入的卡片本体。轮询等「标准模式」锚点出现后挂载，
  SPA 重渲染自动重挂；长期找不到锚点时降级为悬浮卡片兜底。
  卡片调 Hub（127.0.0.1:8787）的 `/api/harness/model` 读/写 pin，
  依赖 server.py 的 CORS 放行（`http://127.0.0.1:3080`）。
- `snapshots/` — 打过补丁的 dist/index.html 快照（app 与 home\profiles 两份），
  记录 dsh 侧实际改动内容；`versions.json` 记录补丁时的包版本。
