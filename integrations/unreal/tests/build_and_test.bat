@echo off
REM Build and run the standalone protocol test with MSVC. No Unreal needed.
REM Usage: build_and_test.bat                       (auto-detects Visual Studio via vswhere)
REM        build_and_test.bat "C:\Path\To\vcvars64.bat"
setlocal EnableDelayedExpansion
set "HERE=%~dp0"
set "VCVARS=%~1"
if defined VCVARS goto :have_vcvars

if defined VCVARS64 set "VCVARS=%VCVARS64%"
if defined VCVARS goto :have_vcvars
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" goto :no_vcvars
REM doubled outer quotes survive the quote-stripping cmd /c applies to for /f commands
REM delayed expansion keeps the "(x86)" in the path from closing the for-parens at parse time
for /f "usebackq delims=" %%i in (`""!VSWHERE!" -latest -products * -property installationPath"`) do set "VSROOT=%%i"
if not defined VSROOT goto :no_vcvars
set "VCVARS=%VSROOT%\VC\Auxiliary\Build\vcvars64.bat"

:have_vcvars
if not exist "!VCVARS!" goto :no_vcvars
call "!VCVARS!" >nul
if not exist "%HERE%out" mkdir "%HERE%out"
cl /nologo /EHsc /std:c++17 /W4 /O2 /I"%HERE%..\VoiceRT\Source\VoiceRT\Public" "%HERE%protocol_test.cpp" /Fe:"%HERE%out\protocol_test.exe" /Fo:"%HERE%out\\"
if errorlevel 1 exit /b 1
"%HERE%out\protocol_test.exe"
exit /b !errorlevel!

:no_vcvars
echo vcvars64.bat not found; install the MSVC C++ toolset or build with g++ (see protocol_test.cpp header).
exit /b 2
