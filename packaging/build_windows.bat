@echo off
REM Build the self-contained Windows app: dist\TIFF Visualizer\TIFF Visualizer.exe
REM Usage: run from the repo root on a Windows machine with Python 3.10+ installed:
REM     packaging\build_windows.bat
REM Distribute by zipping the whole "dist\TIFF Visualizer" folder - it contains
REM Python, Qt and all dependencies; users just run the .exe inside.

cd /d "%~dp0\.."

if not exist .venv-win (
    python -m venv .venv-win || goto :error
)
call .venv-win\Scripts\pip install --quiet --upgrade pip || goto :error
call .venv-win\Scripts\pip install --quiet -e . pyinstaller pillow || goto :error

if not exist packaging\icon.ico (
    call .venv-win\Scripts\python packaging\make_icon.py || goto :error
)

call .venv-win\Scripts\pyinstaller --noconfirm --clean --windowed ^
    --name "TIFF Visualizer" ^
    --icon packaging\icon.ico ^
    --paths . ^
    --add-data "tiff_visualizer\assets;tiff_visualizer\assets" ^
    packaging\launcher.py || goto :error

echo.
echo Done: dist\"TIFF Visualizer"\"TIFF Visualizer.exe"
goto :eof

:error
echo Build failed.
exit /b 1
