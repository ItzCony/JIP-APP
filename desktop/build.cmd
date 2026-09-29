@echo off
rem Sestaví desktopovou aplikaci Moje JIPka a instalátor dist\JIPka-setup.exe.
rem Portál ho přihlášeným nabízí ke stažení (tlačítko v hlavičce), jakmile soubor existuje.
rem Adresu serveru určuje VychoziUrl v Program.cs.
rem Potřeba: Windows 10/11 (csc.exe z .NET Frameworku, curl a tar jsou součástí systému)
rem a Inno Setup 6 (jednorázově: winget install JRSoftware.InnoSetup).
setlocal
cd /d "%~dp0"

rem Knihovny WebView2 z NuGetu – pevná verze, stahují se jen poprvé do build\.
rem curl a tar přímo ze System32 – tar z Git for Windows neumí ZIP (nupkg).
set "WV2=1.0.4258.31"
set "NUPKG=build\webview2-%WV2%.nupkg"
set "CURL=%SystemRoot%\System32\curl.exe"
set "TAR=%SystemRoot%\System32\tar.exe"
if not exist build mkdir build
if not exist "%NUPKG%" (
    "%CURL%" -fsSL -o "%NUPKG%.tmp" "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/%WV2%/microsoft.web.webview2.%WV2%.nupkg" || goto chyba
    move /y "%NUPKG%.tmp" "%NUPKG%" >nul || goto chyba
)
"%TAR%" -xf "%NUPKG%" -C build --strip-components=2 lib/net462/Microsoft.Web.WebView2.Core.dll lib/net462/Microsoft.Web.WebView2.WinForms.dll || goto chyba
"%TAR%" -xf "%NUPKG%" -C build --strip-components=3 runtimes/win-x64/native/WebView2Loader.dll || goto chyba

"%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /codepage:65001 /target:winexe /platform:x64 /optimize+ ^
    /out:build\JIPka.exe /win32icon:jipka.ico /resource:jipka.ico,jipka.ico ^
    /r:System.Windows.Forms.dll /r:System.Drawing.dll ^
    /r:build\Microsoft.Web.WebView2.Core.dll /r:build\Microsoft.Web.WebView2.WinForms.dll ^
    Program.cs || goto chyba

set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo Chybi Inno Setup 6: winget install JRSoftware.InnoSetup
    goto chyba
)
"%ISCC%" /Q JIPka.iss || goto chyba
echo Hotovo: %CD%\dist\JIPka-setup.exe
exit /b 0

:chyba
echo SESTAVENI SELHALO
pause
exit /b 1
