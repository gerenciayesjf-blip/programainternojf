@echo off
setlocal
cd /d "%~dp0"

echo.
echo ==========================================
echo  Gerando EXE do painel Sponte para Asaas
echo ==========================================
echo.

python -m pip install --upgrade pip
if errorlevel 1 goto :error

python -m pip install pyinstaller requests
if errorlevel 1 goto :error

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --name "PainelSponteAsaas" ^
  --windowed ^
  --onedir ^
  app.py
if errorlevel 1 goto :error

echo.
echo EXE gerado com sucesso em:
echo %~dp0dist\PainelSponteAsaas\
echo.
echo Abra o arquivo:
echo %~dp0dist\PainelSponteAsaas\PainelSponteAsaas.exe
echo.
pause
exit /b 0

:error
echo.
echo Ocorreu um erro ao gerar o EXE.
echo.
pause
exit /b 1
