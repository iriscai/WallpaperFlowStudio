@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 壁纸生创流程台
if not exist "node_modules\electron\dist\electron.exe" (
  echo 第一次启动，正在准备桌面程序...
  call npm install
  if errorlevel 1 goto fail
)
start "" "%~dp0node_modules\electron\dist\electron.exe" "%~dp0"
exit /b 0
:fail
echo 桌面程序没有启动。
pause
exit /b 1
