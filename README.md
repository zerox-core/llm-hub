# LLM Key Hub

本地大模型 Key 与免费额度管理面板 + 统一 OpenAI 兼容调用入口。

## 这个项目是干什么的

手头有多个大模型账号体系（阿里云百炼、Google Antigravity「反重力」、DeepSeek 等 OpenAI 兼容服务）时，Key 散落各处、免费额度看不清、模型切换麻烦。本工具把它们集中到本机一个面板里管理，并把所有渠道聚合成 **一条 Key + 一个 URL** 的统一入口：客户端只用填一次接入信息，`MODEL=auto` 时自动在「当前还有免费额度」的渠道与模型之间轮动，额度类失败自动换下一个。

纯本地运行（127.0.0.1:8787），所有 Key 只存在本机。

## 功能

- **渠道管理**：填 Base URL + API Key，自动拉取模型列表、连通性测试、模型排序、当前模型切换、一键复制接入三件套
- **百炼免费额度同步**：一键同步各模型免费额度与到期时间（首次需一次性控制台授权）；「仅免费额度」策略防止误触付费模型，确需付费可单渠道勾选放行；「试用完即停」一键开启
- **Antigravity 反重力接入**：经 CLIProxyAPI 本地代理接入 Google Antigravity；面板直接同步其 **5 小时 / 每周额度**（按 Gemini 组、Claude/GPT 组分组展示，组内模型共享同一额度），耗尽的组自动冷却、到重置点自动恢复，轮动时自动跳过
- **统一入口（跨渠道大轮动）**：`POST /v1/chat/completions`
  - `model=auto`：跨所有渠道按顺序轮动可调用模型
  - 指定具体模型：自动定位拥有该模型的渠道
  - 请求头 `X-Hub-Provider`：限定只在某个渠道内轮动
  - 响应头 `X-Hub-Provider` / `X-Hub-Model`：标明本次实际命中的渠道与模型
- **统一鉴权**：`/v1` 接口需携带 Hub 自动生成的统一 key（面板顶部查看、点击复制）
- **调用监控**：每次调用的渠道 / 模型 / 耗时 / token / 成败全量日志与统计页（/monitor）
- **桌面一键启动**：`start_all.bat` 同时拉起反重力代理与 Hub 面板

## 快速开始

1. Python 3.10+，安装依赖：`pip install fastapi uvicorn httpx pydantic`
2. 双击 `start.bat`（或 `py -3 server.py`），浏览器打开 http://127.0.0.1:8787
3. 点「+ 添加渠道」，填入 Base URL 和 API Key，模型列表自动拉取
4. 渠道卡片点「复制轮询三件套」，把 BASE_URL / API_KEY / MODEL=auto 贴进任何 OpenAI 兼容客户端即可

## Antigravity（反重力）接入

1. 将 CLIProxyAPI 放入 `cliproxy/` 目录（监听 127.0.0.1:8317）
2. 首次运行 `cli-proxy-api.exe -config config.yaml -antigravity-login`，在浏览器完成 Google 授权（授权文件存于用户目录 `.cli-proxy-api/`）
3. Hub 把该代理注册为一个渠道即可；额度同步、组冷却、跨渠道轮动全部自动

## 公网使用（可选）

想把额度共享给其他设备（非局域网）使用：

- 用 Cloudflare Tunnel（或任意内网穿透）把本机 **8317 端口**（反重力代理）映射到自有域名即可，鉴权由 CLIProxyAPI 的 api-key 保障（无 key 一律 401）
- 同理也可映射 Hub 的 `/v1` 统一入口，鉴权由 Hub 统一 key 保障
- 注意：管理面板 `/api/*` 仅面向本机、无鉴权，**不要**把 8787 整站暴露到公网
- 若拥有海外服务器，也可以把 CLIProxyAPI 直接部署到服务器（复制授权文件过去），彻底脱离本机开机依赖；中国大陆服务器无法直连 Google API，不适合部署反重力代理

## 安全说明

- 所有 API Key 仅保存在本机 `data.json`，已被 .gitignore 排除，不会进入版本库
- `cliproxy/` 目录含代理配置与密钥，同样不入库
- Hub 统一 key 首次启动自动生成并落盘；如怀疑泄露，删除 `data.json` 中的 `hub_key` 字段重启即可重新生成

## 目录结构

- `server.py` —— FastAPI 服务（面板 API + 统一入口 + 额度同步 + 监控）
- `static/index.html` —— 渠道管理面板
- `static/monitor.html` —— 调用监控页
- `start.bat` / `start_all.bat` —— 启动脚本（幂等，重复启动不会起第二个实例）
