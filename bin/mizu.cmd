@echo off
REM Windows launcher: bin\mizu has a POSIX shebang; invoke it via Python.
python "%~dp0mizu" %*
