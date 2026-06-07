#ifndef MyAppVersion
#define MyAppVersion "1.3.37"
#endif

#define MyAppName "ATAK Pipeline"
#define MyInstallerExe "ATAKDeviceInstaller.exe"
#define MyImageryExe "ATAKImageryDownloader.exe"

[Setup]
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\ATAK Pipeline
DefaultGroupName=ATAK Pipeline
OutputDir=installer-dist
OutputBaseFilename=ATAKSetup-v{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest

[Files]
Source: "dist\{#MyInstallerExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\{#MyImageryExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\tools\*"; DestDir: "{app}\tools"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "dist\deploy.env*"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "dist\VERSION"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "deploy.env.example"; DestDir: "{app}"; DestName: "deploy.env"; Flags: onlyifdoesntexist skipifsourcedoesntexist

[Icons]
Name: "{group}\ATAK Device Installer"; Filename: "{app}\{#MyInstallerExe}"; WorkingDir: "{app}"
Name: "{group}\ATAK Imagery Downloader"; Filename: "{app}\{#MyImageryExe}"; WorkingDir: "{app}"
Name: "{userdesktop}\ATAK Device Installer"; Filename: "{app}\{#MyInstallerExe}"; WorkingDir: "{app}"
Name: "{userdesktop}\ATAK Imagery Downloader"; Filename: "{app}\{#MyImageryExe}"; WorkingDir: "{app}"

[Run]

[Messages]
WelcomeLabel2=Installs ATAK Device Installer and ATAK Imagery Downloader to your Programs folder.%n%nUse Device Installer first to set up your phone over USB, then Imagery Downloader to download maps.
