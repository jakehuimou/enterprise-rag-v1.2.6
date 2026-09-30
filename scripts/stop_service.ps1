# stop_service.ps1
# Stop the Enterprise-RAG service (FastAPI backend + Gradio frontend).
# Kills only this project's own processes (matched by command line) and
# releases the listening ports 8000 / 7860 as a fallback.
$ErrorActionPreference = 'SilentlyContinue'
$killed = 0

# 1) Kill project processes by command-line signature (venv python / ragserver only)
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='ragserver.exe'"
foreach ($p in $procs) {
    if ($p.CommandLine -and ($p.CommandLine -like '*run.py*' -or $p.CommandLine -like '*app.main:app*' -or $p.CommandLine -like '*gradio_app*')) {
        Write-Host ("  [STOP] PID " + $p.ProcessId)
        Stop-Process -Id $p.ProcessId -Force
        $killed++
    }
}

# 2) Fallback: free whatever is listening on 8000 / 7860
foreach ($port in @(8000, 7860)) {
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        $pid = $c.OwningProcess
        if ($pid) {
            Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
            Write-Host ("  [PORT] " + $port + " freed (PID " + $pid + ")")
            $killed++
        }
    }
}

if ($killed -eq 0) {
    Write-Host "  No running RAG service process found."
} else {
    Write-Host ("  Done. Handled " + $killed + " item(s).")
}
