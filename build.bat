@echo off
rem Builds the app (dist\Transcriber\) and the installer (dist\Transcriber-Setup-<version>.exe).
cd /d "%~dp0"
for /f "tokens=2 delims==" %%v in ('findstr /b "VERSION" app.py') do set VER=%%~v
set VER=%VER: =%
set VER=%VER:"=%
for /f "tokens=1 delims=-" %%n in ("%VER%") do set NUM=%%n
echo Building Transcriber %VER% (%NUM%)

python -m PyInstaller --noconfirm --clean --windowed --name Transcriber ^
  --icon build-assets\icon.ico ^
  --add-data "index.html;." --add-data "app.js;." --add-data "style.css;." --add-data "build-assets\icon.png;." ^
  app.py || exit /b 1

set ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe
if not exist "%ISCC%" set ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe
"%ISCC%" /DAppVersion=%VER% /DNumVersion=%NUM% installer.iss || exit /b 1
echo Done: dist\Transcriber-Setup-%VER%.exe
