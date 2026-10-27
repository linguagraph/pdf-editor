; Inno Setup 6 script for an optional installer around the portable pdfeditor.exe.
; scripts/build_exe.py --installer fills in the @...@ values and runs ISCC.
; The portable exe stays the primary deliverable: the installer only copies it, adds Start
; menu entries, optionally registers .pdf, and uninstalls cleanly.

#define AppName "Elanero - PDF Editor"
; Identifiers stay "pdfeditor" (install folder, ProgID, setup file name) so upgrades keep working.
#define AppId "pdfeditor"
#define AppVersion "@VERSION@"
#define AppExe "pdfeditor.exe"

[Setup]
AppId={{5E7B7D0A-6B4E-4B8B-9F1C-2B8E3C1D4A77}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Elanero
AppPublisherURL=https://github.com/linguagraph/pdf-editor
DefaultDirName={autopf}\{#AppId}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=@LICENSE@
OutputDir=@OUTDIR@
OutputBaseFilename={#AppId}-{#AppVersion}-setup
SetupIconFile=@ICON@
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExe}
ChangesAssociations=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "pdfassoc"; Description: "Open PDF files with Elanero"; GroupDescription: "File associations:"; Flags: unchecked

[Files]
Source: "@EXE@"; DestDir: "{app}"; DestName: "{#AppExe}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; Offered in "Open with" always; made the default handler only with the pdfassoc task.
Root: HKA; Subkey: "Software\Classes\pdfeditor.pdf"; ValueType: string; ValueName: ""; ValueData: "PDF document"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\pdfeditor.pdf\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExe},0"
Root: HKA; Subkey: "Software\Classes\pdfeditor.pdf\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""
Root: HKA; Subkey: "Software\Classes\.pdf\OpenWithProgids"; ValueType: string; ValueName: "pdfeditor.pdf"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.pdf"; ValueType: string; ValueName: ""; ValueData: "pdfeditor.pdf"; Tasks: pdfassoc; Flags: uninsdeletevalue
; The same keys as Preferences > General > PDF files (services/file_association.py).
Root: HKA; Subkey: "Software\Classes\pdfeditor.pdf\Application"; ValueType: string; ValueName: "ApplicationName"; ValueData: "{#AppName}"
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "{#AppName}"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExe},0"
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".pdf"; ValueData: ""
Root: HKA; Subkey: "Software\pdfeditor\Capabilities"; ValueType: string; ValueName: "ApplicationName"; ValueData: "{#AppName}"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\pdfeditor\Capabilities"; ValueType: string; ValueName: "ApplicationDescription"; ValueData: "View, edit, comment on, organize and convert PDF documents."
Root: HKA; Subkey: "Software\pdfeditor\Capabilities"; ValueType: string; ValueName: "ApplicationIcon"; ValueData: "{app}\{#AppExe},0"
Root: HKA; Subkey: "Software\pdfeditor\Capabilities\FileAssociations"; ValueType: string; ValueName: ".pdf"; ValueData: "pdfeditor.pdf"
Root: HKA; Subkey: "Software\RegisteredApplications"; ValueType: string; ValueName: "{#AppName}"; ValueData: "Software\pdfeditor\Capabilities"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
