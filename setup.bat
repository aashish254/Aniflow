@echo off
REM ═══════════════════════════════════════════════════════════════════
REM  AniFlow — One-Command Setup for Windows
REM  Run this after cloning the repository:
REM    setup.bat
REM ═══════════════════════════════════════════════════════════════════

echo.
echo ╔═══════════════════════════════════════════════╗
echo ║           AniFlow Setup Script                ║
echo ╚═══════════════════════════════════════════════╝
echo.

REM ─── 1. Check Python ───────────────────────────────────────────
echo [1/5] Checking Python...
python --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo   ❌ Python not found. Install from https://www.python.org/downloads/
    echo      Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)
for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PY_VERSION=%%i
echo   ✅ Python %PY_VERSION%

REM ─── 2. Check FFmpeg ───────────────────────────────────────────
echo [2/5] Checking FFmpeg...
ffmpeg -version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo   ❌ FFmpeg not found.
    echo      Download from https://ffmpeg.org/download.html
    echo      Or install with: winget install FFmpeg
    pause
    exit /b 1
)
echo   ✅ FFmpeg found

REM ─── 3. Create Virtual Environment ────────────────────────────
echo [3/5] Setting up Python virtual environment...
if exist "venv" (
    echo   ✅ Virtual environment already exists
) else (
    echo   📦 Creating virtual environment...
    python -m venv venv
    echo   ✅ Virtual environment created
)
call venv\Scripts\activate.bat

REM ─── 4. Install Dependencies ──────────────────────────────────
echo [4/5] Installing Python dependencies...
echo   (This may take 10-20 minutes on first run — PyTorch is large)
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
echo   ✅ Dependencies installed

REM ─── 5. Setup .env File ───────────────────────────────────────
echo [5/5] Setting up configuration...
if not exist ".env" (
    copy .env.example .env >nul
    echo   ✅ Created .env from template
    echo   📝 Edit .env to add API keys (optional)
) else (
    echo   ✅ .env already exists
)

REM ─── Create directories ───────────────────────────────────────
if not exist "input\raw_manhwa" mkdir "input\raw_manhwa"
if not exist "output" mkdir "output"
if not exist "temp" mkdir "temp"
if not exist "casts" mkdir "casts"
if not exist "projects" mkdir "projects"
if not exist "assets\bgm" mkdir "assets\bgm"
if not exist "assets\watermarks" mkdir "assets\watermarks"
if not exist "assets\backgrounds" mkdir "assets\backgrounds"
if not exist "assets\intros" mkdir "assets\intros"
if not exist "assets\outros" mkdir "assets\outros"
if not exist "manhwa_download" mkdir "manhwa_download"
if not exist "manhwa_stitch" mkdir "manhwa_stitch"
if not exist "manhwa_detect" mkdir "manhwa_detect"
if not exist "manhwa_crop" mkdir "manhwa_crop"
if not exist "manhwa_text" mkdir "manhwa_text"
if not exist "manhwa_audio" mkdir "manhwa_audio"
if not exist "manhwa_clips" mkdir "manhwa_clips"

echo.
echo ╔═══════════════════════════════════════════════╗
echo ║           ✅ Setup Complete!                  ║
echo ╚═══════════════════════════════════════════════╝
echo.
echo   Start AniFlow:
echo     venv\Scripts\activate
echo     python app.py
echo.
echo   Then open http://localhost:8080 in your browser.
echo.
echo   Optional:
echo     * Edit .env to add Gemini/Claude/ElevenLabs API keys
echo     * Install Ollama from https://ollama.com
echo     * Run: ollama pull qwen2.5vl:3b
echo     * Run: ollama pull qwen2.5:14b
echo.
pause
