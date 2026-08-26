[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PrimaryServer,
    [Parameter(Mandatory = $true)][string]$ClientsPath,
    [Parameter(Mandatory = $true)]
    [ValidateSet("precheck", "stage", "install")]
    [string]$Operation,
    [Parameter(Mandatory = $true)][string]$Package,
    [Parameter(Mandatory = $true)][string]$ApiKey,
    [string]$ApiVersion = "12.0",
    [string]$BaselinePath = ".\job-baseline.json",
    [string]$JobsEndpoint = "/netbackup/admin/jobs",
    [string]$PoliciesEndpoint = "/netbackup/config/policies",
    [string]$PolicyEndpoint = "/netbackup/config/policies/{policyName}",
    [switch]$SkipCertificateCheck
)

$ErrorActionPreference = "Stop"
$monitorScript = Join-Path $PSScriptRoot "Monitor-NetBackupMigration.ps1"
if (-not (Test-Path -LiteralPath $monitorScript)) {
    throw "Required monitoring script not found: $monitorScript"
}

$arguments = @{
    Action = "CaptureBaseline"
    PrimaryServer = $PrimaryServer
    ClientsPath = $ClientsPath
    Operation = $Operation
    Package = $Package
    ApiKey = $ApiKey
    ApiVersion = $ApiVersion
    BaselinePath = $BaselinePath
    JobsEndpoint = $JobsEndpoint
    PoliciesEndpoint = $PoliciesEndpoint
    PolicyEndpoint = $PolicyEndpoint
}
if ($SkipCertificateCheck) { $arguments.SkipCertificateCheck = $true }

& $monitorScript @arguments
exit $LASTEXITCODE

