@echo off
REM Arrastre un archivo (.xlsx, .xlsm, .csv, .txt) sobre este .bat para perfilarlo.
REM Si "python" no se reconoce, cambie la linea de abajo por la ruta completa de python.exe
set PY=python
if "%~1"=="" (
  echo Arrastre un archivo sobre este .bat, o use:  perfilar_archivo.bat ruta\archivo.xlsx --llave id
  pause
  exit /b 1
)
%PY% "%~dp0perfilar_archivo.py" %*
pause
