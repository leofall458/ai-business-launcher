"""Best-effort Windows notification for scripts that only ever run on
Leo's machine (the poller, the SCC status checker) - shells out to
powershell.exe, which WSL can invoke directly via its Windows interop.
Never raises - a failed notification must never take down whatever
long-running loop is calling it."""

import subprocess

def notify_windows(title: str, message: str):
    safe_title = title.replace("'", "''")
    safe_message = message.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon = [System.Drawing.SystemIcons]::Information; "
        "$n.Visible = $true; "
        f"$n.ShowBalloonTip(8000, '{safe_title}', '{safe_message}', "
        "[System.Windows.Forms.ToolTipIcon]::Info); "
        "Start-Sleep -Seconds 9; "
        "$n.Dispose()"
    )
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], timeout=15, capture_output=True)
    except Exception as e:
        print(f"⚠️ Could not send Windows notification: {e}")
