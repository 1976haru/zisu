[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

function Quote-Arg([string]$Value) { return '"' + $Value.Replace('"','\"') + '"' }
function Invoke-Captured([string]$File,[string[]]$Arguments,[string]$Cwd) {
    $start=[Diagnostics.ProcessStartInfo]::new()
    $start.FileName=$File
    $start.Arguments=(($Arguments | ForEach-Object { Quote-Arg ([string]$_) }) -join ' ')
    $start.WorkingDirectory=$Cwd
    $start.UseShellExecute=$false
    $start.CreateNoWindow=$true
    $start.RedirectStandardOutput=$true
    $start.RedirectStandardError=$true
    $child=[Diagnostics.Process]::new();$child.StartInfo=$start
    try {
        if(!$child.Start()){throw 'CHILD_START_FAILED'}
        $outTask=$child.StandardOutput.ReadToEndAsync();$errTask=$child.StandardError.ReadToEndAsync()
        if(!$child.WaitForExit(180000)){try{$child.Kill()}catch{};$child.WaitForExit();throw 'CHILD_TIMEOUT'}
        return [pscustomobject]@{ExitCode=$child.ExitCode;Stdout=$outTask.Result;Stderr=$errTask.Result}
    } finally {$child.Dispose()}
}
function Get-EnvironmentClass {
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    $principal=[Security.Principal.WindowsPrincipal]::new($identity)
    if($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){
        return [pscustomobject]@{Class='ADMIN_SESSION';IsAdmin=$true;ManagedMarker=$false}
    }
    $names=@('ZISU_MANAGED_SANDBOX','CODEX_SANDBOX','OPENAI_SANDBOX')
    $present=@($names | Where-Object {[Environment]::GetEnvironmentVariable($_)})
    if($present.Count){return [pscustomobject]@{Class='MANAGED_SANDBOX_MARKED';IsAdmin=$false;ManagedMarker=$true}}
    return [pscustomobject]@{Class='STANDARD_USER_SESSION';IsAdmin=$false;ManagedMarker=$false}
}
function Get-LastJson([string]$Text) {
    $lines=@($Text -split "`r?`n" | Where-Object { $_.Trim() })
    for($i=$lines.Count-1;$i -ge 0;$i--){try{return ($lines[$i] | ConvertFrom-Json)}catch{}}
    return $null
}
function Write-Result($Result) {
    $reportPath=$null
    if($null -eq $Result['cross_user_access']){
        $Result['cross_user_access']='PENDING_NO_SECOND_USER_CREDENTIALS'
    }
    if($fixtureRoot -and (Test-Path -LiteralPath $fixtureRoot -PathType Container)){
        $reportPath=Join-Path $fixtureRoot 'acl-fixture-report.json'
        $Result['report_path']=$reportPath
        $partialPath=$reportPath+'.partial'
        try{
            $payload=$Result | ConvertTo-Json -Depth 8 -Compress
            $bytes=[Text.UTF8Encoding]::new($false).GetBytes($payload+"`n")
            $stream=[IO.File]::Open($partialPath,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
            try{$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
            [IO.File]::Move($partialPath,$reportPath)
        }catch{
            if($Result.result -eq 'MEMORY_DPAPI_ACL_FIXTURE_PASS'){$Result.result='ACL_FIXTURE_FAIL'}
            $Result['report_path']=$null
            $Result['report_write_error']='REPORT_PERSISTENCE_FAILED'
            $payload=$Result | ConvertTo-Json -Depth 8 -Compress
        }
    }else{
        $payload=$Result | ConvertTo-Json -Depth 8 -Compress
    }
    Write-Output $payload
}
function Test-PathInside([string]$Child,[string]$Parent) {
    $childPath=[IO.Path]::GetFullPath($Child).TrimEnd('\')
    $parentPath=[IO.Path]::GetFullPath($Parent).TrimEnd('\')
    return ($childPath.Equals($parentPath,[StringComparison]::OrdinalIgnoreCase) -or
        $childPath.StartsWith($parentPath+'\',[StringComparison]::OrdinalIgnoreCase))
}
function Assert-NoReparseAncestor([string]$Path) {
    $current=[IO.Path]::GetFullPath($Path)
    while($current){
        if(Test-Path -LiteralPath $current){
            $item=Get-Item -LiteralPath $current -Force
            if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'FIXTURE_PATH_REPARSE_POINT_REJECTED'}
        }
        $parent=[IO.Directory]::GetParent($current)
        if(!$parent -or $parent.FullName -eq $current){break}
        $current=$parent.FullName
    }
}

$environment=Get-EnvironmentClass
$repo=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$realDb=if($env:LOCALAPPDATA){[IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'ZisuResearchStudio\zisu_v2.db'))}else{''}
$fixtureRoot=$null
$phase='preflight'
try {
    if($environment.IsAdmin){
        Write-Result ([ordered]@{result='ACL_FIXTURE_PENDING';environment_class=$environment.Class;fixture_only=$true;phase=$phase;reason='ADMIN_SESSION_NOT_QUALIFYING';message='Run from a normal, non-elevated Windows user session.'})
        exit 2
    }
    if($environment.ManagedMarker){
        Write-Result ([ordered]@{result='ACL_FIXTURE_PENDING';environment_class=$environment.Class;fixture_only=$true;phase=$phase;reason='MANAGED_SANDBOX_MUST_NOT_RETRY_DACL'})
        exit 2
    }
    if(!$env:LOCALAPPDATA){throw 'LOCALAPPDATA_UNAVAILABLE'}
    $releaseScript=Join-Path $repo 'scripts\zisu_release_oneclick.ps1'
    $fixtureBuilder=Join-Path $repo 'scripts\secure_backup_acl_fixture.py'
    if(!(Test-Path -LiteralPath $releaseScript -PathType Leaf) -or !(Test-Path -LiteralPath $fixtureBuilder -PathType Leaf)){
        throw 'REQUIRED_FIXTURE_TOOL_MISSING'
    }
    $base=[IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'ZisuSecureBackupFixtureRuns'))
    $repoPrefix=$repo.TrimEnd('\')+'\'
    $appData=[IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'ZisuResearchStudio'))
    if($base.StartsWith($repoPrefix,[StringComparison]::OrdinalIgnoreCase) -or
       $base.Equals($appData,[StringComparison]::OrdinalIgnoreCase) -or
       $base.StartsWith($appData.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)){
        throw 'FIXTURE_ROOT_OVERLAPS_PROTECTED_PATH'
    }
    Assert-NoReparseAncestor $base
    foreach($name in @('OneDrive','OneDriveConsumer','OneDriveCommercial')){
        $sync=[Environment]::GetEnvironmentVariable($name)
        if($sync -and (Test-PathInside $base $sync)){throw 'FIXTURE_ROOT_CLOUD_SYNC_PATH_REJECTED'}
    }
    $drive=[IO.DriveInfo]::new([IO.Path]::GetPathRoot($base))
    if($drive.DriveType -ne [IO.DriveType]::Fixed -or $drive.DriveFormat -ne 'NTFS'){
        Write-Result ([ordered]@{result='ACL_FIXTURE_PENDING';environment_class=$environment.Class;fixture_only=$true;phase='path_preflight';reason='FIXTURE_ROOT_NOT_LOCAL_NTFS'})
        exit 2
    }
    [void][IO.Directory]::CreateDirectory($base)
    $fixtureRoot=Join-Path $base ('fixture_'+[Guid]::NewGuid().ToString('N'))
    if(Test-Path -LiteralPath $fixtureRoot){throw 'FIXTURE_ROOT_ALREADY_EXISTS'}
    [void][IO.Directory]::CreateDirectory($fixtureRoot)
    $fixtureRoot=[IO.Path]::GetFullPath($fixtureRoot)
    Assert-NoReparseAncestor $fixtureRoot
    $fixtureDb=[IO.Path]::GetFullPath((Join-Path $fixtureRoot 'fixture_db.sqlite'))
    if($fixtureDb -eq $realDb -or $fixtureDb.StartsWith($appData.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)){
        throw 'REAL_DATABASE_PATH_REJECTED'
    }

    $python=(Get-Command python.exe -ErrorAction Stop).Source
    $phase='create_synthetic_wal_fixture'
    $created=Invoke-Captured $python @($fixtureBuilder,'--create',$fixtureRoot) $repo
    if($created.ExitCode -ne 0){throw 'SYNTHETIC_FIXTURE_CREATE_FAILED'}
    $fixtureFacts=Get-LastJson $created.Stdout
    if(!$fixtureFacts -or $fixtureFacts.result -ne 'FIXTURE_READY' -or
       $fixtureFacts.database_header -ne '0202' -or $fixtureFacts.manual_items -ne 50 -or
       $fixtureFacts.kr_items -ne 0 -or !$fixtureFacts.jp_exclude_note -or
       [IO.Path]::GetFullPath($fixtureFacts.database) -ne $fixtureDb){
        throw 'SYNTHETIC_FIXTURE_VALIDATION_FAILED'
    }
    $marker=Join-Path $fixtureRoot '.zisu-synthetic-backup-fixture'
    if(!(Test-Path -LiteralPath $marker -PathType Leaf) -or !(Test-Path -LiteralPath $fixtureDb -PathType Leaf)){
        throw 'FIXTURE_INPUT_NOT_CREATED_BY_THIS_RUN'
    }

    $phase='fixture_only_release_path'
    $safeInstallTarget=Join-Path $fixtureRoot 'must_not_install_here'
    $releaseArgs=@('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',$releaseScript,
        '-RepoPath',$repo,'-FixtureOnly','-FixtureRoot',$fixtureRoot,
        '-DatabasePath',$fixtureDb,'-InstallDirectory',$safeInstallTarget)
    $release=Invoke-Captured (Join-Path $PSHOME 'powershell.exe') $releaseArgs $repo
    $releaseFacts=Get-LastJson $release.Stdout
    if($release.ExitCode -ne 0 -or !$releaseFacts -or $releaseFacts.result -ne 'PASS'){
        $releasePhase=if($releaseFacts -and $releaseFacts.phase){[string]$releaseFacts.phase}else{'release_fixture_failed'}
        $releaseError=if($releaseFacts -and $releaseFacts.safe_error_type){[string]$releaseFacts.safe_error_type}elseif($releaseFacts -and $releaseFacts.error_type){[string]$releaseFacts.error_type}else{'FixtureOnlyFailure'}
        $aclError=($releaseFacts.winerror -eq 5) -or ($releaseError -in @('PermissionError','UnauthorizedAccessException'))
        $result=if($aclError){'ACL_PERMISSION_BLOCKED'}else{'ACL_FIXTURE_FAIL'}
        Write-Result ([ordered]@{result=$result;environment_class=$environment.Class;fixture_only=$true;phase=$releasePhase;error_type=$releaseError;winerror=if($releaseFacts){$releaseFacts.winerror}else{$null};errno=if($releaseFacts){$releaseFacts.errno}else{$null};fixture_root=$fixtureRoot;real_database_argument_used=$false;install_performed=$false})
        if($result -eq 'ACL_PERMISSION_BLOCKED'){exit 2}else{exit 1}
    }
    if($releaseFacts.backup_mode -ne 'MEMORY_DPAPI' -or $releaseFacts.plaintext_backup_files -ne 0 -or
       !$releaseFacts.restore_verified -or $releaseFacts.dpapi_scope -ne 'CurrentUser' -or
       !$releaseFacts.source_file_set_unchanged -or $releaseFacts.table_counts.manual_research_items -ne 50 -or
       $releaseFacts.crash_partial_acl -ne 'PASS' -or $releaseFacts.crash_final_published -ne $false){
        throw 'RELEASE_BACKUP_FACTS_INCOMPLETE'
    }

    $phase='corrupt_ciphertext_rejection'
    $verify=Invoke-Captured $python @($fixtureBuilder,'--verify-corrupt',$fixtureRoot,[string]$releaseFacts.backup_path) $repo
    if($verify.ExitCode -ne 0){throw 'CORRUPT_BACKUP_REJECTION_TEST_FAILED'}
    $verifyFacts=Get-LastJson $verify.Stdout
    if(!$verifyFacts -or $verifyFacts.result -ne 'CORRUPT_CIPHERTEXT_REJECTED' -or !$verifyFacts.backup_reopened -or
       $verifyFacts.integrity -ne 'ok' -or !$verifyFacts.corrupt_probe_removed){
        throw 'RESTORE_OR_CORRUPTION_FACTS_INCOMPLETE'
    }
    $finalResult=[ordered]@{
        result='MEMORY_DPAPI_ACL_FIXTURE_PASS';environment_class=$environment.Class;fixture_only=$true;phase='complete'
        fixture_root=$fixtureRoot;database_header=$fixtureFacts.database_header;wal_bytes=$fixtureFacts.wal_bytes
        shm_present=$fixtureFacts.shm_present;manual_items=$fixtureFacts.manual_items;jp_exclude_note=$fixtureFacts.jp_exclude_note
        powershell_acl_probe='PASS';python_child_write='PASS';dpapi_current_user='PASS';ciphertext_partial_and_atomic_publish='PASS'
        crash_partial_acl=$releaseFacts.crash_partial_acl;crash_final_published=$releaseFacts.crash_final_published
        memory_only_snapshot=$releaseFacts.plaintext_snapshot_staging;source_file_set_unchanged=$releaseFacts.source_file_set_unchanged
        cross_user_access='PENDING_NO_SECOND_USER_CREDENTIALS'
        encrypted_reopen_restore='PASS';integrity=$verifyFacts.integrity;foreign_keys=$verifyFacts.foreign_keys
        corrupt_ciphertext_rejected=$true;plaintext_snapshot_staging='none_memory_only'
        real_database_argument_used=$false;real_database_modified=$false;install_performed=$false;api_oauth_calls=0
        backup_path=$releaseFacts.backup_path;fixture_evidence_retained=$true
    }
    Write-Result $finalResult
    if($finalResult.result -ne 'MEMORY_DPAPI_ACL_FIXTURE_PASS'){exit 1}
    exit 0
} catch {
    $category=if($_.Exception.Message -match 'Access is denied|denied|UnauthorizedAccess'){'ACL_WRITE_OR_ACCESS_DENIED'}else{$_.Exception.Message -replace '[^A-Za-z0-9_-]','_'}
    $result=if($phase -eq 'fixture_only_release_path' -and
       $category -eq 'ACL_WRITE_OR_ACCESS_DENIED'){'ACL_PERMISSION_BLOCKED'}else{'ACL_FIXTURE_FAIL'}
    Write-Result ([ordered]@{result=$result;environment_class=$environment.Class;fixture_only=$true;phase=$phase;error_code=$category;fixture_root=$fixtureRoot;real_database_argument_used=$false;install_performed=$false})
    if($result -eq 'ACL_PERMISSION_BLOCKED'){exit 2}else{exit 1}
}
