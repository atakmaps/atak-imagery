; End-user Windows installer for pre-built ATAK Pipeline EXEs (Inno Setup 6).
; Maintainer: build EXEs first, then compile this script.
;
;   powershell -ExecutionPolicy Bypass -File windows_build\build_windows_exe.ps1
;   powershell -ExecutionPolicy Bypass -File windows_build\build_windows_installer.ps1
;
#ifndef MyAppVersion
#define MyAppVersion "1.3.47"
#endif

#define MyAppName "ATAK Pipeline"
#define MyAppPublisher "ATAK Maps"
#define MyInstallerExe "ATAKDeviceInstaller.exe"
#define MyImageryExe "ATAKImageryDownloader.exe"

[Setup]
AppId={{A7B3C9E1-4F2D-4A8B-9C1E-ATAKPIPELINE01}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\ATAK Pipeline
DefaultGroupName=ATAK Pipeline
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
DisableWelcomePage=yes
WizardStyle=modern
PrivilegesRequired=lowest
OutputDir=installer-dist
OutputBaseFilename=ATAKSetup-v{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
CloseApplications=force
SetupLogging=yes
UninstallDisplayIcon={app}\{#MyInstallerExe}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "dist\{#MyInstallerExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\{#MyImageryExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\tools\platform-tools\*"; DestDir: "{app}\tools\platform-tools"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
Source: "dist\deploy.env.example"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "dist\deploy.env.example"; DestDir: "{app}"; DestName: "deploy.env"; Flags: onlyifdoesntexist skipifsourcedoesntexist
Source: "deploy.env.example"; DestDir: "{app}"; DestName: "deploy.env"; Flags: onlyifdoesntexist skipifsourcedoesntexist
Source: "dist\VERSION"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "VERSION"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\ATAK Device Installer"; Filename: "{app}\{#MyInstallerExe}"; WorkingDir: "{app}"; Comment: "Install ATAK and plugins on your phone over USB"
Name: "{group}\ATAK Imagery Downloader"; Filename: "{app}\{#MyImageryExe}"; WorkingDir: "{app}"; Comment: "Download USGS imagery and build ATAK map packages"
Name: "{autodesktop}\ATAK Device Installer"; Filename: "{app}\{#MyInstallerExe}"; WorkingDir: "{app}"; Comment: "Install ATAK and plugins on your phone over USB"
Name: "{autodesktop}\ATAK Imagery Downloader"; Filename: "{app}\{#MyImageryExe}"; WorkingDir: "{app}"; Comment: "Download USGS imagery and build ATAK map packages"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

[Run]

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\atak-pipeline\installer_logs"

[Messages]
FinishedLabel=Install complete.%n%nPlease run the ATAK Device Installer first (if necessary), then run the ATAK Imagery Downloader.%n%nDesktop shortcuts were created for both programs.
ClickFinish=Finish

[Code]
procedure SetStatus(const Msg: String);
begin
  if WizardForm.StatusLabel <> nil then
    WizardForm.StatusLabel.Caption := Msg;
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
end;

procedure InitializeWizard();
begin
  WizardForm.Caption := '{#MyAppName} Setup';
  WizardForm.WelcomeLabel1.Caption := '{#MyAppName}';
  WizardForm.WelcomeLabel2.Caption := 'Installing ATAK Device Installer and ATAK Imagery Downloader.';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  case CurStep of
    ssInstall:
      SetStatus('Copying program files and Android platform-tools...');
    ssPostInstall:
      SetStatus('Creating desktop and Start Menu shortcuts...');
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then
  begin
    WizardForm.FinishedLabel.Caption :=
      'Install complete.' + #13#10 + #13#10 +
      'Please run the ATAK Device Installer first (if necessary), ' +
      'then run the ATAK Imagery Downloader.' + #13#10 + #13#10 +
      'Desktop shortcuts were created for both programs.';
  end;
end;
