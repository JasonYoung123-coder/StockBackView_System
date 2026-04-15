$ErrorActionPreference = "SilentlyContinue"

$workspace = (Resolve-Path $PSScriptRoot).Path.TrimEnd("\")
$ports = @(8000, 8010, 8011, 8012, 8013, 8080)
$stoppedRoots = New-Object System.Collections.Generic.List[string]
$scheduledRoots = New-Object System.Collections.Generic.HashSet[int]

function Get-ProcessMap {
    $all = Get-CimInstance Win32_Process
    $byId = @{}
    $children = @{}

    foreach ($proc in $all) {
        $pidValue = [int]$proc.ProcessId
        $parentValue = [int]$proc.ParentProcessId
        $byId[$pidValue] = $proc
        if (-not $children.ContainsKey($parentValue)) {
            $children[$parentValue] = New-Object System.Collections.Generic.List[int]
        }
        $children[$parentValue].Add($pidValue) | Out-Null
    }

    return @{
        ById = $byId
        Children = $children
    }
}

function Test-IsBackendProcess {
    param(
        $Proc
    )

    if (-not $Proc) { return $false }
    $cmd = [string]$Proc.CommandLine
    if ([string]::IsNullOrWhiteSpace($cmd)) { return $false }

    $hasBackendSignature = (
        $cmd -match "(^|\s)-m\s+uvicorn(\s|$)" -or
        $cmd -match "app\.main:app" -or
        $cmd -match "uvicorn" -or
        $cmd -match "run_backend\.py"
    )
    $hasProjectPath = $cmd -like "*$workspace*"

    return ($hasBackendSignature -and ($hasProjectPath -or $cmd -match "app\.main:app"))
}

function Get-RootBackendPid {
    param(
        [int]$StartPid,
        $ById
    )

    $current = $StartPid
    $candidate = $null

    while ($current -gt 0 -and $ById.ContainsKey($current)) {
        $proc = $ById[$current]
        if (Test-IsBackendProcess -Proc $proc) {
            $candidate = $current
        }
        $current = [int]$proc.ParentProcessId
    }

    if ($null -ne $candidate) {
        return [int]$candidate
    }

    return $StartPid
}

function Stop-BackendTree {
    param(
        [int]$RootPid,
        [string]$Reason,
        $ById
    )

    if ($RootPid -le 0) { return }
    if (-not $scheduledRoots.Add($RootPid)) { return }

    $proc = $ById[$RootPid]
    if (-not $proc) { return }

    $cmd = [string]$proc.CommandLine
    $display = if ([string]::IsNullOrWhiteSpace($cmd)) { [string]$proc.Name } else { $cmd }

    Write-Host ("[KILL] root PID {0} - {1}" -f $RootPid, $Reason)
    Write-Host ("       {0}" -f $display)

    cmd /c "taskkill /PID $RootPid /T /F" | Out-Null
    $stoppedRoots.Add(("PID {0} - {1}" -f $RootPid, $Reason)) | Out-Null
}

Write-Host "[INFO] Scanning StockBackView backend processes..."

$map = Get-ProcessMap
$byId = $map.ById

foreach ($proc in $byId.Values) {
    if (-not (Test-IsBackendProcess -Proc $proc)) {
        continue
    }
    $rootPid = Get-RootBackendPid -StartPid ([int]$proc.ProcessId) -ById $byId
    Stop-BackendTree -RootPid $rootPid -Reason "Matched backend command line" -ById $byId
}

foreach ($port in $ports) {
    $listeners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        $ownerPid = [int]$listener.OwningProcess
        if (-not $byId.ContainsKey($ownerPid)) {
            continue
        }
        $rootPid = Get-RootBackendPid -StartPid $ownerPid -ById $byId
        $rootProc = $byId[$rootPid]
        if (Test-IsBackendProcess -Proc $rootProc) {
            Stop-BackendTree -RootPid $rootPid -Reason ("Listening on port {0}" -f $port) -ById $byId
        }
    }
}

Start-Sleep -Milliseconds 1000

$remaining = foreach ($port in $ports) {
    Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty LocalPort
}
$remaining = @($remaining | Sort-Object -Unique)

if ($stoppedRoots.Count -eq 0) {
    Write-Host "[INFO] No backend processes needed cleanup."
    exit 0
}

Write-Host ""
Write-Host ("[INFO] Stopped {0} backend root processes." -f $stoppedRoots.Count)

if ($remaining.Count -gt 0) {
    Write-Host ("[WARN] Ports still listening: {0}" -f ($remaining -join ", "))
    exit 1
}

Write-Host "[INFO] Common backend ports have been cleaned up."
exit 0
