[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$OllamaExe,
    [Parameter(Mandatory=$true)][string]$ModelsPath,
    [string]$PythonExe = '',
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'MediaBot\DesktopAI'),
    [switch]$NoShortcut
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$OllamaExe = (Resolve-Path -LiteralPath $OllamaExe).Path
$ModelsPath = (Resolve-Path -LiteralPath $ModelsPath).Path
if (-not $PythonExe) { $PythonExe = (Get-Command python.exe -ErrorAction Stop).Source }
$PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path
$pythonVersion = & $PythonExe -c 'import sys; print(sys.version_info >= (3, 10))'
if ($LASTEXITCODE -ne 0 -or $pythonVersion -ne 'True') { throw 'Python 3.10 or newer is required.' }
$signature = Get-AuthenticodeSignature -LiteralPath $OllamaExe
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Ollama') {
    throw 'The Ollama executable must have a valid Ollama signature.'
}
$manifest = Join-Path $ModelsPath 'manifests\registry.ollama.ai\library\gemma4\12b-it-qat'
if ((Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash.ToLowerInvariant() -ne '38044be4f923e5a55264ed7df4eaac2676651a905f735197c504045140c02bd3') {
    throw 'The Gemma model manifest does not match the reviewed model. No model was downloaded.'
}
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls.exe $InstallRoot /inheritance:r /grant:r "*$($sid):(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Cannot protect the desktop AI installation directory.' }
$existingSwitch = Join-Path $InstallRoot 'Desktop-AI.ps1'
if (Test-Path -LiteralPath $existingSwitch) { & $existingSwitch -Action Off -InstallRoot $InstallRoot }
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'gateway.py') -Destination (Join-Path $InstallRoot 'gateway.py') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Desktop-AI.ps1') -Destination $existingSwitch -Force
$tokenPath = Join-Path $InstallRoot 'gateway-token.txt'
if (-not (Test-Path -LiteralPath $tokenPath)) {
    $bytes = New-Object byte[] 48
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    [IO.File]::WriteAllText($tokenPath, [Convert]::ToBase64String($bytes), (New-Object Text.UTF8Encoding($false)))
}
$tailscale = (Get-Command tailscale.exe -ErrorAction Stop).Source
$tailState = (& $tailscale status --json | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or $tailState.BackendState -ne 'Running') { throw 'Tailscale must be signed in and running.' }
$dnsName = $tailState.Self.DNSName.TrimEnd('.')
$config = [ordered]@{
    ollama_exe = $OllamaExe
    ollama_sha256 = (Get-FileHash -LiteralPath $OllamaExe -Algorithm SHA256).Hash.ToLowerInvariant()
    models_path = $ModelsPath
    python_exe = $PythonExe
    tailscale_exe = $tailscale
    endpoint = "https://$($dnsName):8445"
    model = 'gemma4:12b-it-qat'
    model_manifest_sha256 = 'sha256:38044be4f923e5a55264ed7df4eaac2676651a905f735197c504045140c02bd3'
}
[IO.File]::WriteAllText((Join-Path $InstallRoot 'config.json'), ($config | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
New-Item -ItemType Directory -Path (Join-Path $InstallRoot 'runtime-profile') -Force | Out-Null
if (-not $NoShortcut) {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'MediaBot Desktop AI.lnk'))
    $shortcut.TargetPath = (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe')
    $shortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $existingSwitch + '" -Action UI -InstallRoot "' + $InstallRoot + '"'
    $shortcut.WorkingDirectory = $InstallRoot
    $shortcut.Description = 'Enable or disable the private MediaBot desktop GPU worker.'
    $shortcut.IconLocation = (Join-Path $env:SystemRoot 'System32\shell32.dll') + ',25'
    $shortcut.Save()
}
Write-Output "Desktop AI installed, currently off. Private endpoint: $($config.endpoint)"
Write-Output "Switch: $existingSwitch"
Write-Output "Credential file for the server operator: $tokenPath"
