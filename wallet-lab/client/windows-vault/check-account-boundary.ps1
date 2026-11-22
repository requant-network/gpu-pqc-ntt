# Run from an elevated Windows PowerShell session with the compiled lab test binary.
param([string]$WalletTest, [string]$ServiceRoot)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Security
$serviceSid = 'S-1-5-19'
$entropy = [Text.Encoding]::UTF8.GetBytes('requant/pqc-lab/windows-seed/v1')
$scope = [Security.Cryptography.DataProtectionScope]::CurrentUser
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value

if ($ServiceRoot) {
    if ($sid -ne $serviceSid) { throw 'The probe must run as LocalService' }
    $wallet = [IO.File]::ReadAllBytes((Join-Path $ServiceRoot 'wallet.sealed'))
    if ([Text.Encoding]::ASCII.GetString($wallet, 0, 8) -ne 'RQPL-WS1') {
        throw 'Unexpected wallet header'
    }
    $sample = [byte[]](1..33)
    $sealed = [Security.Cryptography.ProtectedData]::Protect($sample, $entropy, $scope)
    $opened = [Security.Cryptography.ProtectedData]::Unprotect($sealed, $entropy, $scope)
    if ([Convert]::ToBase64String($opened) -ne [Convert]::ToBase64String($sample)) {
        throw 'LocalService DPAPI control did not round-trip'
    }
    try {
        $opened = [Security.Cryptography.ProtectedData]::Unprotect($wallet[8..($wallet.Length - 1)], $entropy, $scope)
        [Array]::Clear($opened, 0, $opened.Length)
        throw 'LocalService opened the other account wallet'
    } catch [Security.Cryptography.CryptographicException] {
        $report = [ordered]@{
            foreign_principal = 'LocalService'
            foreign_own_dpapi_roundtrip = $true
            foreign_wallet_ciphertext_sha256 = (Get-FileHash (Join-Path $ServiceRoot 'wallet.sealed')).Hash.ToLowerInvariant()
            foreign_unprotect_hresult = '{0:x8}' -f $_.Exception.GetBaseException().HResult
        }
        $report | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $ServiceRoot 'result/probe.json')
    }
    return
}

if (-not $WalletTest) { throw 'Pass the compiled pqc_wallet test binary with -WalletTest' }
$WalletTest = (Resolve-Path -LiteralPath $WalletTest).Path
$task = "RequantDpapiLab-$([guid]::NewGuid().ToString('N'))"
$root = Join-Path $env:ProgramData $task
$registered = $false
New-Item -ItemType Directory -Path $root | Out-Null
try {
    & icacls $root /inheritance:r /grant:r "*${sid}:(OI)(CI)F" "*${serviceSid}:(OI)(CI)RX" | Out-Null
    if ($LASTEXITCODE) { throw 'Could not set fixture ACL' }
    $result = Join-Path $root 'result'
    New-Item -ItemType Directory -Path $result | Out-Null
    & icacls $result /grant:r "*${serviceSid}:(OI)(CI)M" | Out-Null
    if ($LASTEXITCODE) { throw 'Could not set result ACL' }
    $script = Join-Path $root 'probe.ps1'
    Copy-Item -LiteralPath $PSCommandPath -Destination $script
    $env:REQUANT_DPAPI_LAB_BLOB = Join-Path $root 'wallet.sealed'
    $env:REQUANT_DPAPI_LAB_ARTIFACT = Join-Path $root 'wallet-public.bin'
    $env:REQUANT_DPAPI_LAB_PHASE = 'initialize'
    try {
        & $WalletTest --exact windows_vault::dpapi_child --ignored --nocapture | Out-Null
        if ($LASTEXITCODE) { throw 'Could not initialize the disposable Rust wallet' }
    } finally {
        Remove-Item Env:REQUANT_DPAPI_LAB_BLOB, Env:REQUANT_DPAPI_LAB_ARTIFACT, Env:REQUANT_DPAPI_LAB_PHASE
    }
    $walletPath = Join-Path $root 'wallet.sealed'
    $wallet = [IO.File]::ReadAllBytes($walletPath)
    $hash = (Get-FileHash $walletPath).Hash.ToLowerInvariant()
    $opened = [Security.Cryptography.ProtectedData]::Unprotect($wallet[8..($wallet.Length - 1)], $entropy, $scope)
    try {
        if ($opened.Length -ne 33 -or $opened[0] -ne 1) { throw 'Owner DPAPI control failed' }
    } finally {
        [Array]::Clear($opened, 0, $opened.Length)
    }

    $principal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\LOCAL SERVICE' -LogonType ServiceAccount -RunLevel Limited
    $windowsPowerShell = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
    $action = New-ScheduledTaskAction -Execute $windowsPowerShell -Argument "-NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File `"$script`" -ServiceRoot `"$root`""
    Register-ScheduledTask -TaskName $task -Action $action -Principal $principal | Out-Null
    $registered = $true
    $started = (Get-Date).AddSeconds(-2)
    Start-ScheduledTask -TaskName $task
    $deadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 200
        $info = Get-ScheduledTaskInfo -TaskName $task
        $state = (Get-ScheduledTask -TaskName $task).State
    } until (($info.LastRunTime -ge $started -and $state -ne 'Running' -and $state -ne 'Queued') -or (Get-Date) -ge $deadline)
    if ($info.LastTaskResult -ne 0) { throw "LocalService probe failed with task code $($info.LastTaskResult)" }
    $report = Get-Content -LiteralPath (Join-Path $result 'probe.json') -Raw | ConvertFrom-Json
    if ($report.foreign_wallet_ciphertext_sha256 -ne $hash -or (Get-FileHash $walletPath).Hash -ine $hash) {
        throw 'Probe did not read the intact Rust wallet'
    }
    $report | Add-Member -NotePropertyName owner_unprotect_succeeded -NotePropertyValue $true
    $report | ConvertTo-Json
} finally {
    if ($registered) {
        Stop-ScheduledTask -TaskName $task
        Unregister-ScheduledTask -TaskName $task -Confirm:$false
    }
    # This is the generated fixture directory and contains no pre-existing paths.
    if ((Resolve-Path -LiteralPath $root).Path -eq (Join-Path $env:ProgramData $task)) {
        Remove-Item -LiteralPath $root -Recurse -Force
    }
}
