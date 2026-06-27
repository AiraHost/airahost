# scripts/monitor_memory.ps1
# PowerShell script to monitor Python and Chrome memory usage over time.

$LogFile = "C:\Users\Aira\Desktop\airahost\scripts\memory_monitor.log"
$IntervalSeconds = 600 # Log every 10 minutes

Write-Host "========================================="
Write-Host "Starting memory monitor."
Write-Host "Logging to $LogFile every $IntervalSeconds seconds..."
Write-Host "========================================="

# Create new log file with header (overwrites any previous log file)
"Timestamp,ProcessName,Count,TotalWorkingSet(MB),TotalPrivateMemory(MB)" | Out-File -FilePath $LogFile -Encoding utf8

while ($true) {
    $Timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    
    # Python statistics
    $PyProcs = Get-Process -Name python -ErrorAction SilentlyContinue
    if ($PyProcs) {
        $PyCount = $PyProcs.Count
        $PyWS = [math]::round(($PyProcs | Measure-Object -Property WorkingSet -Sum).Sum / 1MB, 2)
        $PyPM = [math]::round(($PyProcs | Measure-Object -Property PrivateMemorySize64 -Sum).Sum / 1MB, 2)
        "$Timestamp,python,$PyCount,$PyWS,$PyPM" | Out-File -FilePath $LogFile -Append -Encoding utf8
    } else {
        "$Timestamp,python,0,0,0" | Out-File -FilePath $LogFile -Append -Encoding utf8
    }
    
    # Chrome statistics
    $ChromeProcs = Get-Process -Name chrome -ErrorAction SilentlyContinue
    if ($ChromeProcs) {
        $ChromeCount = $ChromeProcs.Count
        $ChromeWS = [math]::round(($ChromeProcs | Measure-Object -Property WorkingSet -Sum).Sum / 1MB, 2)
        $ChromePM = [math]::round(($ChromeProcs | Measure-Object -Property PrivateMemorySize64 -Sum).Sum / 1MB, 2)
        "$Timestamp,chrome,$ChromeCount,$ChromeWS,$ChromePM" | Out-File -FilePath $LogFile -Append -Encoding utf8
    } else {
        "$Timestamp,chrome,0,0,0" | Out-File -FilePath $LogFile -Append -Encoding utf8
    }
    
    # Sleep until next check
    Start-Sleep -Seconds $IntervalSeconds
}
