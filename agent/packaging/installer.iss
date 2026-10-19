; Installer Ronda Agent (Inno Setup 6). Dibangun lewat packaging\build.ps1 setelah PyInstaller.
;
;   Mode GUI (double-click):
;       Muncul form konfigurasi Server URL & Enrollment Key secara interaktif (tidak butuh terminal).
;
;   Mode Silent/CLI (deploy IT / GPO):
;       ronda-agent-setup-1.0.0.exe /VERYSILENT /SUPPRESSMSGBOXES /SERVER=https://ronda.example.local /KEY=IT-xxxxxxxx
;           [/CA=\\fileserver\it\ca-rs.pem] [/SCREENSHOT=0] [/ALLOWHTTP=1] [/LOG=C:\Windows\Temp\ronda-setup.log]
;
; Instalasi baru via silent wajib /SERVER dan /KEY. Instalasi ulang/upgrade: config.toml lama dipertahankan; parameter yang
; diberikan menimpa nilainya (mis. /KEY baru untuk PC yang ditolak, /SCREENSHOT=0 untuk mematikan screenshot; tanpa parameter = aktif).
;
; Hasil:
;   {commonpf64}\Ronda\ronda-launcher\        service RondaLauncher (auto, LocalSystem, restart 1 menit 3x)
;   C:\ProgramData\Ronda\versions\<versi>\    ronda-agent\ + ronda-helper\ (versi awal; berikutnya lewat auto-update)
;   THIRD_PARTY_LICENSES.txt di tiap folder program (dibuat build.ps1 lewat licenses.py)
;   C:\ProgramData\Ronda\config.toml          ACL folder: SYSTEM + Administrators; versions\ juga Users baca/eksekusi
; Instalasi baru (service belum terdaftar) dengan folder C:\ProgramData\Ronda yang sudah ada: folder itu dipindah ke
; C:\ProgramData\Ronda.karantina-<waktu> dan tidak dipakai (bisa dibuat user biasa, lihat SecureDataDir).
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
  FreshInstall: Boolean;   { service belum terdaftar: tidak ada instalasi Ronda yang sah di PC ini }
  ConfigPage: TInputQueryWizardPage;

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

function ServiceExists(): Boolean;
begin
  Result := Run(Sys('sc.exe'), 'query ' + SvcName) <> ErrServiceNotFound;
end;

function InitializeSetup(): Boolean;
var
  Url: String;
begin
  FreshInstall := not ServiceExists();
  ServerUrl := Trim(ExpandConstant('{param:SERVER|}'));
  EnrollKey := Trim(ExpandConstant('{param:KEY|}'));
  CaPath := Trim(ExpandConstant('{param:CA|}'));
  Screenshot := Trim(ExpandConstant('{param:SCREENSHOT|}'));
  Url := Lowercase(ServerUrl);
  Result := True;
  { "Baru" = service belum ada, BUKAN config.toml belum ada: config.toml bisa saja dibuat user biasa (lihat
    SecureDataDir) dan isinya tidak dipakai. Mode silent wajib sertakan parameter lengkap. }
  if WizardSilent and FreshInstall and ((ServerUrl = '') or (EnrollKey = '')) then
    Result := Fail('Instalasi silent baru butuh /SERVER=https://... dan /KEY=IT-...')
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

procedure InitializeWizard();
begin
  ConfigPage := CreateInputQueryPage(wpWelcome,
    'Konfigurasi Server Ronda',
    'Masukkan URL Server dan Kunci Pendaftaran (Enrollment Key)',
    'Data ini diperlukan agar Ronda Agent dapat terhubung ke server Ronda.');

  ConfigPage.Add('Server URL (misal: https://ronda.kantor.local atau http://192.168.1.10:8000):', False);
  ConfigPage.Add('Enrollment Key (kunci pendaftaran dari halaman web Ronda):', False);

  ConfigPage.Values[0] := ServerUrl;
  ConfigPage.Values[1] := EnrollKey;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if (ConfigPage <> nil) and (PageID = ConfigPage.ID) then
  begin
    { Lewati form jika upgrade atau jika parameter CLI sudah lengkap }
    if (not FreshInstall) or ((ServerUrl <> '') and (EnrollKey <> '')) then
      Result := True;
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Url: String;
begin
  Result := True;
  if (ConfigPage <> nil) and (CurPageID = ConfigPage.ID) then
  begin
    ServerUrl := Trim(ConfigPage.Values[0]);
    EnrollKey := Trim(ConfigPage.Values[1]);
    Url := Lowercase(ServerUrl);

    if (ServerUrl = '') or (EnrollKey = '') then
    begin
      MsgBox('Server URL dan Enrollment Key wajib diisi!', mbError, MB_OK);
      Result := False;
      Exit;
    end;

    if not IsSafe(ServerUrl) then
    begin
      MsgBox('Server URL berisi karakter tidak valid (tidak boleh mengandung spasi atau tanda kutip).', mbError, MB_OK);
      Result := False;
      Exit;
    end;

    if (Pos('https://', Url) <> 1) then
    begin
      if (Pos('http://', Url) = 1) then
      begin
        if MsgBox('Server menggunakan HTTP (tidak terenkripsi).' + #13#10 +
                  'Gunakan hanya untuk lingkungan pengujian/lab.' + #13#10#13#10 +
                  'Tetap lanjutkan dengan HTTP?', mbConfirmation, MB_YESNO) <> IDYES then
        begin
          Result := False;
          Exit;
        end;
      end
      else
      begin
        MsgBox('Server URL harus diawali dengan https:// atau http://', mbError, MB_OK);
        Result := False;
        Exit;
      end;
    end;

    if not IsSafe(EnrollKey) then
    begin
      MsgBox('Enrollment Key berisi karakter tidak valid.', mbError, MB_OK);
      Result := False;
      Exit;
    end;
  end;
end;

procedure StopAll();
begin
  Run(Sys('net.exe'), 'stop ' + SvcName);                 { launcher menghentikan agent + helper }
  Run(Sys('taskkill.exe'), '/F /IM ronda-agent.exe');
  Run(Sys('taskkill.exe'), '/F /IM ronda-helper.exe');
end;

{ Folder ACL SYSTEM + Administrators saja (S-1-5-18, S-1-5-32-544); isi folder mewarisi dari sini. }
procedure LockDown(const D: String);
begin
  Run(Sys('icacls.exe'), '"' + D + '" /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F');
end;

function DirIsEmpty(const D: String): Boolean;
var
  F: TFindRec;
begin
  Result := True;
  if FindFirst(D + '\*', F) then
  try
    repeat
      if (F.Name <> '.') and (F.Name <> '..') then
        Result := False;
    until (not Result) or (not FindNext(F));
  finally
    FindClose(F);
  end;
end;

{ C:\ProgramData bisa ditulisi user biasa. Folder Ronda yang SUDAH ADA sebelum instalasi baru mungkin dibuat user
  (mis. versions\99.0.0\ronda-agent\ronda-agent.exe + current), dan user itu tetap PEMILIK-nya walau ACL diganti:
  launcher (SYSTEM) akan menjalankan exe-nya. Folder seperti itu tidak dipakai dan isinya tidak disentuh (bisa memuat
  junction ke folder sistem; takeown/icacls rekursif bisa mengikutinya): hanya folder itu sendiri yang diambil alih
  lalu dipindah ke samping. Folder baru dibuat dan dikunci SEBELUM file disalin, lalu dipastikan masih kosong. }
function SecureDataDir(): String;
var
  D, Quarantine: String;
begin
  Result := '';
  D := DataDir();
  if FreshInstall and DirExists(D) then
  begin
    Run(Sys('takeown.exe'), '/F "' + D + '" /A');                     { folder itu saja, tidak rekursif }
    Run(Sys('icacls.exe'), '"' + D + '" /grant *S-1-5-32-544:F');
    Quarantine := D + '.karantina-' + GetDateTimeString('yyyymmddhhnnss', #0, #0);
    if not RenameFile(D, Quarantine) then
    begin
      Result := D + ' sudah ada sebelum instalasi dan tidak bisa dipindahkan. Periksa/hapus folder itu, lalu ulangi.';
      Exit;
    end;
    Log('PERINGATAN: ' + D + ' sudah ada sebelum instalasi baru; dipindah ke ' + Quarantine + ' (tidak dipakai)');
  end;
  if not DirExists(D) then
  begin
    if not CreateDir(D) then
    begin
      Result := 'Tidak bisa membuat ' + D;
      Exit;
    end;
    LockDown(D);
    if not DirIsEmpty(D) then      { ada yang menyelinap di antara CreateDir dan icacls }
    begin
      Result := 'Isi tak dikenal muncul di ' + D + ' saat instalasi. Instalasi dibatalkan.';
      Exit;
    end;
  end
  else
  begin
    Run(Sys('takeown.exe'), '/F "' + D + '" /A');                     { upgrade: pemilik folder akar dipastikan }
    LockDown(D);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopAll();
  Result := SecureDataDir();
end;

procedure SetAcl();
var
  D: String;
begin
  D := DataDir();
  LockDown(D);
  Run(Sys('icacls.exe'), '"' + D + '\*" /reset /T /C /Q');
  { Launcher hanya menjalankan file milik SYSTEM/Administrators (ronda_launcher\trust.py). Pemilik file buatan
    installer = pembuatnya, yang bisa berupa akun admin itu sendiri (kebijakan "Default owner"): seragamkan ke
    Administrators. Hanya pada instalasi baru: seluruh folder dibuat installer ini (SecureDataDir), jadi rekursi
    tidak mungkin melewati junction titipan. }
  if FreshInstall then
    Run(Sys('takeown.exe'), '/F "' + D + '" /A /R');
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
