@echo off
rem Dobavlyaet sertifikat servisa "Efir" v doverennye kornevye Windows.
rem Posle etogo brauzer perestaet pokazyvat preduprezhdenie na etom PK.
rem Zapusk: dvoynoy klik. Prava administratora zaprosyatsya sami.

set CERT=%~dp0instance\cert.pem

if not exist "%CERT%" (
    echo Certificate not found:
    echo   %CERT%
    echo.
    echo Run "python main.py" once - it creates the certificate on first start.
    echo.
    pause
    exit /b
)

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator rights...
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo Installing certificate into Trusted Root Certification Authorities...
certutil -addstore -f Root "%CERT%"

echo.
echo Done. Restart the browser completely, then open the https:// address.
echo.
echo To remove later:  certutil -delstore Root "Efir Local"
echo Firefox has its own store: Settings - Certificates - Import.
echo.
pause
