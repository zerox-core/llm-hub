@echo off
rem LLM Hub 一键启动：反重力代理(CLIProxyAPI) + Hub 面板
rem 两个子脚本各自幂等：已在运行则不会重复启动
start "" /min cmd /c "F:\llm_hub\cliproxy\start_proxy.bat"
start "" /min cmd /c "F:\llm_hub\start.bat"
exit /b 0
