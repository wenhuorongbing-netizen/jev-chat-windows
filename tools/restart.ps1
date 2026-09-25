# Restart when running from source: stop every process of this app, then start a fresh one with pythonw.
# Pass -stop to only stop. ASCII only: Windows PowerShell 5 reads BOM-less files as the ANSI code page.
# Process chain: venv launcher (command line has jev-chat-windows) -> base interpreter running main.py
# -> two capture workers (spawn_main). Only that chain, plus orphaned workers whose parent is gone, is killed.
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$procs = @(Get-CimInstance Win32_Process -Filter "Name like 'python%'")
$ours = @($procs | Where-Object { $_.CommandLine -match 'jev-chat-windows' } | ForEach-Object { $_.ProcessId })
$mains = @($procs | Where-Object { $ours -contains $_.ParentProcessId -and $_.CommandLine -match '\bmain\.py\b' } | ForEach-Object { $_.ProcessId })
$kids = @($procs | Where-Object {
    $_.CommandLine -match 'spawn_main\(parent_pid=(\d+)' -and
    (($mains -contains [int]$Matches[1]) -or -not (Get-Process -Id ([int]$Matches[1]) -ErrorAction SilentlyContinue))
} | ForEach-Object { $_.ProcessId })
$kids + $mains + $ours | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 800
if ($args -notcontains '-stop') {
    Start-Process -FilePath (Join-Path $root '.venv\Scripts\pythonw.exe') -ArgumentList 'main.py' -WorkingDirectory $root
}
