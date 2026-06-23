# scripts/restart_chrome_rolling.ps1
# PowerShell script to perform a rolling restart of the Chrome CDP instances.

$Ports = @(9222, 9223, 9224)
$ChromePath = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$UserProfile = $env:USERPROFILE
$Timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Write-Host "========================================="
Write-Host "Script Execution Time: $Timestamp"
Write-Host "========================================="

foreach ($Port in $Ports) {
    Write-Host "========================================="
    Write-Host "Restarting Chrome instance on port $Port..."
    Write-Host "========================================="
    
    # Locate the Chrome process running with the specific remote debugging port
    # Query CommandLine to find the exact debugging port instance
    $Processes = Get-CimInstance Win32_Process -Filter "Name = 'chrome.exe'" | Where-Object { 
        $_.CommandLine -like "*--remote-debugging-port=$Port*" 
    }
    
    if ($Processes) {
        foreach ($Proc in $Processes) {
            Write-Host "Found Chrome process (PID: $($Proc.ProcessId)). Terminating..."
            try {
                Stop-Process -Id $Proc.ProcessId -Force -ErrorAction SilentlyContinue
            } catch {
                # Process might have already exited
            }
        }
        # Wait a moment for OS to clean up processes
        Start-Sleep -Seconds 2
    } else {
        Write-Host "No active Chrome instance found on port $Port."
    }
    
    # Define user data directory
    $DataDir = "$UserProfile\chrome-cdp-profile-$Port"
    
    # Start Chrome again in the background
    Write-Host "Starting Chrome on port $Port with data-dir $DataDir..."
    $ArgsList = @(
        "--remote-debugging-port=$Port",
        "--user-data-dir=$DataDir",
        "--no-first-run",
        "--no-default-browser-check",
        "--blink-settings=imagesEnabled=false" # Optional: disable images to save memory/bandwidth
    )
    
    Start-Process -FilePath $ChromePath -ArgumentList $ArgsList -WindowStyle Minimized
    
    # Wait for Chrome to warm up before moving to the next instance
    Write-Host "Waiting 10 seconds for Chrome port $Port to bind and warm up..."
    Start-Sleep -Seconds 10
}

Write-Host "========================================="
Write-Host "Rolling restart of all Chrome instances completed successfully!"
Write-Host "========================================="
