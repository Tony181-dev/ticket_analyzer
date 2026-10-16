; Inno Setup script for Ticket Analyzer — built by build_installer.bat
; Produces dist\TicketAnalyzer-Setup-<version>.exe with an uninstaller (Apps & features + Start menu).
; Per-user install: no admin rights needed.

#define AppName "Ticket Analyzer"
#define AppExe "TicketAnalyzer.exe"
#define AppPublisher "hoang6.nguyen"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F2C4E7A-3B1D-4C8E-9A57-1D2E8B4F0C93}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppCopyright=© {#AppPublisher}
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} Setup
DefaultDirName={localappdata}\Programs\TicketAnalyzer
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=TicketAnalyzer-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
english.LaunchApp=Launch Ticket Analyzer / Mở Ticket Analyzer
english.DeleteData=Also delete Ticket Analyzer data (run history, settings, Jira/Collab tokens)?%n%nXoá luôn dữ liệu của Ticket Analyzer (lịch sử chạy, cấu hình, token Jira/Collab)?%n%n%1%n%nDownloaded attachments, the Playwright browser cache and the M365 sign-in profile are kept.
english.UninstallShortcut=Uninstall Ticket Analyzer

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\TicketAnalyzer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "installed.flag"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Comment: "Jira → AI → Collab · {#AppPublisher}"
Name: "{autoprograms}\{cm:UninstallShortcut}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchApp}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM {#AppExe}"; Flags: runhidden; RunOnceId: "StopTicketAnalyzer"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
function DataDir(): String;
begin
  Result := ExpandConstant('{localappdata}\TicketAnalyzer');
end;

// Stop a running copy before files are replaced (upgrade / reinstall).
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and DirExists(DataDir()) and not UninstallSilent() then
    if MsgBox(FmtMessage(CustomMessage('DeleteData'), [DataDir()]), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(DataDir(), True, True, True);
end;
