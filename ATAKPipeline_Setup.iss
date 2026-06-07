#define MyAppName "ATAK Imagery Downloader"
#define MyAppVersion "0.2.5"
#define MyAppExeName "ATAKPipeline.exe"

[Setup]
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=installer-dist
OutputBaseFilename=ATAKPipelineSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\ATAKPipeline.exe"; WorkingDir: "{app}"
Name: "{userprograms}\{#MyAppName}"; Filename: "{app}\ATAKPipeline.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\ATAKPipeline.exe"; WorkingDir: "{app}"

[Run]
; Intentionally empty — installer must not launch any program after setup.
