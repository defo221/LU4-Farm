@echo off
rem Run ONCE to initialise the local git repo and link it to GitHub.
rem After this, use git_push.bat for all future updates.
pushd "%~dp0"

git init
git add .
git commit -m "initial commit"

echo.
set /p REMOTE="Paste your GitHub remote URL (https://github.com/you/repo.git): "
git remote add origin %REMOTE%
git branch -M main
git push -u origin main

echo.
echo Done. Use git_push.bat from now on.
pause
popd
