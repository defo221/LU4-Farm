@echo off
rem Stage all changes and push to remote.
pushd "%~dp0"

git add .
set /p MSG="Commit message: "
if "%MSG%"=="" set MSG=update %date% %time%
git commit -m "%MSG%"
git push

pause
popd
