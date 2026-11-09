@echo off
setlocal EnableExtensions
rem ======================================================================
rem  Build JABR as a Windows program (.exe) with PyInstaller.
rem
rem    build_windows.bat            one file:  dist\JABR.exe
rem    build_windows.bat folder     a folder:  dist\JABR\JABR.exe
rem                                 (starts faster, fewer antivirus false alarms)
rem    build_windows.bat console    keeps a console window, to see errors if the
rem                                 program closes immediately (can be combined)
rem
rem  Needs: Python 3.9 or newer from python.org, and internet access.
rem  Double-click works too; the window stays open at the end.
rem  Set NOPAUSE=1 to skip the final "press any key".
rem ======================================================================

cd /d "%~dp0"
set "HERE=%~dp0"
set "BUILD=%HERE%build-windows"
set "DIST=%HERE%dist"
set "MODE=onefile"
set "WINOPT=--windowed"
call :arg %1
call :arg %2
call :arg %3

echo.
echo ==^> Checking the project files
set "MISSING="
for %%F in (jabr.py packaging\jabr.png icons\jabr.ico) do if not exist "%HERE%%%F" set "MISSING=1" & echo missing: %%F
if defined MISSING goto :missingfiles

echo.
echo ==^> Checking Python
set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY where python >nul 2>nul
if not defined PY if not errorlevel 1 set "PY=python"
if not defined PY goto :nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
if errorlevel 1 goto :nopython
%PY% -V

echo.
echo ==^> Setting up the build environment
if not exist "%BUILD%" mkdir "%BUILD%"
%PY% -m venv "%BUILD%\venv"
if errorlevel 1 goto :fail
set "VPY=%BUILD%\venv\Scripts\python.exe"
"%VPY%" -m pip install --quiet --upgrade pip
if errorlevel 1 goto :fail
"%VPY%" -m pip install --quiet pyinstaller "PySide6-Essentials~=6.5"
if errorlevel 1 goto :fail

echo.
echo ==^> Building with PyInstaller (%MODE%)
if exist "%DIST%\JABR.exe" del /q "%DIST%\JABR.exe"
if exist "%DIST%\JABR" rmdir /s /q "%DIST%\JABR"
"%VPY%" -m PyInstaller --noconfirm --clean %WINOPT% --%MODE% --name JABR --icon "%HERE%icons\jabr.ico" --add-data "%HERE%packaging\jabr.png;packaging" --distpath "%DIST%" --workpath "%BUILD%\pyi" --specpath "%BUILD%" "%HERE%jabr.py"
if errorlevel 1 goto :fail

set "EXE=%DIST%\JABR.exe"
if "%MODE%"=="onedir" set "EXE=%DIST%\JABR\JABR.exe"
if not exist "%EXE%" goto :fail

echo.
echo ==^> Done
echo Your program: %EXE%
echo.
echo Double-click it to run. The first start of the one-file version takes a few seconds.
echo If Windows SmartScreen warns about an unknown publisher: More info ^> Run anyway.
if not defined NOPAUSE pause
exit /b 0

:missingfiles
echo.
echo ERROR: some project files are missing. Run this script from the folder that
echo        contains jabr.py, packaging\ and icons\ - the files listed above are not there.
goto :fail

:nopython
echo.
echo ERROR: Python 3.9 or newer was not found.
echo        Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
echo        The Microsoft Store "python" shortcut does not count.
goto :fail

:fail
echo.
echo !! BUILD FAILED - see the messages above. No program was created.
if not defined NOPAUSE pause
exit /b 1

:arg
if /I "%~1"=="folder" set "MODE=onedir"
if /I "%~1"=="console" set "WINOPT=--console"
goto :eof
