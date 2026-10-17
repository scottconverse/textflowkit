; Only a coordinator-prepared, validated payload is packaged. No downloads.
#ifndef PayloadDir
  #error PayloadDir is required
#endif
#ifndef AppVersion
  #error AppVersion is required
#endif

[Setup]
AppId={{8D526ED5-5F8F-4809-A1AE-70C13F94B4DA}
AppName=TextFlowKit
AppVersion={#AppVersion}
AppPublisher=TextFlowKit
DefaultDirName={localappdata}\Programs\TextFlowKit
DefaultGroupName=TextFlowKit
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0
AppMutex=Local\TextFlowKit.Desktop
SetupMutex=Local\TextFlowKit.Setup
CloseApplications=no
RestartApplications=no
OutputBaseFilename=TextFlowKit-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=TextFlowKit
UninstallDisplayIcon={app}\runtime\pythonw.exe

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "{#PayloadDir}\runtime\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PayloadDir}\bin\*"; DestDir: "{app}\bin"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PayloadDir}\models\*"; DestDir: "{app}\models"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PayloadDir}\licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PayloadDir}\launch.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\manifest.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\TextFlowKit"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\launch.py"""; WorkingDir: "{app}"
Name: "{userdesktop}\TextFlowKit"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\launch.py"""; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\launch.py"" --after-install"; WorkingDir: "{app}"; Description: "Open TextFlowKit"; Flags: nowait postinstall skipifsilent

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if CheckForMutexes('Local\TextFlowKit.Desktop') then
    Result := 'TextFlowKit is running. Stop jobs and close TextFlowKit before installing.';
end;

function InitializeUninstall(): Boolean;
begin
  if CheckForMutexes('Local\TextFlowKit.Setup') then
  begin
    MsgBox('TextFlowKit setup is already running. Close the installer or uninstaller and try again.', mbError, MB_OK);
    Result := False;
    Exit;
  end;
  CreateMutex('Local\TextFlowKit.Setup');
  Result := not CheckForMutexes('Local\TextFlowKit.Desktop');
  if not Result then
    MsgBox('TextFlowKit is running. Stop jobs and close TextFlowKit before uninstalling.', mbError, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usUninstall) and CheckForMutexes('Local\TextFlowKit.Desktop') then
    RaiseException('TextFlowKit started during uninstall. Close it and try again.');
end;
