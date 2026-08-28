# Safe ChatGPT Restart Procedure for chiap04

**Card:** 61bc82e3
**Purpose**: Document safe restart, update, and install procedures that preserve GNOME Terminal and unrelated Codex CLI sessions
**Based on**: Root cause analysis of terminal-closure regression (OOM during installation)

## Critical Safety Rules

**NEVER** use these commands to restart ChatGPT on Linux:
```bash
# DANGEROUS - may kill unrelated terminals or processes
pkill -f chatgpt
killall chatgpt
pkill -f codex
systemctl stop gnome-terminal-server
killall gnome-terminal-server
```

**ALWAYS** use the targeted procedures below.

## Linux Desktop Safe Restart

### Method 1: Application Menu (Recommended)

1. In the ChatGPT window, click **File** → **Quit** (or use `Ctrl+Q`)
2. Reopen ChatGPT from the application launcher
3. Terminal windows remain unaffected

### Method 2: Targeted Process Termination (If menu unavailable)

```bash
# Identify ONLY the ChatGPT main process
pgrep -a -f '/usr/lib/chatgpt/ChatGPT'

# Terminate ONLY the ChatGPT process by PID
kill <chatgpt_pid>

# Restart ChatGPT
chatgpt
```

**Verification**: After restart, confirm terminals are still running:
```bash
pgrep -a -f 'gnome-terminal'
pgrep -a -f 'vte-spawn'
```

## Linux Desktop Safe Update

### Pre-Update Memory Check

```bash
# Check available memory
free -h

# If available memory < 2GB, close unnecessary terminals first
ps aux | grep -E 'gnome-terminal|vte-spawn' | awk '{print $2, $4, $11}' | sort -k2 -rn | head -10
```

### Update Procedure

```bash
# Update package list
sudo apt update

# Upgrade ONLY ChatGPT package
sudo apt install --only-upgrade chatgpt

# If a specific version is required (pinned update)
sudo apt install chatgpt=<version>
```

**Do NOT** use these during update:
```bash
# DANGEROUS - stops all terminals
sudo systemctl restart gnome-terminal-server
```

## Linux Desktop Fresh Install

### Pre-Installation Checklist

```bash
# 1. Check memory availability
free -h

# 2. Identify high-memory terminal processes
ps aux | grep -E 'gnome-terminal|vte-spawn' | awk '{print $2, $4, $11}' | sort -k2 -rn | head -10

# 3. If memory is constrained, close non-essential terminals
# Close from GUI: right-click terminal window → Close

# 4. Verify sufficient free memory (recommended: 2GB+)
free -h | grep '^Mem:' | awk '{if ($4 < 2000000) print "WARNING: Low memory"}'
```

### Installation Procedure

```bash
# Navigate to download location
cd ~/Downloads

# Install the package
sudo apt install ./chatgpt_amd64.deb

# Start ChatGPT
chatgpt
```

## Windows with WSL2 Safe Restart

### Safe PowerShell Procedure

```powershell
# Capture terminal PIDs before restart
$terminalPidsBefore = @(
    Get-Process WindowsTerminal -ErrorAction SilentlyContinue
).Id | Sort-Object

# Stop ONLY ChatGPT process
Get-Process ChatGPT -ErrorAction SilentlyContinue | Stop-Process -Force

# Restart ChatGPT
$app = Get-StartApps | Where-Object Name -eq 'ChatGPT' | Select-Object -First 1
if ($null -eq $app) {
    throw 'ChatGPT is not registered in the Start menu'
}
Start-Process explorer.exe "shell:AppsFolder\$($app.AppID)"

# Verify terminals survived
$terminalPidsAfter = @(
    Get-Process WindowsTerminal -ErrorAction SilentlyContinue
).Id | Sort-Object
if (Compare-Object $terminalPidsBefore $terminalPidsAfter) {
    Write-Warning 'Windows Terminal process set changed during ChatGPT restart'
} else {
    Write-Host 'Windows Terminal processes preserved successfully'
}
```

### Safe Update Procedure (Windows)

```powershell
# Update WSL packages INSIDE WSL
wsl -d Ubuntu -- bash -c 'sudo apt update && sudo apt install --only-upgrade chatgpt'

# Restart ChatGPT from Windows using the safe procedure above
```

## Rollback Procedure

### Linux Rollback to Previous Version

```bash
# Identify currently installed version
dpkg-query -W -f='${Version}\n' chatgpt

# Rollback to previous version (if still in apt cache)
sudo apt install chatgpt=<previous_version>

# Or reinstall from backed up .deb file
sudo apt install /path/to/chatgpt_<previous_version>_amd64.deb
```

### Windows/WSL Rollback

```powershell
# Rollback INSIDE WSL
wsl -d Ubuntu -- bash -c 'sudo apt install chatgpt=<previous_version>'

# Restart ChatGPT using safe PowerShell procedure
```

## Canary Testing Procedure

To verify that terminals survive ChatGPT operations:

### Setup Protected Terminal Canaries

```bash
# Terminal 1: Start a long-running canary process
echo "CANARY_TERMINAL_1 $$ $(date)" && sleep 3600

# Terminal 2: Start another canary
echo "CANARY_TERMINAL_2 $$ $(date)" && sleep 3600

# Terminal 3: Run Codex CLI session
codex --help
```

### Test ChatGPT Operations

In a fourth terminal:
```bash
# 1. Check canary PIDs
pgrep -a -f 'CANARY_TERMINAL'

# 2. Restart ChatGPT using safe procedure
# ... (use method 1 or 2 above) ...

# 3. Verify canaries still running
pgrep -a -f 'CANARY_TERMINAL'

# 4. Update ChatGPT
sudo apt install --only-upgrade chatgpt

# 5. Verify canaries still running
pgrep -a -f 'CANARY_TERMINAL'
```

### Acceptance Criteria

- Both canary terminals must survive all operations
- PIDs must remain unchanged
- No `gnome-terminal-server` restart in journalctl
- No OOM events in journalctl during operations

## Troubleshooting

### Symptom: Terminals Close During Update

**Cause**: Memory exhaustion triggering OOM killer
**Solution**:
```bash
# Check memory before operations
free -h

# Close high-memory terminals
ps aux | grep -E 'gnome-terminal|vte-spawn' | awk '{print $2, $4, $11}' | sort -k2 -rn

# If needed, increase swap space
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

### Symptom: ChatGPT Won't Start After Update

**Cause**: Process still running from previous instance
**Solution**:
```bash
# Check for lingering ChatGPT processes
pgrep -a -f 'ChatGPT'

# Terminate if found (by PID only)
kill <chatgpt_pid>

# Restart
chatgpt
```

### Symptom: Update Fails with Dependency Errors

**Solution**:
```bash
sudo apt --fix-broken install
sudo apt update
sudo apt install --only-upgrade chatgpt
```

## Monitoring

### Check for OOM Events After Operations

```bash
# Check recent OOM kills
sudo journalctl --since "1 hour ago" | grep -i "oom-kill"

# Check for gnome-terminal-server restarts
journalctl --user --since "1 hour ago" | grep "gnome-terminal-server"
```

### Monitor Memory During Operations

In a separate terminal:
```bash
watch -n 1 'free -h && echo "---" && ps aux | grep -E "chatgpt|gnome-terminal" | head -10'
```

## References

- Root cause analysis: `evidence/work/61bc82e3/terminal-closure-regression-root-cause.md`
- Main runbook: `docs/runbooks/chatgpt-codex-sk-client.md`
- chiap04 qualification: `docs/evidence/chatgpt-client/CGC-S1-03-CHIAP04-QUALIFICATION-2026-08-22.md`

---

**Document Version**: 1.0
**Last Updated**: 2026-08-28
**Evidence Hash**: $(sha256sum /home/skuser01/.skcapstone/evidence/work/61bc82e3/safe-restart-procedure.md | awk '{print $1}')
