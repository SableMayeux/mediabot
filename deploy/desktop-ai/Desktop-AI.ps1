[CmdletBinding()]
param(
    [ValidateSet('On','Off','Status','UI')][string]$Action = 'UI',
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'MediaBot\DesktopAI')
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$config = Get-Content -LiteralPath (Join-Path $InstallRoot 'config.json') -Raw | ConvertFrom-Json
$gatewayPath = Join-Path $InstallRoot 'gateway.py'
$sessionPath = Join-Path $InstallRoot 'session.json'
$stopPath = Join-Path $InstallRoot 'stop.request'
$markerPath = Join-Path $InstallRoot 'serve-owned.json'

function Read-WorkerStatus {
    try {
        $token = [IO.File]::ReadAllText((Join-Path $InstallRoot 'gateway-token.txt')).Trim()
        return Invoke-RestMethod -Uri 'http://127.0.0.1:11890/v1/status' -Headers @{ Authorization = ('Bearer ' + $token) } -TimeoutSec 3
    } catch {
        return [pscustomobject]@{ enabled=$false; ready=$false; loaded=$false; busy=$false; reason='off'; model=$config.model }
    }
}

function Get-OwnedGateway {
    if (-not (Test-Path -LiteralPath $sessionPath)) { return $null }
    $session = Get-Content -LiteralPath $sessionPath -Raw | ConvertFrom-Json
    $owned = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$session.gateway_pid)" -ErrorAction SilentlyContinue
    if (-not $owned) { return $null }
    if ([string]::IsNullOrWhiteSpace($owned.CommandLine)) {
        # CIM can observe a process during its final exit with no command line.
        Start-Sleep -Milliseconds 100
        $owned = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$session.gateway_pid)" -ErrorAction SilentlyContinue
        if (-not $owned) { return $null }
        if ([string]::IsNullOrWhiteSpace($owned.CommandLine)) { throw 'Cannot verify the saved process command line. It will not be stopped.' }
    }
    if ($owned.CommandLine.IndexOf($gatewayPath, [StringComparison]::OrdinalIgnoreCase) -lt 0 -or $session.gateway_path -ne $gatewayPath) {
        throw 'The saved PID belongs to a different process. It will not be stopped.'
    }
    $startedSeconds = ([DateTimeOffset]$owned.CreationDate).ToUnixTimeSeconds()
    if ([Math]::Abs($startedSeconds - [double]$session.started_at) -gt 10) {
        throw 'The saved PID was reused. It will not be stopped.'
    }
    return $owned
}

function Get-ServeTarget {
    $state = (& $config.tailscale_exe serve status --json | ConvertFrom-Json)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect existing Tailscale Serve configuration.' }
    $web = $state.PSObject.Properties['Web']
    if (-not $web) { return $null }
    $key = ([uri]$config.endpoint).Host + ':8445'
    $entry = $web.Value.PSObject.Properties[$key]
    if (-not $entry) { return $null }
    $handler = $entry.Value.Handlers.PSObject.Properties['/']
    if (-not $handler) { throw 'HTTPS port 8445 already has another Serve configuration.' }
    return $handler.Value.Proxy
}

function Enable-Worker {
    $existing = Read-WorkerStatus
    if ($existing.enabled) { return $existing }
    $target = Get-ServeTarget
    if ($target -and $target -ne 'http://127.0.0.1:11890') {
        throw 'HTTPS port 8445 belongs to another Tailscale Serve target. It will not be changed.'
    }
    $listener = Get-NetTCPConnection -LocalPort 11890 -State Listen -ErrorAction SilentlyContinue
    if ($listener) { throw 'Loopback port 11890 is already occupied by an unrecognized service.' }
    if (Get-OwnedGateway) { throw 'The worker is still starting or stopping. Retry Status in a few seconds.' }
    if (Test-Path -LiteralPath $stopPath) { Remove-Item -LiteralPath $stopPath -Force }
    if (Test-Path -LiteralPath $sessionPath) { Remove-Item -LiteralPath $sessionPath -Force }
    $args = '"' + $gatewayPath + '" --root "' + $InstallRoot + '"'
    $process = Start-Process -FilePath $config.python_exe -ArgumentList $args -WorkingDirectory $InstallRoot -WindowStyle Hidden -PassThru -RedirectStandardError (Join-Path $InstallRoot 'gateway-errors.log') -RedirectStandardOutput (Join-Path $InstallRoot 'gateway-output.log')
    $ready = $false
    for ($n=0; $n -lt 50; $n++) {
        Start-Sleep -Milliseconds 200
        if ($process.HasExited) { throw 'The desktop gateway could not start. Inspect gateway-errors.log in the installation folder.' }
        $state = Read-WorkerStatus
        if ($state.enabled) { $ready=$true; break }
    }
    if (-not $ready) {
        [IO.File]::WriteAllText($stopPath, 'stop')
        throw 'The desktop gateway did not become available. Stop was requested.'
    }
    try {
        # Dedicated private HTTPS endpoint. Never use Funnel or reset another Serve service.
        & $config.tailscale_exe serve --bg --https=8445 --yes 'http://127.0.0.1:11890' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Tailscale Serve could not enable private HTTPS.' }
        [IO.File]::WriteAllText($markerPath, '{"https_port":8445,"target":"http://127.0.0.1:11890"}')
    } catch {
        [IO.File]::WriteAllText($stopPath, 'stop')
        throw
    }
    return Read-WorkerStatus
}

function Disable-Worker {
    [IO.File]::WriteAllText($stopPath, 'stop')
    if (Test-Path -LiteralPath $markerPath) {
        $target = Get-ServeTarget
        if ($target -and $target -ne 'http://127.0.0.1:11890') {
            Write-Warning 'Port 8445 was changed by another service. Its route was preserved.'
        } elseif ($target) {
            & $config.tailscale_exe serve --https=8445 off | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'The worker was asked to stop, but its private proxy could not be disabled.' }
        }
        Remove-Item -LiteralPath $markerPath -Force
    }
    for ($n=0; $n -lt 30; $n++) {
        $owned = Get-OwnedGateway
        if (-not $owned) { break }
        Start-Sleep -Milliseconds 200
    }
    $owned = Get-OwnedGateway
    if ($owned) {
        # The gateway owns a kill-on-close Windows Job Object. Its exit also stops its Ollama children.
        Stop-Process -Id $owned.ProcessId -Force -ErrorAction Stop
    }
    return Read-WorkerStatus
}

if ($Action -eq 'Status') { Read-WorkerStatus | ConvertTo-Json -Depth 5; exit }
if ($Action -eq 'On') { Enable-Worker | ConvertTo-Json -Depth 5; exit }
if ($Action -eq 'Off') { Disable-Worker | ConvertTo-Json -Depth 5; exit }

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()
$form = New-Object Windows.Forms.Form
$form.Text = 'MediaBot Desktop AI'
$form.Size = New-Object Drawing.Size(520,315)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$title = New-Object Windows.Forms.Label
$title.Text = 'Share your desktop GPU with MediaBot'
$title.Font = New-Object Drawing.Font('Segoe UI',13,[Drawing.FontStyle]::Bold)
$title.Location = New-Object Drawing.Point(20,20)
$title.Size = New-Object Drawing.Size(470,35)
$form.Controls.Add($title)
$detail = New-Object Windows.Forms.Label
$detail.Text = "ON lets authorized users use Gemma 4 12B while resources allow.`r`nOFF frees this worker's GPU memory. Users with server access can still use it.`r`nUser access rules remain in force in both modes."
$detail.Location = New-Object Drawing.Point(20,62)
$detail.Size = New-Object Drawing.Size(470,65)
$form.Controls.Add($detail)
$status = New-Object Windows.Forms.Label
$status.Location = New-Object Drawing.Point(20,135)
$status.Size = New-Object Drawing.Size(470,55)
$status.Font = New-Object Drawing.Font('Segoe UI',10)
$form.Controls.Add($status)
function Update-Display {
    $value = Read-WorkerStatus
    if (-not $value.enabled) { $status.Text='OFF. Desktop GPU memory is available to your other apps.' }
    elseif ($value.busy) { $status.Text='ON. Answering a request.' }
    elseif ($value.ready) { $status.Text='ON. Ready for requests. The model unloads after 60 idle seconds.' }
    else { $status.Text='ON, currently yielding: ' + $value.reason }
}
$on = New-Object Windows.Forms.Button
$on.Text='Turn on'
$on.Location=New-Object Drawing.Point(20,210)
$on.Size=New-Object Drawing.Size(140,35)
$off = New-Object Windows.Forms.Button
$off.Text='Turn off'
$off.Location=New-Object Drawing.Point(175,210)
$off.Size=New-Object Drawing.Size(140,35)
$refresh = New-Object Windows.Forms.Button
$refresh.Text='Refresh'
$refresh.Location=New-Object Drawing.Point(330,210)
$refresh.Size=New-Object Drawing.Size(140,35)
$on.Add_Click({ try { $on.Enabled=$false; $form.Cursor='WaitCursor'; Enable-Worker | Out-Null; Update-Display } catch { [Windows.Forms.MessageBox]::Show($_.Exception.Message,'Desktop AI') | Out-Null } finally { $on.Enabled=$true; $form.Cursor='Default' } })
$off.Add_Click({ try { $off.Enabled=$false; $form.Cursor='WaitCursor'; Disable-Worker | Out-Null; Update-Display } catch { [Windows.Forms.MessageBox]::Show($_.Exception.Message,'Desktop AI') | Out-Null } finally { $off.Enabled=$true; $form.Cursor='Default' } })
$refresh.Add_Click({ Update-Display })
$form.Controls.AddRange(@($on,$off,$refresh))
Update-Display
[void]$form.ShowDialog()
