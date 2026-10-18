<#
ZISU one-command release workflow. Dry run is the default and is always offline.
Real GitHub writes and installation require both -ExecuteRelease and -Install.
No Google, YouTube or Gemini API is called by this script.
#>
[CmdletBinding()]
param(
    [string]$RepoPath = (Split-Path -Parent $PSScriptRoot),
    [string]$ExpectedBranch = 'feature/v4.5-ai-research-manager',
    [string]$CommitMessage = 'chore: prepare verified ZISU release',
    [string]$PackageZip,
    [string]$ZipManifestPath,
    [string]$ArtifactName = 'ZisuResearchStudio-Windows',
    [string]$InstallDirectory = 'D:\03 zisu\ZISU_APP',
    [string]$DatabasePath = (Join-Path $env:LOCALAPPDATA 'ZisuResearchStudio\zisu_v2.db'),
    [string]$ResumeRunId,
    [ValidateRange(1,240)][int]$TimeoutMinutes = 90,
    [switch]$ExecuteRelease,
    [switch]$Install
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

function Invoke-Checked([string]$File,[string[]]$Arguments,[string]$Cwd) {
    Push-Location $Cwd
    try { & $File @Arguments; if ($LASTEXITCODE -ne 0) { throw "Command failed: $File (exit $LASTEXITCODE)." } }
    finally { Pop-Location }
}
function Get-Text([string]$File,[string[]]$Arguments,[string]$Cwd) {
    Push-Location $Cwd
    # PowerShell can promote native stderr (for example Git's harmless
    # permission warning while traversing an ignored temp directory) to a
    # terminating NativeCommandError even when the process exits 0. Preserve
    # the exit-code contract and never surface stderr text.
    try {
        try { $v=& $File @Arguments 2>$null }
        catch { if ($LASTEXITCODE -ne 0) { return $null } }
        if ($LASTEXITCODE -ne 0) { return $null }
        return ($v | Out-String).Trim()
    }
    finally { Pop-Location }
}
function Save-Checkpoint {
    $tmp="$script:CheckpointPath.tmp"
    [IO.File]::WriteAllText($tmp,($script:Checkpoint | ConvertTo-Json -Depth 8),[Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $tmp -Destination $script:CheckpointPath -Force
}
function Complete-Step([string]$Name) {
    if (@($script:Checkpoint.completed_steps) -notcontains $Name) { $script:Checkpoint.completed_steps=@($script:Checkpoint.completed_steps)+@($Name) }
    $script:Checkpoint.updated_at_utc=[DateTime]::UtcNow.ToString('o'); Save-Checkpoint
}
function Check-SafePaths([string]$Root,[switch]$CachedOnly) {
    $tracked=@((Get-Text git @('ls-files') $Root) -split "`r?`n")
    $untracked=@((Get-Text git @('ls-files','--others','--exclude-standard') $Root) -split "`r?`n")
    $changedArgs=if($CachedOnly){@('diff','--cached','--name-only')}else{@('diff','--name-only','HEAD')}
    $changed=@((Get-Text git $changedArgs $Root) -split "`r?`n")
    $paths=@($tracked+$untracked+$changed | Where-Object { $_ } | Select-Object -Unique)
    $deny='(?i)(^|/)(client_secret[^/]*\.json|client_secrets[^/]*\.json|credentials?\.json|authorized[_-]?user\.json|oauth_tokens?)(/|$)|(^|/)[^/]*\.(db|sqlite|sqlite3|db-wal|db-shm|log|bak|backup)$|(^|/)(\.env($|\.)|_local_backups|\.test_tmp)(/|$)|(^|/)token[^/]*\.json$'
    if (@($paths | Where-Object { $_ -match $deny }).Count) { throw 'Protected database, credential, log, backup or local-test path is in Git scope.' }
    # Only inspect contents that can enter this change. Tracked-path names are
    # still checked for forbidden data, but opening every historical source
    # file is unnecessary and can fail on legacy long/invalid Windows paths.
    $contentPaths=@($untracked+$changed | Where-Object { $_ } | Select-Object -Unique)
    foreach($rel in $contentPaths) {
        $p=Join-Path $Root $rel
        if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { continue }
        $f=Get-Item -LiteralPath $p
        if ($f.Length -gt 20MB) { throw 'Unexpected large file in Git scope.' }
        $txt=[IO.File]::ReadAllText($p)
        if ($txt -match '(?i)AIza[0-9A-Za-z_-]{30,}|ya29\.[0-9A-Za-z._-]{20,}|"client_secret"\s*:\s*"[^"\r\n]{8,}|"refresh_token"\s*:\s*"[^"\r\n]{8,}|-----BEGIN (RSA |EC )?PRIVATE KEY-----') {
            throw 'Credential-like content detected; content was not displayed.'
        }
    }
}
function Get-ProxyFacts {
    $out=@()
    foreach($n in @('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy')) {
        $v=[Environment]::GetEnvironmentVariable($n,'Process'); if(-not $v){continue}
        try { $u=[Uri]$v; $out += [ordered]@{source="env:$n";host=$u.Host;port=$u.Port;loopback_port_9=(($u.Host -in @('127.0.0.1','localhost','::1')) -and $u.Port -eq 9)} }
        catch { $out += [ordered]@{source="env:$n";host='unparsed';port=0;loopback_port_9=$false} }
    }
    $v=Get-Text git @('config','--get','http.proxy') $script:RepoRoot
    if($v){try{$u=[Uri]$v;$out += [ordered]@{source='git:http.proxy';host=$u.Host;port=$u.Port;loopback_port_9=(($u.Host -in @('127.0.0.1','localhost','::1')) -and $u.Port -eq 9)}}catch{$out += [ordered]@{source='git:http.proxy';host='configured';port=0;loopback_port_9=$false}}}
    return ,$out
}
function Invoke-GH([string]$File,[string[]]$Arguments,[switch]$Capture) {
    $names=@('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy');$saved=@{}
    $badProxy=@($script:ProxyFacts | Where-Object {$_.loopback_port_9}).Count -gt 0
    try {
        if($badProxy){foreach($n in $names){$saved[$n]=[Environment]::GetEnvironmentVariable($n,'Process');[Environment]::SetEnvironmentVariable($n,$null,'Process')};$script:ProxyOverrideApplied=$true}
        $actualArgs=@($Arguments)
        if($badProxy -and $File -eq 'git'){$actualArgs=@('-c','http.proxy=')+$actualArgs}
        Push-Location $script:RepoRoot
        try {if($Capture){$v=& $File @actualArgs 2>$null;if($LASTEXITCODE -ne 0){throw "GitHub command failed: $File (exit $LASTEXITCODE)."};return $v};& $File @actualArgs;if($LASTEXITCODE -ne 0){throw "GitHub command failed: $File (exit $LASTEXITCODE)."}}
        finally {Pop-Location}
    } finally {foreach($n in $saved.Keys){[Environment]::SetEnvironmentVariable($n,$saved[$n],'Process')}}
}
function Get-PackageFacts([string]$Zip,[string]$Manifest) {
    if(!(Test-Path -LiteralPath $Zip -PathType Leaf) -or !(Test-Path -LiteralPath $Manifest -PathType Leaf)){throw 'Release ZIP or same-build SHA-256 manifest is missing.'}
    $zh=(Get-FileHash -Algorithm SHA256 -LiteralPath $Zip).Hash.ToLowerInvariant();$zn=[IO.Path]::GetFileName($Zip)
    $line=Get-Content -LiteralPath $Manifest -Encoding ascii | Where-Object {$_ -match ('\s'+[regex]::Escape($zn)+'$')} | Select-Object -First 1
    if($line -notmatch '^([0-9a-fA-F]{64})\s+' -or $Matches[1].ToLowerInvariant() -ne $zh){throw 'ZIP SHA-256 does not match its same-build manifest.'}
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $a=[IO.Compression.ZipFile]::OpenRead($Zip)
    try {
        $e=@($a.Entries);$names=@($e | ForEach-Object {$_.FullName.Replace('\','/')})
        foreach($n in $names){
            if($n.StartsWith('/') -or $n -match '(^|/)\.\.?(/|$)' -or $n -match '^[A-Za-z]:'){throw 'ZIP path traversal detected.'}
            if($n -match '(?i)client_secret|oauth_tokens?|token[^/]*\.json|credentials?\.json|\.db($|[.-])|\.sqlite|\.log$|backup|\.env|\.py$'){throw 'ZIP contains a forbidden sensitive or source file.'}
        }
        $x=$e | Where-Object {$_.FullName -eq 'ZisuResearchStudio.exe'} | Select-Object -First 1
        $m=$e | Where-Object {$_.FullName -eq 'SHA256SUMS.txt'} | Select-Object -First 1
        if(!$x -or !$m -or !($names | Where-Object {$_ -like '_internal/*'}) -or 'Quick-Start-KO.txt' -notin $names -or 'User-Guide-KO.html' -notin $names){throw 'ZIP is missing EXE, _internal or required beginner guides.'}
        $r=[IO.StreamReader]::new($m.Open());try{$text=$r.ReadToEnd()}finally{$r.Dispose()}
        if($text -notmatch '(?im)^([0-9a-f]{64})\s+ZisuResearchStudio\.exe\s*$'){throw 'EXE checksum manifest is missing or malformed.'};$expected=$Matches[1]
        $s=$x.Open();$mem=New-Object IO.MemoryStream;try{$s.CopyTo($mem)}finally{$s.Dispose()}
        $sha=[Security.Cryptography.SHA256]::Create();try{$actual=([BitConverter]::ToString($sha.ComputeHash($mem.ToArray())) -replace '-','').ToLowerInvariant()}finally{$sha.Dispose();$mem.Dispose()}
        if($actual -ne $expected){throw 'EXE hash does not match ZIP internal manifest.'}
        return [ordered]@{zip_path=$Zip;zip_sha256=$zh;exe_sha256=$actual;entry_count=$e.Count;runtime_entry_count=@($names|Where-Object {$_ -like '_internal/*'}).Count}
    } finally {$a.Dispose()}
}
function Test-NoZisuProcess {
    if(@(Get-Process -Name ZisuResearchStudio -ErrorAction SilentlyContinue).Count){throw 'ZISU is running; close it normally before replacement.'}
}
function Backup-Database([string]$Db,[string]$Snapshot,[string]$Python) {
    if(!(Test-Path -LiteralPath $Db -PathType Leaf)){throw 'User database is missing; installation is blocked.'}
    $helper=Join-Path $script:RunRoot 'sqlite_backup_api.py'
    $code=@'
import sqlite3,sys,json,hashlib,base64,pathlib
src=pathlib.Path(sys.argv[1]).resolve();dst=pathlib.Path(sys.argv[2]).resolve()
def openro(p):
 u='file:'+p.as_posix()+'?mode=ro'
 try:return sqlite3.connect(u,uri=True)
 except sqlite3.Error:
  if pathlib.Path(str(p)+'-wal').exists() or pathlib.Path(str(p)+'-shm').exists():raise
  return sqlite3.connect(u+'&immutable=1',uri=True)
def check(c):
 if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('SQLite integrity check failed')
 tabs=[x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")];counts={};h=hashlib.sha256()
 for t in tabs:
  q='"'+t.replace('"','""')+'"';rows=c.execute('SELECT * FROM '+q).fetchall();counts[t]=len(rows)
  for row in rows:
   vals=[{'blob':base64.b64encode(v).decode('ascii')} if isinstance(v,bytes) else v for v in row];h.update(json.dumps(vals,ensure_ascii=False,separators=(',',':'),sort_keys=True).encode()+b'\n')
 return counts,h.hexdigest()
s=openro(src);before=check(s);out=sqlite3.connect(str(dst));s.backup(out);out.commit();out.close();s.close();r=openro(dst);after=check(r);r.close()
if before!=after:raise SystemExit('Backup differs logically from source')
print(json.dumps({'integrity':'ok','table_counts':before[0],'logical_digest':before[1],'logical_match':True},sort_keys=True))
'@
    [IO.File]::WriteAllText($helper,$code,[Text.UTF8Encoding]::new($false))
    $v=& $Python $helper $Db $Snapshot;if($LASTEXITCODE -ne 0){throw 'SQLite Backup API/restore-compare verification failed.'}
    return (($v|Out-String).Trim()|ConvertFrom-Json)
}

if($Install -and -not $ExecuteRelease){throw 'Real install requires explicit -ExecuteRelease and -Install.'}
$script:RepoRoot=[IO.Path]::GetFullPath($RepoPath)
if(!(Test-Path -LiteralPath (Join-Path $script:RepoRoot '.git'))){throw 'RepoPath is not the expected Git work tree.'}
$branch=(Get-Text git @('branch','--show-current') $script:RepoRoot);$head=(Get-Text git @('rev-parse','HEAD') $script:RepoRoot)
if($branch -ne $ExpectedBranch){throw "Wrong branch: expected $ExpectedBranch. No change was made."}
$runBase=Join-Path $script:RepoRoot '_local_backups\ZisuReleaseRuns';New-Item -ItemType Directory -Force -Path $runBase|Out-Null
if($ResumeRunId){
    if($ResumeRunId -notmatch '^\d{8}_\d{6}_[a-f0-9]{8}$'){throw 'Invalid ResumeRunId.'}
    $script:RunRoot=Join-Path $runBase $ResumeRunId;$script:CheckpointPath=Join-Path $script:RunRoot 'checkpoint.json'
    if(!(Test-Path -LiteralPath $script:CheckpointPath)){throw 'Resume checkpoint is missing.'}
    $script:Checkpoint=Get-Content -LiteralPath $script:CheckpointPath -Raw|ConvertFrom-Json
    if($script:Checkpoint.branch -ne $branch){throw 'Checkpoint branch does not match current branch.'}
    if($script:Checkpoint.commit_sha -ne $head){
        & git -C $script:RepoRoot merge-base --is-ancestor $script:Checkpoint.commit_sha $head
        if($LASTEXITCODE -ne 0){throw 'Checkpoint SHA is not an ancestor of HEAD; refusing unsafe resume.'}
        $script:Checkpoint.commit_sha=$head;Save-Checkpoint
    }
}else{
    $id=(Get-Date -Format 'yyyyMMdd_HHmmss')+'_'+[Guid]::NewGuid().ToString('N').Substring(0,8);$script:RunRoot=Join-Path $runBase $id
    New-Item -ItemType Directory -Path $script:RunRoot|Out-Null;$script:CheckpointPath=Join-Path $script:RunRoot 'checkpoint.json'
    $script:Checkpoint=[ordered]@{schema=1;run_id=$id;branch=$branch;commit_sha=$head;completed_steps=@();created_at_utc=[DateTime]::UtcNow.ToString('o');updated_at_utc=[DateTime]::UtcNow.ToString('o')};Save-Checkpoint
}
$script:ProxyFacts=@(Get-ProxyFacts);$script:ProxyOverrideApplied=$false
$report=[ordered]@{schema=1;app='ZISU';mode=$(if($ExecuteRelease){'execute'}else{'dry_run'});run_id=$script:Checkpoint.run_id;branch=$branch;commit_sha=$head;steps=@();proxy=$script:ProxyFacts;external_api_calls=0;sensitive_values_logged=$false;result='running'}
function Report-Step([string]$Name,[string]$Status,[string]$Detail=''){$report.steps += [ordered]@{name=$Name;status=$Status;detail=$Detail};Complete-Step $Name}

try {
    $script:FailureStep='sensitive_path_scan'
    Check-SafePaths $script:RepoRoot
    Report-Step 'branch_and_secret_file_gate' 'PASS' 'Branch matches; protected paths/content absent.'
    if(@($script:ProxyFacts|Where-Object {$_.loopback_port_9}).Count){$report.proxy_handling='Loopback port 9 detected; no global config changed. Only GitHub child-command environment is scoped.'}
    $script:FailureStep='compile_tests_gui_diff_check'
    Invoke-Checked python @('-m','compileall','-q','main.py','mcrs','tests') $script:RepoRoot
    Invoke-Checked python @('-m','unittest','discover','-s','tests','-p','test_*.py','-v') $script:RepoRoot
    Invoke-Checked python @('tests/smoke_gui.py') $script:RepoRoot
    Invoke-Checked git @('diff','--check') $script:RepoRoot
    Report-Step 'compile_tests_gui_diff_check' 'PASS' 'Compile, unit suite, GUI smoke and diff check completed.'

    if($PackageZip){
        $script:FailureStep='package_hash_validation'
        if(!$ZipManifestPath){$ZipManifestPath=Join-Path (Split-Path -Parent $PackageZip) 'RELEASE_SHA256SUMS.txt'}
        $facts=Get-PackageFacts $PackageZip $ZipManifestPath;$report.package=$facts
        $stage=Join-Path $script:RunRoot 'isolated_package';New-Item -ItemType Directory -Force -Path $stage|Out-Null
        $exe=Join-Path $stage 'ZisuResearchStudio.exe';if(!(Test-Path -LiteralPath $exe)){[IO.Compression.ZipFile]::ExtractToDirectory($PackageZip,$stage)}
        if((Get-FileHash -Algorithm SHA256 -LiteralPath $exe).Hash.ToLowerInvariant() -ne $facts.exe_sha256){throw 'Isolated package EXE hash mismatch.'}
        if($env:OS -eq 'Windows_NT'){
            $script:FailureStep='isolated_windows_exe_smoke'
            $smoke=Join-Path $script:RepoRoot 'scripts\windows_exe_smoke.ps1';$smokeRoot=Join-Path $script:RunRoot ('isolated_exe_smoke_'+(Get-Date -Format 'HHmmss'))
            Invoke-Checked powershell.exe @('-NoProfile','-ExecutionPolicy','Bypass','-File',$smoke,'-ExePath',$exe,'-WorkRoot',$smokeRoot,'-TimeoutSeconds','45') $script:RepoRoot
            Report-Step 'package_hash_and_isolated_install_smoke' 'PASS' 'ZIP/EXE manifests, runtime and isolated Windows GUI smoke passed.'
        }else{Report-Step 'package_hash' 'PASS' 'ZIP and EXE hash verified; Windows-only GUI smoke unavailable.'}
    }else{Report-Step 'package_validation' 'NOT_RUN' 'Provide -PackageZip to test a package offline.'}

    if(-not $ExecuteRelease){
        Check-SafePaths $script:RepoRoot -CachedOnly
        $report.result='DRY_RUN_PASS';$report.proxy_override_applied=$false
        $report.report_path=Join-Path $script:RunRoot 'release-report.json'
        $report|ConvertTo-Json -Depth 8|Set-Content -LiteralPath $report.report_path -Encoding utf8;Save-Checkpoint
        $report|ConvertTo-Json -Depth 8;exit 0
    }

    $auth=Invoke-GH gh @('auth','status') -Capture;if(-not $auth){throw 'gh auth status failed in this Codex process. Authentication details were not requested or printed.'}
    Report-Step 'github_auth' 'PASS' 'gh authentication is available; identity withheld.'
    Check-SafePaths $script:RepoRoot
    $staged=@((Get-Text git @('diff','--cached','--name-only') $script:RepoRoot)-split "`r?`n"|Where-Object {$_})
    if(!$staged.Count){Invoke-Checked git @('add','--all') $script:RepoRoot;Check-SafePaths $script:RepoRoot -CachedOnly}
    Invoke-Checked git @('diff','--cached','--check') $script:RepoRoot
    if(@((Get-Text git @('diff','--cached','--name-only') $script:RepoRoot)-split "`r?`n"|Where-Object {$_}).Count){Invoke-Checked git @('commit','-m',$CommitMessage) $script:RepoRoot}
    $head=(Get-Text git @('rev-parse','HEAD') $script:RepoRoot);$report.commit_sha=$head;$script:Checkpoint.commit_sha=$head;Save-Checkpoint
    Report-Step 'commit' 'PASS' 'Reviewed safe payload committed.'

    $remote=(Invoke-GH git @('ls-remote','origin',"refs/heads/$branch") -Capture | Out-String).Trim()
    if($remote){$remoteSha=($remote-split '\s+')[0];if($remoteSha -ne $head){& git -C $script:RepoRoot merge-base --is-ancestor $remoteSha $head;if($LASTEXITCODE -ne 0){throw 'Remote branch diverged; no rebase/merge/force push attempted.'};Invoke-GH git @('push','origin',"HEAD:refs/heads/$branch");Report-Step 'push' 'PASS' 'Fast-forward push only.'}else{Report-Step 'push' 'SKIPPED' 'Remote already at local HEAD.'}}
    else{Invoke-GH git @('push','origin',"HEAD:refs/heads/$branch");Report-Step 'push' 'PASS' 'Branch pushed without force.'}

    $deadline=(Get-Date).AddMinutes($TimeoutMinutes);$run=$null
    while((Get-Date)-lt $deadline){$items=Invoke-GH gh @('run','list','--commit',$head,'--json','databaseId,headSha,status,conclusion','--limit','20') -Capture|ConvertFrom-Json;$matches=@($items|Where-Object {$_.headSha -eq $head});$run=$matches|Where-Object {$_.status -eq 'completed' -and $_.conclusion -eq 'success'}|Select-Object -First 1;if(!$run){$run=$matches|Select-Object -First 1};if($run -and $run.status -eq 'completed'){break};Start-Sleep -Seconds 15}
    if(!$run -or $run.status -ne 'completed' -or $run.conclusion -ne 'success' -or $run.headSha -ne $head){throw 'Matching CI run did not finish successfully before timeout.'}
    $report.ci_run_id=$run.databaseId;Report-Step 'github_actions' 'PASS' "CI succeeded for commit $head."
    $artifact=Join-Path $script:RunRoot 'artifact'
    if(!(Test-Path -LiteralPath $artifact)){New-Item -ItemType Directory -Path $artifact|Out-Null;Invoke-GH gh @('run','download',[string]$run.databaseId,'--name',$ArtifactName,'--dir',$artifact)}
    $z=Get-ChildItem -LiteralPath $artifact -Filter 'ZISU-Research-Studio-V*-Windows.zip' -File -Recurse|Select-Object -First 1
    $m=Get-ChildItem -LiteralPath $artifact -Filter 'RELEASE_SHA256SUMS.txt' -File -Recurse|Select-Object -First 1
    if(!$z -or !$m){throw 'CI artifact missing release ZIP or manifest.'};$facts=Get-PackageFacts $z.FullName $m.FullName;$report.package=$facts
    Report-Step 'artifact_download_hash' 'PASS' 'CI artifact ZIP and embedded EXE hash verified.'

    if($Install){
        Test-NoZisuProcess
        $snapshot=Join-Path $script:RunRoot ('user_database_snapshot_'+(Get-Date -Format 'HHmmss')+'.sqlite');$python=(Get-Command python.exe -ErrorAction Stop).Source
        $dbBefore=Backup-Database $DatabasePath $snapshot $python;$report.database_before=$dbBefore
        Report-Step 'sqlite_backup_api' 'PASS' 'Backup integrity, table counts and logical digest matched.'
        $old=Join-Path $script:RunRoot 'previous_install'
        $launcherName='ZISU_'+([char]0xBC14)+([char]0xB85C)+([char]0xC2E4)+([char]0xD589)+'.cmd'
        $launcher=Join-Path (Split-Path -Parent $InstallDirectory) $launcherName;$launcherBackup=Join-Path $script:RunRoot 'previous_launcher.cmd'
        $stage=Join-Path $script:RunRoot 'install_stage'
        try{
            $installedPath=Join-Path $InstallDirectory 'ZisuResearchStudio.exe'
            $alreadyInstalled=(Test-Path -LiteralPath $installedPath) -and ((Get-FileHash -Algorithm SHA256 -LiteralPath $installedPath).Hash.ToLowerInvariant() -eq $facts.exe_sha256)
            if(-not $alreadyInstalled){
                if((Test-Path -LiteralPath $InstallDirectory) -and (Test-Path -LiteralPath $old)){throw 'A previous-install backup exists but installed version is not the expected new EXE; refusing replacement.'}
                if(Test-Path -LiteralPath $launcher -and !(Test-Path -LiteralPath $launcherBackup)){Copy-Item -LiteralPath $launcher -Destination $launcherBackup}
                if((Test-Path -LiteralPath $InstallDirectory) -and !(Test-Path -LiteralPath $old)){Move-Item -LiteralPath $InstallDirectory -Destination $old}
                if(!(Test-Path -LiteralPath $stage)){New-Item -ItemType Directory -Path $stage|Out-Null;[IO.Compression.ZipFile]::ExtractToDirectory($z.FullName,$stage)}
                if((Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $stage 'ZisuResearchStudio.exe')).Hash.ToLowerInvariant() -ne $facts.exe_sha256){throw 'Staged EXE hash mismatch.'}
                Move-Item -LiteralPath $stage -Destination $InstallDirectory
            }
            $newLauncher=Join-Path $script:RepoRoot $launcherName;if(Test-Path -LiteralPath $newLauncher){Copy-Item -LiteralPath $newLauncher -Destination $launcher -Force}
            if((Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $InstallDirectory 'ZisuResearchStudio.exe')).Hash.ToLowerInvariant() -ne $facts.exe_sha256){throw 'Installed EXE hash mismatch.'}
            Test-NoZisuProcess
            $smoke=Join-Path $script:RepoRoot 'scripts\windows_exe_smoke.ps1';$smokeRoot=Join-Path $script:RunRoot ('installed_gui_smoke_'+(Get-Date -Format 'HHmmss'))
            Invoke-Checked powershell.exe @('-NoProfile','-ExecutionPolicy','Bypass','-File',$smoke,'-ExePath',(Join-Path $InstallDirectory 'ZisuResearchStudio.exe'),'-WorkRoot',$smokeRoot,'-TimeoutSeconds','45') $script:RepoRoot
            $after=Backup-Database $DatabasePath (Join-Path $script:RunRoot ('database_after_'+(Get-Date -Format 'HHmmss')+'.sqlite')) $python
            if($after.logical_digest -ne $dbBefore.logical_digest){throw 'User database changed after isolated install smoke.'};$report.database_after=$after
            Report-Step 'install_gui_smoke_data_compare' 'PASS' 'Installed hash, GUI smoke, and user DB logical equality verified.'
        }catch{
            $failed=Join-Path $script:RunRoot ('failed_install_'+[DateTime]::UtcNow.ToString('yyyyMMdd_HHmmss'))
            if(Test-Path -LiteralPath $InstallDirectory){Move-Item -LiteralPath $InstallDirectory -Destination $failed}
            if(Test-Path -LiteralPath $old){Move-Item -LiteralPath $old -Destination $InstallDirectory}
            if(Test-Path -LiteralPath $launcherBackup){Copy-Item -LiteralPath $launcherBackup -Destination $launcher -Force}
            throw
        }
    }else{Report-Step 'install' 'NOT_RUN' 'Install only when -Install is explicitly supplied.'}
    $report.result='RELEASE_PASS'
}catch{$report.result='FAIL';$report.safe_error=$_.Exception.GetType().Name;$report.failure_step=$script:FailureStep}
finally{$report.proxy_override_applied=$script:ProxyOverrideApplied;$report.sensitive_values_logged=$false;$report.external_api_calls=0;$report.report_path=Join-Path $script:RunRoot 'release-report.json';$report|ConvertTo-Json -Depth 8|Set-Content -LiteralPath $report.report_path -Encoding utf8;Save-Checkpoint}
$report|ConvertTo-Json -Depth 8
if($report.result -eq 'FAIL'){exit 1}
