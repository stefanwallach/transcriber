; Inno Setup script: builds dist\Transcriber-Setup-<version>.exe from the PyInstaller output.
#define AppName "Transcriber"
#ifndef AppVersion
  #define AppVersion "0.3.0-beta"
#endif
#ifndef NumVersion
  #define NumVersion "0.3.0"
#endif

[Setup]
AppId={{6E1B3C52-9A4F-4C1E-B7D2-5F3A8E0C9D41}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
VersionInfoVersion={#NumVersion}
CloseApplications=yes
AppPublisher=Transcriber
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Installs for the current user without needing an administrator (they can choose all users).
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=dist
OutputBaseFilename=Transcriber-Setup-{#AppVersion}
SetupIconFile=build-assets\icon.ico
UninstallDisplayIcon={app}\Transcriber.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "dist\Transcriber\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\Transcriber.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\Transcriber.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Transcriber.exe"; Description: "Open Transcriber now"; Flags: nowait postinstall skipifsilent
; After a silent update from inside the app, reopen it.
Filename: "{app}\Transcriber.exe"; Flags: nowait; Check: WizardSilent
