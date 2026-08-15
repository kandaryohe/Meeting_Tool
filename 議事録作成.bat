@echo off
rem ---------------------------------------------------------------
rem  Minutes maker - drag and drop an audio folder onto this file.
rem
rem  IMPORTANT: keep this file ASCII only.
rem  Japanese text inside a .bat breaks depending on the console
rem  code page, so all messages live in make_minutes.py instead.
rem ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

py -3.14 make_minutes.py %1
