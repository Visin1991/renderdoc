@echo off
setlocal EnableDelayedExpansion

REM ============================================================
REM  build_all.bat - Build Windows + Android simultaneously
REM  Usage: build_all.bat [options]
REM    --win-only     Only build Windows
REM    --android-only Only build Android
REM    --release      Build Release instead of Development
REM    --clean        Clean before build (recommended)
REM ============================================================

REM === Configuration (modify these paths if needed) ===
set "RENDERDOC_ROOT=%~dp0"
set "SLN_FILE=%RENDERDOC_ROOT%renderdoc.sln"
set "BUILD_CONFIG=Development"
set "BUILD_PLATFORM=x64"

REM MSYS2 installation path - modify if your MSYS2 is installed elsewhere
set "MSYS2_ROOT=C:\msys64"
if not exist "%MSYS2_ROOT%" (
    set "MSYS2_ROOT=D:\msys64"
)
if not exist "%MSYS2_ROOT%" (
    set "MSYS2_ROOT=E:\msys64"
)

REM === Parse command line arguments ===
set "BUILD_WIN=1"
set "BUILD_ANDROID=1"
set "DO_CLEAN=0"

:parse_args
if "%~1"=="" goto :done_args
if /i "%~1"=="--win-only" (
    set "BUILD_ANDROID=0"
    shift
    goto :parse_args
)
if /i "%~1"=="--android-only" (
    set "BUILD_WIN=0"
    shift
    goto :parse_args
)
if /i "%~1"=="--release" (
    set "BUILD_CONFIG=Release"
    shift
    goto :parse_args
)
if /i "%~1"=="--clean" (
    set "DO_CLEAN=1"
    shift
    goto :parse_args
)
shift
goto :parse_args
:done_args

REM === Display build plan ===
echo.
echo ============================================================
echo   RenderDoc Unified Build Script
echo ============================================================
echo   Root:     %RENDERDOC_ROOT%
echo   Config:   %BUILD_CONFIG% ^| %BUILD_PLATFORM%
echo   Clean:    %DO_CLEAN%
echo   Windows:  %BUILD_WIN%
echo   Android:  %BUILD_ANDROID%
echo   MSYS2:    %MSYS2_ROOT%
echo ============================================================
echo.

REM === Find MSBuild via vswhere ===
REM NOTE: ProgramFiles(x86) contains parentheses which break if() blocks in cmd.
REM       So we resolve paths OUTSIDE of any if() block.
set "MSBUILD_EXE="
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"

if "%BUILD_WIN%"=="0" goto :skip_find_msbuild

REM Try vswhere first (most reliable)
if exist "%VSWHERE%" (
    for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -requires Microsoft.Component.MSBuild -find MSBuild\**\Bin\MSBuild.exe`) do (
        set "MSBUILD_EXE=%%i"
    )
)

REM Fallback: try common VS paths
if "!MSBUILD_EXE!"=="" (
    if exist "%ProgramFiles%\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe" (
        set "MSBUILD_EXE=%ProgramFiles%\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe"
    )
)
if "!MSBUILD_EXE!"=="" (
    if exist "%ProgramFiles%\Microsoft Visual Studio\2022\Professional\MSBuild\Current\Bin\MSBuild.exe" (
        set "MSBUILD_EXE=%ProgramFiles%\Microsoft Visual Studio\2022\Professional\MSBuild\Current\Bin\MSBuild.exe"
    )
)
if "!MSBUILD_EXE!"=="" (
    if exist "%ProgramFiles%\Microsoft Visual Studio\2022\Enterprise\MSBuild\Current\Bin\MSBuild.exe" (
        set "MSBUILD_EXE=%ProgramFiles%\Microsoft Visual Studio\2022\Enterprise\MSBuild\Current\Bin\MSBuild.exe"
    )
)
if "!MSBUILD_EXE!"=="" (
    if exist "%ProgramFiles%\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\MSBuild.exe" (
        set "MSBUILD_EXE=%ProgramFiles%\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\MSBuild.exe"
    )
)

if "!MSBUILD_EXE!"=="" (
    echo [ERROR] MSBuild.exe not found! Please install Visual Studio with C++ workload.
    echo         Or set MSBUILD_EXE environment variable manually.
    if "%BUILD_ANDROID%"=="0" exit /b 1
    echo [WARN]  Skipping Windows build, continuing with Android only...
    set "BUILD_WIN=0"
) else (
    echo [INFO] Found MSBuild: !MSBUILD_EXE!
)

:skip_find_msbuild

REM === Validate MSYS2 ===
if "%BUILD_ANDROID%"=="0" goto :skip_validate_msys2
if exist "%MSYS2_ROOT%\mingw64.exe" goto :msys2_found
if exist "%MSYS2_ROOT%\msys2_shell.cmd" goto :msys2_found

echo [ERROR] MSYS2 not found at %MSYS2_ROOT%!
echo         Please install MSYS2 or set MSYS2_ROOT in this script.
if "%BUILD_WIN%"=="0" exit /b 1
echo [WARN]  Skipping Android build, continuing with Windows only...
set "BUILD_ANDROID=0"

:msys2_found
:skip_validate_msys2

REM === Create log directory ===
set "LOG_DIR=%RENDERDOC_ROOT%build_logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM === Get timestamp for log files ===
for /f "tokens=*" %%a in ('powershell -NoProfile -Command "Get-Date -Format 'yyyyMMdd_HHmmss'"') do set "TIMESTAMP=%%a"

REM === Clean up old result files before starting ===
if exist "%LOG_DIR%\win_result.tmp" del "%LOG_DIR%\win_result.tmp"
if exist "%LOG_DIR%\android_result.tmp" del "%LOG_DIR%\android_result.tmp"

REM === Start builds ===
set "WIN_LOG=%LOG_DIR%\win_build_%TIMESTAMP%.log"
set "ANDROID_LOG=%LOG_DIR%\android_build_%TIMESTAMP%.log"

REM --- Determine MSBuild target (Clean;Build or just Build) ---
set "MSBUILD_TARGET=Build"
if "%DO_CLEAN%"=="1" set "MSBUILD_TARGET=Clean;Build"

REM --- Start Windows build in background ---
if "%BUILD_WIN%"=="1" (
    echo.
    if "%DO_CLEAN%"=="1" (
        echo [WIN] Starting Windows CLEAN + BUILD: %BUILD_CONFIG%^|%BUILD_PLATFORM% ...
    ) else (
        echo [WIN] Starting Windows build: %BUILD_CONFIG%^|%BUILD_PLATFORM% ...
    )
    echo [WIN] Log: %WIN_LOG%

    start "RenderDoc-Windows-Build" /min cmd /c ^"^"!MSBUILD_EXE!^" ^"!SLN_FILE!^" /t:!MSBUILD_TARGET! /p:Configuration=!BUILD_CONFIG! /p:Platform=!BUILD_PLATFORM! /m /v:minimal /nologo ^> ^"!WIN_LOG!^" 2^>^&1 ^&^& echo WIN_SUCCESS ^> ^"!LOG_DIR!\win_result.tmp^" ^|^| echo WIN_FAILED ^> ^"!LOG_DIR!\win_result.tmp^"^"

    echo [WIN] Build started in background.
)

REM --- Start Android build in background ---
if "%BUILD_ANDROID%"=="1" (
    echo.
    echo [ANDROID] Starting Android build via MSYS2 MINGW64 ...
    echo [ANDROID] Log: %ANDROID_LOG%

    REM Use msys2_shell.cmd with -mingw64 to launch MINGW64 environment
    REM -defterm: use default terminal (cmd window)
    REM -no-start: don't open a new window (we handle it ourselves)
    REM -here: use current directory
    REM -c: run command

    REM Convert Windows paths to MSYS2 paths for the shell command
    set "MSYS_LOG_DIR=%LOG_DIR:\=/%"
    set "MSYS_LOG_DIR=!MSYS_LOG_DIR:C:/=/c/!"
    set "MSYS_LOG_DIR=!MSYS_LOG_DIR:D:/=/d/!"
    set "MSYS_LOG_DIR=!MSYS_LOG_DIR:E:/=/e/!"

    REM Android clean: remove old build directories before build_android_all.sh
    REM (build_android_all.sh already does rm -rf internally, but we also clean build-android output)
    if "%DO_CLEAN%"=="1" (
        echo [ANDROID] Cleaning old Android build directories...
        if exist "%RENDERDOC_ROOT%build-android-arm32" rmdir /s /q "%RENDERDOC_ROOT%build-android-arm32"
        if exist "%RENDERDOC_ROOT%build-android-arm64" rmdir /s /q "%RENDERDOC_ROOT%build-android-arm64"
        if exist "%RENDERDOC_ROOT%build-android" rmdir /s /q "%RENDERDOC_ROOT%build-android"
    )

    REM Convert RENDERDOC_ROOT to MSYS2 path
    set "MSYS_ROOT_PATH=%RENDERDOC_ROOT:\=/%"
    set "MSYS_ROOT_PATH=!MSYS_ROOT_PATH:C:/=/c/!"
    set "MSYS_ROOT_PATH=!MSYS_ROOT_PATH:D:/=/d/!"
    set "MSYS_ROOT_PATH=!MSYS_ROOT_PATH:E:/=/e/!"

    start "RenderDoc-Android-Build" /min "!MSYS2_ROOT!\msys2_shell.cmd" -mingw64 -defterm -no-start -here -c "cd '!MSYS_ROOT_PATH!' && bash build_android_all.sh > '!MSYS_LOG_DIR!/android_build_!TIMESTAMP!.log' 2>&1 && echo ANDROID_SUCCESS > '!MSYS_LOG_DIR!/android_result.tmp' || echo ANDROID_FAILED > '!MSYS_LOG_DIR!/android_result.tmp'"

    echo [ANDROID] Build started in background.
)

REM === Wait for builds to complete ===
echo.
echo ============================================================
echo   Waiting for builds to complete...
echo   (You can check logs in %LOG_DIR%)
echo ============================================================
echo.

set "WIN_DONE=0"
set "ANDROID_DONE=0"
if "%BUILD_WIN%"=="0" set "WIN_DONE=1"
if "%BUILD_ANDROID%"=="0" set "ANDROID_DONE=1"

REM Polling loop - check every 5 seconds
:wait_loop
timeout /t 5 /nobreak >nul

if "%WIN_DONE%"=="0" (
    if exist "%LOG_DIR%\win_result.tmp" (
        set /p WIN_RESULT=<"%LOG_DIR%\win_result.tmp"
        set "WIN_DONE=1"
        echo [WIN] Build finished: !WIN_RESULT!
    )
)

if "%ANDROID_DONE%"=="0" (
    if exist "%LOG_DIR%\android_result.tmp" (
        set /p ANDROID_RESULT=<"%LOG_DIR%\android_result.tmp"
        set "ANDROID_DONE=1"
        echo [ANDROID] Build finished: !ANDROID_RESULT!
    )
)

if "%WIN_DONE%"=="0" goto :wait_loop
if "%ANDROID_DONE%"=="0" goto :wait_loop

REM === Report results ===
echo.
echo ============================================================
echo   Build Results
echo ============================================================

set "EXIT_CODE=0"

if "%BUILD_WIN%"=="1" (
    if "!WIN_RESULT!"=="WIN_SUCCESS" (
        echo   [WIN]     SUCCESS  - Output: %RENDERDOC_ROOT%%BUILD_PLATFORM%\%BUILD_CONFIG%\
        echo                        Log:    %WIN_LOG%
    ) else (
        echo   [WIN]     FAILED   - Check log: %WIN_LOG%
        set "EXIT_CODE=1"
    )
)

if "%BUILD_ANDROID%"=="1" (
    if "!ANDROID_RESULT!"=="ANDROID_SUCCESS" (
        echo   [ANDROID] SUCCESS  - Output: %RENDERDOC_ROOT%build-android\bin\
        echo                        Log:    %ANDROID_LOG%
    ) else (
        echo   [ANDROID] FAILED   - Check log: %ANDROID_LOG%
        set "EXIT_CODE=1"
    )
)

echo ============================================================
echo.

REM Cleanup temp files
if exist "%LOG_DIR%\win_result.tmp" del "%LOG_DIR%\win_result.tmp"
if exist "%LOG_DIR%\android_result.tmp" del "%LOG_DIR%\android_result.tmp"

if "%EXIT_CODE%"=="0" (
    echo All builds completed successfully!
) else (
    echo Some builds failed. Please check the logs above.
)

exit /b %EXIT_CODE%
