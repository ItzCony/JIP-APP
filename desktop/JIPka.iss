; Instalátor desktopové aplikace Moje JIPka (Inno Setup 6) – sestavuje build.cmd.
; Instaluje se jen pro aktuálního uživatele, bez práv správce (%LOCALAPPDATA%\Programs\JIPka).
#define Verze "1.0.0"

[Setup]
AppId={{CAD25475-B83B-4EBC-9623-DC28C428093C}
AppName=Moje JIPka
AppVersion={#Verze}
AppPublisher=JIP
UninstallDisplayName=Moje JIPka
UninstallDisplayIcon={app}\JIPka.exe
DefaultDirName={autopf}\JIPka
PrivilegesRequired=lowest
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
SetupIconFile=jipka.ico
OutputDir=dist
OutputBaseFilename=JIPka-setup
Compression=lzma2/max
SolidCompression=yes

[Languages]
Name: "cs"; MessagesFile: "compiler:Languages\Czech.isl"

[Tasks]
Name: "plocha"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "build\JIPka.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "build\Microsoft.Web.WebView2.Core.dll"; DestDir: "{app}"; Flags: ignoreversion
Source: "build\Microsoft.Web.WebView2.WinForms.dll"; DestDir: "{app}"; Flags: ignoreversion
Source: "build\WebView2Loader.dll"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Moje JIPka"; Filename: "{app}\JIPka.exe"
Name: "{autodesktop}\Moje JIPka"; Filename: "{app}\JIPka.exe"; Tasks: plocha

[Run]
Filename: "{app}\JIPka.exe"; Description: "{cm:LaunchProgram,Moje JIPka}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; profil aplikace (přihlášení, cache WebView2) – viz Program.cs
Type: filesandordirs; Name: "{localappdata}\JIPka"
