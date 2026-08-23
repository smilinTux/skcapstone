# Tailnet browser access from Windows through WSL

## Purpose

CHI Windows workstations keep one canonical Tailscale identity inside WSL.
Windows Chrome and Edge can opt into tailnet web access through a local WSL
HTTP CONNECT proxy without installing or enabling a second Windows Tailscale
identity.

## Security boundary

- The proxy and PAC server bind only to WSL loopback.
- The proxy resolves destinations inside WSL and connects only to Tailscale
  overlay addresses in `100.64.0.0/10` or `fd7a:115c:a1e0::/48`.
- The PAC sends single-label MagicDNS names, `.ts.net` names, and overlay IPv4
  destinations to the proxy. All other browser traffic remains direct.
- No Tailscale key, browser credential, or capability token is copied into the
  Windows scripts or shortcuts.
- The Windows Public Desktop shortcuts change only the invoking profile's
  Internet Settings and preserve the prior values for rollback.

## Install

From a checked-out SKCapstone repository inside the workstation's WSL:

```bash
sudo scripts/fleet/install-tailnet-browser-proxy.sh
```

The installer enables `skcapstone-tailnet-browser-proxy.service` and creates:

- `Enable Tailscale Browser Access.lnk`
- `Disable Tailscale Browser Access.lnk`

under the Windows Public Desktop, making both controls visible to every
Windows profile. Each user must enable or disable access for their own profile.

## Verification

```bash
curl --fail http://127.0.0.1:1056/healthz
curl --fail http://127.0.0.1:1056/proxy.pac
ss -ltnp | rg ':(1055|1056)'
```

Use a temporary approved web endpoint on a remote Tailscale overlay address,
then verify that Windows Chrome or Edge can reach it after Enable is invoked.
Confirm that a public website remains direct and that a non-tailnet private
address receives an HTTP 403 when explicitly sent to the proxy.

The installed acceptance helper launches an isolated headless Chrome profile:

```powershell
& "C:\ProgramData\SKCapstone\TailnetBrowserProxy\Test-TailscaleBrowserAccess.ps1" `
  -TestUrl "http://100.64.0.1:18080/" `
  -ExpectedText "EXPECTED_TEST_MARKER"
```

## Rollback

For every Windows profile that enabled access, run the Disable shortcut first.
Then remove the shared controls and WSL service:

```bash
sudo systemctl disable --now skcapstone-tailnet-browser-proxy.service
sudo rm /etc/systemd/system/skcapstone-tailnet-browser-proxy.service
sudo rm /usr/local/libexec/skcapstone-tailnet-browser-proxy
sudo systemctl daemon-reload
```

Remove the two Public Desktop shortcuts and
`C:\ProgramData\SKCapstone\TailnetBrowserProxy` after verifying that no profile
still has its PAC URL enabled.
