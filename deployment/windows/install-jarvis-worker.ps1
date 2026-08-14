[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Wheel,
    [Parameter(Mandatory = $true)][string]$Config,
    [Parameter(Mandatory = $true)][string]$CaCertificate,
    [Parameter(Mandatory = $true)][string]$ClientCertificate,
    [Parameter(Mandatory = $true)][string]$ClientPrivateKey
)

$ErrorActionPreference = 'Stop'
$serviceName = 'JarvisWindowsWorker'
$root = Join-Path $env:ProgramData 'Jarvis\worker'
$credentials = Join-Path $root 'credentials'
$venv = Join-Path $root '.venv'

if (Get-Service -Name $serviceName -ErrorAction SilentlyContinue) {
    throw "Service $serviceName already exists; use the reviewed upgrade procedure"
}

New-Item -ItemType Directory -Force -Path $credentials | Out-Null
Copy-Item -LiteralPath $Config -Destination (Join-Path $credentials 'worker.json')
Copy-Item -LiteralPath $CaCertificate -Destination (Join-Path $credentials 'worker-ca.pem')
Copy-Item -LiteralPath $ClientCertificate -Destination (Join-Path $credentials 'worker-certificate.pem')
Copy-Item -LiteralPath $ClientPrivateKey -Destination (Join-Path $credentials 'worker-private-key.pem')

uv venv --python 3.13 $venv
uv pip install --python (Join-Path $venv 'Scripts\python.exe') $Wheel

$acl = Get-Acl -LiteralPath $root
$acl.SetAccessRuleProtection($true, $false)
$rules = @(
    (New-Object System.Security.AccessControl.FileSystemAccessRule('SYSTEM', 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    (New-Object System.Security.AccessControl.FileSystemAccessRule('BUILTIN\Administrators', 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    (New-Object System.Security.AccessControl.FileSystemAccessRule('NT AUTHORITY\LOCAL SERVICE', 'ReadAndExecute', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
)
foreach ($rule in $rules) { $acl.AddAccessRule($rule) }
Set-Acl -LiteralPath $root -AclObject $acl

$python = Join-Path $venv 'Scripts\python.exe'
$serviceArgs = "`"$python`" -m jarvis_control_plane.native_worker_runtime windows-service --config `"$(Join-Path $credentials 'worker.json')`""
New-Service -Name $serviceName -BinaryPathName $serviceArgs -DisplayName 'Jarvis native Windows worker' -Description 'Outbound mTLS Jarvis worker over the private Tailscale overlay' -StartupType Manual | Out-Null
sc.exe failure $serviceName reset= 86400 actions= restart/5000/restart/15000/none/0 | Out-Null
Write-Output "Installed $serviceName with Manual startup; the service was not started."
