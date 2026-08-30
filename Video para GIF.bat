@echo off
rem Abre o conversor sem janela de console.
rem Dica: voce tambem pode arrastar um video para cima deste arquivo.
setlocal

where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw "%~dp0video_para_gif.py" %*
    exit /b 0
)

where python >nul 2>&1
if %errorlevel%==0 (
    start "" python "%~dp0video_para_gif.py" %*
    exit /b 0
)

echo Python nao foi encontrado no PATH.
echo Instale em https://www.python.org/downloads/ e tente de novo.
pause
exit /b 1
