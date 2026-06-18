@echo off
chcp 65001 >nul
title 诸天万界 · LifeUp 存档管理器

set BACKUP=%1
if "%BACKUP%"=="" set BACKUP=C:\Users\USER\Nutstore\1\LifeUp\LifeupBackup.zip

echo.
echo  ☯ 诸天万界 · LifeUp 存档管理器
echo  ─────────────────────────────
echo  存档: %BACKUP%
echo  面板: http://localhost:5000
echo.
echo  按 Ctrl+C 停止服务
echo.

python "%~dp0server.py" "%BACKUP%"
pause
