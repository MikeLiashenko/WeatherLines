@echo off
chcp 65001 >nul
rem Запуск бота. При первом запуске сам создаёт окружение и ставит библиотеки.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Первый запуск: устанавливаю библиотеки...
    py -3 -m venv .venv || goto :error
    .venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements.txt || goto :error
)
:loop
.venv\Scripts\python.exe -m weatherlines %*
if "%~1"=="" (
    echo Бот остановился. Перезапуск через 15 секунд... Закройте окно, чтобы выйти.
    timeout /t 15 /nobreak >nul
    goto :loop
)
exit /b

:error
echo Не удалось подготовить окружение. Проверьте, что установлен Python 3.11 или новее.
pause
