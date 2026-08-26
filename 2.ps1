[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("CaptureBaseline", "MonitorValidate")]
    [string]$Action,
    [Parameter(Mandatory = $true)][string]$PrimaryServer,
    [Parameter(Mandatory = $true)][string]$ClientsPath,
    [string]$ApiKey = $env:NBU_API_KEY,
    [string]$ApiVersion = "12.0",
    [string]$JobsEndpoint = "/netbackup/admin/jobs",
    [string]$PoliciesEndpoint = "/netbackup/config/policies",
    [string]$PolicyEndpoint = "/netbackup/config/policies/{policyName}",
    [string]$BaselinePath = ".\job-baseline.json",
    [ValidateSet("precheck", "stage", "install")][string]$Operation,
    [string]$Package,
    [string]$OutputDirectory = ".\output",
    [int]$PollSeconds = 15,
    [int]$DiscoveryTimeoutMinutes = 10,
    [int]$JobTimeoutMinutes = 90,
    [int]$MaxBackupAgeHours = 48,
    [switch]$SkipCertificateCheck,
    [string]$MockDataPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
if ([string]::IsNullOrWhiteSpace($ApiKey)) { throw "API key is empty. Use -ApiKey or NBU_API_KEY." }

$Clients = @(Get-Content -LiteralPath $ClientsPath -Encoding UTF8 |
    ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith("#") } | Select-Object -Unique)
if ($Clients.Count -eq 0) { throw "Client file contains no clients." }
foreach ($clientName in $Clients) {
    if ($clientName -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') { throw "Invalid client name: $clientName" }
}
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$MockData = if ($MockDataPath) { Get-Content -LiteralPath $MockDataPath -Raw -Encoding UTF8 | ConvertFrom-Json } else { $null }

if (-not ("NbuTwoScriptHttpV1" -as [type])) {
    Add-Type -TypeDefinition @"
using System; using System.IO; using System.IO.Compression; using System.Net; using System.Text;
public sealed class NbuTwoScriptResponseV1 { public int StatusCode; public string Content; }
public static class NbuTwoScriptHttpV1 {
 public static NbuTwoScriptResponseV1 Get(string uri,string key,string media,int timeout,bool skip) {
  ServicePointManager.SecurityProtocol=SecurityProtocolType.Tls12;
  if(skip) ServicePointManager.ServerCertificateValidationCallback=delegate{return true;};
  HttpWebRequest r=(HttpWebRequest)WebRequest.Create(uri); r.Method="GET"; r.Accept=media;
  r.ContentType=media; r.Headers["Authorization"]=key; r.Timeout=timeout*1000;
  r.AutomaticDecompression=DecompressionMethods.GZip|DecompressionMethods.Deflate;
  try { using(HttpWebResponse p=(HttpWebResponse)r.GetResponse()) using(Stream s=p.GetResponseStream())
   using(StreamReader q=new StreamReader(s,Encoding.UTF8,true)) return new NbuTwoScriptResponseV1{StatusCode=(int)p.StatusCode,Content=q.ReadToEnd()}; }
  catch(WebException e) { if(e.Response==null) throw; using(HttpWebResponse p=(HttpWebResponse)e.Response)
   using(Stream s=p.GetResponseStream()) using(StreamReader q=new StreamReader(s,Encoding.UTF8,true))
   return new NbuTwoScriptResponseV1{StatusCode=(int)p.StatusCode,Content=q.ReadToEnd()}; }
 }
}
"@
}

function Write-JsonFile([string]$Path, $Value) {
    $parent = Split-Path -Parent $Path; if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    [IO.File]::WriteAllText($Path, (ConvertTo-Json $Value -Depth 50) + [Environment]::NewLine,
        (New-Object Text.UTF8Encoding($false)))
}

function Invoke-NbuGet([string]$Path) {
    if ($MockData) {
        $property = $MockData.PSObject.Properties[$Path]
        if (-not $property) { throw "Missing mock GET response: $Path" }
        return $property.Value
    }
    $uri = if ($Path -match '^https://') { $Path } else { "https://$PrimaryServer$Path" }
    $media = "application/vnd.netbackup+json;version=$ApiVersion"
    $response = [NbuTwoScriptHttpV1]::Get($uri, $ApiKey, $media, 120, [bool]$SkipCertificateCheck)
    if ($response.StatusCode -lt 200 -or $response.StatusCode -ge 300) {
        throw "GET $uri failed. HTTP $($response.StatusCode): $($response.Content)"
    }
    return $response.Content | ConvertFrom-Json
}

function Get-Items($Response) {
    if ($null -eq $Response) { return @() }
    if ($Response.PSObject.Properties.Name -contains "data") { return @($Response.data) }
    return @($Response)
}

function Get-PagedItems([string]$Path) {
    $result = New-Object Collections.Generic.List[object]; $next = $Path; $pages = 0
    while ($next -and $pages -lt 100) {
        $response = Invoke-NbuGet $next
        foreach ($item in @(Get-Items $response)) { $result.Add($item) }
        $next = ""
        if ($response.PSObject.Properties.Name -contains "links" -and $response.links -and
            $response.links.PSObject.Properties.Name -contains "next" -and $response.links.next) {
            $next = if ($response.links.next -is [string]) { [string]$response.links.next } else { [string]$response.links.next.href }
        }
        $pages++
    }
    return @($result.ToArray())
}

function Get-Value($Item, [string[]]$Names) {
    $source = if ($Item.PSObject.Properties.Name -contains "attributes") { $Item.attributes } else { $Item }
    foreach ($name in $Names) {
        if ($source.PSObject.Properties.Name -contains $name -and $null -ne $source.$name) { return $source.$name }
    }
    return $null
}

function Convert-Job($Item) {
    $jobId = if ($Item.PSObject.Properties.Name -contains "id" -and $Item.id) { [string]$Item.id }
             else { [string](Get-Value $Item @("jobId", "id")) }
    [PSCustomObject]@{
        Id = $jobId
        ParentId = [string](Get-Value $Item @("parentJobId", "parentId"))
        Type = [string](Get-Value $Item @("jobType", "type"))
        State = [string](Get-Value $Item @("state", "status", "jobState"))
        ExitStatus = [string](Get-Value $Item @("statusCode", "exitStatus", "statusCodeNumber"))
        Client = [string](Get-Value $Item @("clientName", "client", "hostName"))
        StartTime = Get-Value $Item @("startTime", "startTimeUTC", "createdTime")
        EndTime = Get-Value $Item @("endTime", "endTimeUTC", "completedTime")
        Version = [string](Get-Value $Item @("clientVersion", "netBackupVersion", "version"))
    }
}

function Get-Jobs { @(Get-PagedItems $JobsEndpoint | ForEach-Object { Convert-Job $_ }) }

function Get-PolicyMap {
    $map = @{}
    foreach ($item in @(Get-PagedItems $PoliciesEndpoint)) {
        $name = if ($item.PSObject.Properties.Name -contains "id") { [string]$item.id } else { [string](Get-Value $item @("policyName", "name")) }
        if (-not $name) { continue }
        try {
            $path = $PolicyEndpoint.Replace("{policyName}", [Uri]::EscapeDataString($name))
            $policy = (Invoke-NbuGet $path).data.attributes.policy
            foreach ($entry in @($policy.clients)) {
                $clientHost = if ($entry -is [string]) { [string]$entry } else { [string]$entry.hostName }
                if (-not $clientHost) { continue }
                $key = $clientHost.ToLowerInvariant()
                if (-not $map.ContainsKey($key)) { $map[$key] = New-Object Collections.Generic.List[string] }
                $map[$key].Add($name)
            }
        } catch { Write-Warning "Policy '$name' could not be read: $($_.Exception.Message)" }
    }
    return $map
}

if ($Action -eq "CaptureBaseline") {
    if (-not $Operation) { throw "CaptureBaseline requires -Operation." }
    if (-not $Package) { throw "CaptureBaseline requires -Package." }
    $jobs = @(Get-Jobs)
    $policyMap = Get-PolicyMap
    $missing = @($Clients | Where-Object { -not $policyMap.ContainsKey($_.ToLowerInvariant()) })
    if ($missing.Count) { throw "Clients absent from NetBackup policies: $($missing -join ', ')" }
    $baseline = [ordered]@{
        schemaVersion = 1; capturedAt = (Get-Date).ToUniversalTime().ToString("o")
        clients = $Clients; operation = $Operation; package = $Package
        existingJobIds = @($jobs | ForEach-Object Id | Where-Object { $_ })
    }
    Write-JsonFile $BaselinePath $baseline
    Write-Host "Baseline captured: $($baseline.existingJobIds.Count) job IDs; $($Clients.Count) clients."
    exit 0
}

if (-not (Test-Path $BaselinePath)) { throw "Baseline not found: $BaselinePath" }
$baseline = Get-Content $BaselinePath -Raw -Encoding UTF8 | ConvertFrom-Json
$baselineIds = @{}; foreach ($id in @($baseline.existingJobIds)) { $baselineIds[[string]$id] = $true }

$discoveryDeadline = (Get-Date).AddMinutes($DiscoveryTimeoutMinutes); $newDeployment = @()
do {
    $allJobs = @(Get-Jobs)
    $newDeployment = @($allJobs | Where-Object { $_.Id -and -not $baselineIds.ContainsKey($_.Id) -and $_.Type -match '(?i)deploy' })
    if ($newDeployment.Count) { break }
    Start-Sleep $PollSeconds
} while ((Get-Date) -lt $discoveryDeadline)
if (-not $newDeployment.Count) { throw "No new Deployment job was discovered after nbinstallcmd submission." }

$newIds = @{}; foreach ($job in $newDeployment) { $newIds[$job.Id] = $true }
$parentIds = @($newDeployment | Where-Object { -not $_.ParentId -or -not $newIds.ContainsKey($_.ParentId) } |
    Select-Object -ExpandProperty Id -Unique)
if ($parentIds.Count -gt 1) {
    $matchingParents = @($parentIds | Where-Object {
        $candidate = $_; @($newDeployment | Where-Object { $_.ParentId -eq $candidate -and $_.Client -in $Clients }).Count -gt 0
    })
    if ($matchingParents.Count -eq 1) { $parentIds = $matchingParents }
}
if ($parentIds.Count -ne 1) { throw "AmbiguousJobMatch: candidate parent jobs: $($parentIds -join ', ')" }
$parentId = [string]$parentIds[0]

$jobDeadline = (Get-Date).AddMinutes($JobTimeoutMinutes)
do {
    $allJobs = @(Get-Jobs)
    $tracked = @($allJobs | Where-Object { $_.Id -eq $parentId -or $_.ParentId -eq $parentId })
    $active = @($tracked | Where-Object { $_.State -notmatch '(?i)done|complete|finished' })
    if ($tracked.Count -and -not $active.Count) { break }
    Start-Sleep $PollSeconds
} while ((Get-Date) -lt $jobDeadline)

$policyMap = Get-PolicyMap
$backupJobs = @($allJobs | Where-Object { $_.Type -match '(?i)backup' -and $_.Client -and $_.EndTime })
$rows = foreach ($clientName in $Clients) {
    $clientJobs = @($tracked | Where-Object { $_.Client -ieq $clientName })
    $deployment = @($clientJobs | Sort-Object EndTime -Descending | Select-Object -First 1)
    if (-not $deployment.Count) { $deployment = @($tracked | Where-Object Id -eq $parentId | Select-Object -First 1) }
    $backup = @($backupJobs | Where-Object Client -ieq $clientName | Sort-Object EndTime -Descending | Select-Object -First 1)
    $backupReady = $false; $backupError = "No completed backup found."
    if ($backup.Count) {
        $age = (Get-Date).ToUniversalTime() - ([datetime]$backup[0].EndTime).ToUniversalTime()
        $backupReady = $backup[0].ExitStatus -eq "0" -and $age.TotalHours -le $MaxBackupAgeHours
        $backupError = if ($backupReady) { "" } elseif ($backup[0].ExitStatus -ne "0") { "Latest backup status is $($backup[0].ExitStatus)." } else { "Latest backup is too old." }
    }
    $policyPresent = $policyMap.ContainsKey($clientName.ToLowerInvariant())
    $deploymentPassed = $deployment.Count -and $deployment[0].ExitStatus -eq "0"
    [PSCustomObject][ordered]@{
        Client=$clientName; Operation=$baseline.operation; Package=$baseline.package; ParentJobId=$parentId
        JobId=if($clientJobs.Count){$clientJobs[0].Id}else{$parentId}; JobState=if($deployment.Count){$deployment[0].State}else{"NotFound"}
        JobExitStatus=if($deployment.Count){$deployment[0].ExitStatus}else{""}; PolicyConfigured=$policyPresent
        Policies=if($policyPresent){$policyMap[$clientName.ToLowerInvariant()] -join ";"}else{""}
        DetectedVersion=if($deployment.Count){$deployment[0].Version}else{""}; BackupReadiness=if($backupReady){"Passed"}else{"Failed"}
        LatestBackupTime=if($backup.Count){$backup[0].EndTime}else{""}; LatestBackupStatus=if($backup.Count){$backup[0].ExitStatus}else{""}
        OverallStatus=if($policyPresent -and $deploymentPassed -and $backupReady){"Passed"}else{"Failed"}
        Error=(@(if(-not $policyPresent){"Policy missing."};if(-not $deploymentPassed){"Deployment failed or missing."};if($backupError){$backupError}) -join " ")
    }
}
$jsonReport = Join-Path $OutputDirectory "migration-report.json"; $csvReport = Join-Path $OutputDirectory "migration-report.csv"
Write-JsonFile $jsonReport @($rows); @($rows) | Export-Csv $csvReport -NoTypeInformation -Encoding UTF8
Write-Host "Discovered parent job $parentId. Reports: $csvReport and $jsonReport"
if (@($rows | Where-Object OverallStatus -eq "Failed").Count) { exit 2 }
exit 0
