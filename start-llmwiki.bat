@echo off
setlocal

set "PYTHONUTF8=1"
set "LLMWIKI_REPO=%~dp0"
set "LLMWIKI_WORKSPACE=G:\llmdata"
set "LLMWIKI_INDEX_PATH=%LOCALAPPDATA%\LLMWiki\indexes\llmdata"

if not exist "%LLMWIKI_WORKSPACE%" mkdir "%LLMWIKI_WORKSPACE%"

"%LLMWIKI_REPO%.venv\Scripts\python.exe" "%LLMWIKI_REPO%llmwiki" open "%LLMWIKI_WORKSPACE%"

endlocal
