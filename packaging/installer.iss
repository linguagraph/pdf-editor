; Inno Setup 6 script for an optional installer around the portable pdfeditor.exe.
; scripts/build_exe.py --installer fills in the @...@ values and runs ISCC.
; The portable exe stays the primary deliverable: the installer only copies it, adds Start
; menu entries, optionally registers .pdf, and uninstalls cleanly.

#define AppName "pdfeditor"
#define AppVersion "@VERSION@"
#define AppExe "pdfeditor.exe"

[Setup]
AppId={{5E7B7D0A-6B4E-4B8B-9F1C-2B8E3C1D4A77}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=pdfeditor
AppPublisherURL=https://github.com/linguagraph/pdf-editor
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=@LICENSE@
OutputDir=@OUTDIR@
OutputBaseFilename={#AppName}-{#AppVersion}-setup
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
Name: "pdfassoc"; Description: "Open PDF files with {#AppName}"; GroupDescription: "File associations:"; Flags: unchecked

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

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
