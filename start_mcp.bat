@echo off
REM ============================================================
REM  ComfyUI-Roundabout MCP 网关启动器（stdio 传输）
REM  供 MCP 客户端（WorkBuddy / Claude Desktop 等）作为外部命令调用，
REM  或手动启动测试：python mcp_server.py
REM ============================================================
setlocal

REM 定位 ComfyUI 根目录（脚本在 custom_nodes/ComfyUI-Roundabout/ 下）
set "NODE_DIR=%~dp0"
set "COMFY_DIR=%NODE_DIR%..\.."

REM 优先使用 ComfyUI 自带的 python（秋叶整合包）
set "PY=%COMFY_DIR%\..\python\python.exe"
if not exist "%PY%" set "PY=python"

cd /d "%NODE_DIR%"
"%PY%" mcp_server.py %*
endlocal
