#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{41B49C3F-CB46-4247-8C0D-7F487F904451}
AppName=AutoSkyPlay
AppVersion={#AppVersion}
DefaultDirName={autopf}\AutoSkyPlay
DefaultGroupName=AutoSkyPlay
OutputDir=..\dist
OutputBaseFilename=AutoSkyPlay-Setup
SetupIconFile=..\src\autoskyplay\assets\app.ico
LicenseFile=..\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
CloseApplications=yes
RestartApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\dist\AutoSkyPlay\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\AutoSkyPlay"; Filename: "{app}\AutoSkyPlay.exe"
Name: "{autodesktop}\AutoSkyPlay"; Filename: "{app}\AutoSkyPlay.exe"

[Run]
Filename: "{app}\AutoSkyPlay.exe"; Description: "Launch AutoSkyPlay"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if (CurStep = ssDone) and WizardSilent then
    Exec(ExpandConstant('{app}\AutoSkyPlay.exe'), '', '', SW_SHOWNORMAL, ewNoWait, ResultCode);
end;
