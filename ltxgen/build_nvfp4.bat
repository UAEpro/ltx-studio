@echo off
rem Builds nvfp4_cpp (LTX-2 NVFP4 kernels) for RTX 50xx on Windows and installs it into the LTX-2 venv.
set "PATH=C:\Program Files (x86)\Microsoft Visual Studio\Installer;%PATH%"
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 1
set DISTUTILS_USE_SDK=1
set "CUDA_HOME=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.4"
set "PATH=%~dp0..\LTX-2\.venv\Scripts;%CUDA_HOME%\bin;%PATH%"
set "B=%TEMP%\nvfp4build"
if not exist "%B%" mkdir "%B%"
cd /d "%B%"
%~dp0..\LTX-2\.venv\Scripts\python.exe "%~dp0build_nvfp4.py" build_ext --build-temp "%B%\tmp" --build-lib %~dp0..\LTX-2\.venv\Lib\site-packages
