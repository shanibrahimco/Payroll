# Copies the server's daily backups to this computer, so a copy always exists away from the server.
# Example:  .\deploy\download_backups.ps1 -Server 203.0.113.10 -Destination "D:\Payroll Backups"
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [Parameter(Mandatory = $true)][string]$Destination,
    [string]$User = "root"
)

New-Item -ItemType Directory -Force -Path $Destination | Out-Null
scp -p "${User}@${Server}:/srv/payroll/backups/payroll_backup_*.zip" "$Destination"
if ($LASTEXITCODE -ne 0) { throw "The backups could not be downloaded." }
Write-Output "Backups copied to $Destination"
