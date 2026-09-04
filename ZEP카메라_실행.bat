@echo off
REM ZEP 카메라 자동 클릭 - Windows 실행 런처
REM 이 파일을 더블클릭하면 콘솔 창 없이 GUI만 실행됩니다.

cd /d "%~dp0"

REM pythonw.exe = 콘솔 창 없는 파이썬 (GUI 전용)
where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw app.py
    exit /b
)

REM pythonw가 PATH에 없으면 py 런처로 시도
where py >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw.exe app.py 2>nul || start "" py -w app.py
    exit /b
)

echo [오류] 파이썬을 찾을 수 없습니다.
echo python.org 에서 Python을 설치하고 "Add Python to PATH"를 체크했는지 확인하세요.
pause
