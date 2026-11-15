### Security
- Fleet lock: Brotli 1.2.1 and brotlicffi 1.2.1.0 floors (CVE-2026-111497, Brotli decoder out-of-bounds write). Both are now on the security-downgrade list, and the gate audits a known-vulnerable canary first, so it fails if pip-audit cannot see them.
