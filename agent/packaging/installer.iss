; Installer Ronda Agent (Inno Setup 6). Dibangun lewat packaging\build.ps1 setelah PyInstaller.
;
;   ronda-agent-setup-1.0.0.exe /VERYSILENT /SUPPRESSMSGBOXES /SERVER=https://ronda.example.local /KEY=IT-xxxxxxxx
;       [/CA=\\fileserver\it\ca-rs.pem] [/SCREENSHOT=1] [/ALLOWHTTP=1] [/LOG=C:\Windows\Temp\ronda-setup.log]
;
; Instalasi baru wajib /SERVER dan /KEY. Instalasi ulang/upgrade: config.toml lama dipertahankan; parameter yang
; diberikan menimpa nilainya (mis. /KEY baru untuk PC yang ditolak, /SCREENSHOT=1 setelah kebijakan disetujui).
;
; Hasil:
;   {commonpf64}\Ronda\ronda-launcher\        service RondaLauncher (auto, LocalSystem, restart 1 menit 3x)
;   C:\ProgramData\Ronda\versions\<versi>\    ronda-agent\ + ronda-helper\ (versi awal; berikutnya lewat auto-update)
;   C:\ProgramData\Ronda\config.toml          ACL folder: SYSTEM + Administrators; versions\ juga Users baca/eksekusi
; local.token dibuat launcher saat pertama jalan. Helper dijalankan launcher di sesi user (tanpa scheduled task).

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{E7E98CD9-146C-4C9D-B7C1-479FCA71DB4B}
AppName=Ronda Agent
AppVersion={#AppVersion}
AppPublisher=Ronda
DefaultDirName={commonpf64}\Ronda
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=ronda-agent-setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
CloseApplications=no
UninstallDisplayName=Ronda Agent
SetupLogging=yes

[Files]
Source: "..\dist\ronda-launcher\*"; DestDir: "{app}\ronda-launcher"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\ronda-agent\*"; DestDir: "{commonappdata}\Ronda\versions\{#AppVersion}\ronda-agent"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\ronda-helper\*"; DestDir: "{commonappdata}\Ronda\versions\{#AppVersion}\ronda-helper"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{commonappdata}\Ronda"
Name: "{commonappdata}\Ronda\logs"

[UninstallDelete]
; logs\ sengaja ditinggal untuk penelusuran
Type: filesandordirs; Name: "{commonappdata}\Ronda\versions"
Type: files; Name: "{commonappdata}\Ronda\*"

[Code]
const
  SvcName = 'RondaLauncher';
  ErrServiceNotFound = 1060;

var
  ServerUrl, EnrollKey, CaPath, Screenshot: String;

function DataDir(): String;
begin
  Result := ExpandConstant('{commonappdata}\Ronda');
end;

function Run(const Exe, Params: String): Integer;
begin
  if not Exec(Exe, Params, '', SW_HIDE, ewWaitUntilTerminated, Result) then
    Result := -1;
  Log(Format('%s %s -> %d', [Exe, Params, Result]));
end;

function Sys(const Name: String): String;
begin
  Result := ExpandConstant('{sys}\' + Name);
end;

{ Nilai yang ditulis ke config.toml sebagai string TOML berkutip ganda: tolak karakter yang merusak sintaks. }
function IsSafe(const S: String): Boolean;
begin
  Result := (Pos('"', S) = 0) and (Pos('\', S) = 0) and (Pos(#10, S) = 0) and (Pos(#13, S) = 0)
    and (Pos(' ', S) = 0);
end;

function Fail(const Msg: String): Boolean;
begin
  Log('GAGAL: ' + Msg);
  SuppressibleMsgBox(Msg, mbCriticalError, MB_OK, IDOK);
  Result := False;
end;

function InitializeSetup(): Boolean;
var
  Url: String;
begin
  ServerUrl := Trim(ExpandConstant('{param:SERVER|}'));
  EnrollKey := Trim(ExpandConstant('{param:KEY|}'));
  CaPath := Trim(ExpandConstant('{param:CA|}'));
  Screenshot := Trim(ExpandConstant('{param:SCREENSHOT|}'));
  Url := Lowercase(ServerUrl);
  Result := True;
  if (not FileExists(DataDir() + '\config.toml')) and ((ServerUrl = '') or (EnrollKey = '')) then
    Result := Fail('Instalasi baru butuh /SERVER=https://... dan /KEY=IT-...')
  else if (ServerUrl <> '') and not IsSafe(ServerUrl) then
    Result := Fail('/SERVER berisi karakter tidak valid')
  else if (ServerUrl <> '') and (Pos('https://', Url) <> 1)
      and not ((Pos('http://', Url) = 1) and (ExpandConstant('{param:ALLOWHTTP|0}') = '1')) then
    Result := Fail('/SERVER harus diawali https:// (http:// hanya dengan /ALLOWHTTP=1 untuk uji)')
  else if (EnrollKey <> '') and not IsSafe(EnrollKey) then
    Result := Fail('/KEY berisi karakter tidak valid')
  else if (CaPath <> '') and not FileExists(CaPath) then
    Result := Fail('/CA: file ' + CaPath + ' tidak ada')
  else if (Screenshot <> '') and (Screenshot <> '0') and (Screenshot <> '1') then
    Result := Fail('/SCREENSHOT harus 0 atau 1');
end;

procedure StopAll();
begin
  Run(Sys('net.exe'), 'stop ' + SvcName);                 { launcher menghentikan agent + helper }
  Run(Sys('taskkill.exe'), '/F /IM ronda-agent.exe');
  Run(Sys('taskkill.exe'), '/F /IM ronda-helper.exe');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopAll();
  Result := '';
end;

procedure SetAcl();
var
  D: String;
begin
  D := DataDir();
  { SYSTEM + Administrators saja (S-1-5-18, S-1-5-32-544); isi folder mewarisi dari sini }
  Run(Sys('icacls.exe'), '"' + D + '" /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F');
  Run(Sys('icacls.exe'), '"' + D + '\*" /reset /T /C /Q');
  { helper berjalan sebagai user: butuh baca + eksekusi folder versi (S-1-5-32-545 = Users) }
  Run(Sys('icacls.exe'), '"' + D + '\versions" /grant *S-1-5-32-545:(OI)(CI)RX');
end;

procedure SetKey(Lines: TStringList; const Key, Value: String);
var
  I: Integer;
  Line: String;
begin
  for I := Lines.Count - 1 downto 0 do
  begin
    Line := TrimLeft(Lines[I]);
    if (Pos(Key + ' ', Line + ' ') = 1) or (Pos(Key + '=', Line) = 1) then
      Lines.Delete(I);
  end;
  Lines.Add(Key + ' = ' + Value);
end;

procedure WriteConfig();
var
  Path, Ca: String;
  Lines: TStringList;
  Raw: TArrayOfString;
  I: Integer;
begin
  Path := DataDir() + '\config.toml';
  Ca := DataDir() + '\ca-rs.pem';
  Lines := TStringList.Create;
  try
    if LoadStringsFromFile(Path, Raw) then
    begin
      for I := 0 to GetArrayLength(Raw) - 1 do
        Lines.Add(Raw[I]);
    end
    else
      Lines.Add('# Ditulis installer Ronda Agent. enrollment_key dihapus agent setelah terdaftar.');
    if ServerUrl <> '' then
      SetKey(Lines, 'server_url', '"' + ServerUrl + '"');
    if EnrollKey <> '' then
      SetKey(Lines, 'enrollment_key', '"' + EnrollKey + '"');
    if CaPath <> '' then
    begin
      if FileCopy(CaPath, Ca, False) then
        SetKey(Lines, 'ca_bundle', '''' + Ca + '''')     { string literal TOML: backslash apa adanya }
      else
        Log('GAGAL: CA tidak bisa disalin dari ' + CaPath);
    end;
    if Screenshot = '1' then
      SetKey(Lines, 'screenshot', 'true')
    else if Screenshot = '0' then
      SetKey(Lines, 'screenshot', 'false');
    SetArrayLength(Raw, Lines.Count);
    for I := 0 to Lines.Count - 1 do
      Raw[I] := Lines[I];
    if not SaveStringsToUTF8File(Path, Raw, False) then
      Log('GAGAL: tidak bisa menulis ' + Path);
  finally
    Lines.Free;
  end;
end;

{ -1 bila A < B, 0 bila sama, 1 bila A > B; format "x.y.z". }
function CompareVersion(A, B: String): Integer;
var
  PA, PB, NA, NB: Integer;
begin
  Result := 0;
  while (Result = 0) and ((A <> '') or (B <> '')) do
  begin
    PA := Pos('.', A + '.');
    PB := Pos('.', B + '.');
    NA := StrToIntDef(Copy(A, 1, PA - 1), 0);
    NB := StrToIntDef(Copy(B, 1, PB - 1), 0);
    A := Copy(A, PA + 1, Length(A));
    B := Copy(B, PB + 1, Length(B));
    if NA < NB then
      Result := -1
    else if NA > NB then
      Result := 1;
  end;
end;

{ Sudah ada versi aktif (instalasi ulang/upgrade): minta launcher pindah ke versi installer ini, lewat masa uji yang
  sama dengan auto-update (rollback bila gagal). Versi installer yang lebih lama tidak menurunkan versi aktif. }
procedure ScheduleSwitch();
var
  Current: AnsiString;
begin
  if not LoadStringFromFile(DataDir() + '\current', Current) then
    Exit;                      { instalasi baru: launcher memakai satu-satunya versi di versions\ }
  if CompareVersion('{#AppVersion}', Trim(String(Current))) > 0 then
    SaveStringToFile(DataDir() + '\pending_switch', '{#AppVersion}', False)
  else
    Log('versi aktif ' + Trim(String(Current)) + ' tidak lebih lama dari {#AppVersion}; tidak dipindah');
end;

procedure InstallService();
var
  Bin: String;
begin
  Bin := 'binPath= "\"' + ExpandConstant('{app}\ronda-launcher\ronda-launcher.exe') + '\"" start= auto';
  if Run(Sys('sc.exe'), 'query ' + SvcName) = ErrServiceNotFound then
    Run(Sys('sc.exe'), 'create ' + SvcName + ' ' + Bin + ' DisplayName= "Ronda Launcher"')
  else
    Run(Sys('sc.exe'), 'config ' + SvcName + ' ' + Bin);
  Run(Sys('sc.exe'), 'description ' + SvcName + ' "Menjalankan dan memperbarui agent monitoring Ronda (Instalasi IT)."');
  Run(Sys('sc.exe'), 'failure ' + SvcName + ' reset= 86400 actions= restart/60000/restart/60000/restart/60000');
  if Run(Sys('net.exe'), 'start ' + SvcName) <> 0 then
    Log('PERINGATAN: service ' + SvcName + ' tidak bisa dijalankan; cek logs\launcher.log');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    SetAcl();
    WriteConfig();
    ScheduleSwitch();
    InstallService();
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    StopAll();
    Run(Sys('sc.exe'), 'delete ' + SvcName);
  end;
end;
