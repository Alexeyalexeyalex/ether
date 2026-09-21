@echo off
rem Otkryvaet port v brandmauere Windows dlya servisa "Efir".
rem Zapusk: dvoynoy klik. Prava administratora zaprosyatsya sami.
rem Drugoy port: open-port.bat 8080

set PORT=%1
if "%PORT%"=="" set PORT=2079

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator rights...
    powershell -Command "Start-Process -FilePath '%~f0' -ArgumentList '%PORT%' -Verb RunAs"
    exit /b
)

echo Opening TCP port %PORT% for inbound connections...
netsh advfirewall firewall delete rule name="Efir %PORT%" >nul 2>&1
netsh advfirewall firewall add rule name="Efir %PORT%" dir=in action=allow protocol=TCP localport=%PORT% profile=any

echo.
echo Done. Firewall rule "Efir %PORT%" created.
echo.
pause
