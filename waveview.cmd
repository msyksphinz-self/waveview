@echo off
rem Windows launcher for tools\waveview.py (Python 3 from python.org or winget; OpenSSH client is built in).
rem   waveview serve build01:/work/sim/w.fst --rtl build01:/work/scariv/core/filelist.f --open
where py >nul 2>nul && (py -3 "%~dp0waveview.py" %*) || (python "%~dp0waveview.py" %*)
