### Security
- `fleet-qualify` extra: ansible-core 2.19.5 to 2.19.11 on Python < 3.12, for CVE-2026-11332 (GHSA-w8p5-mx5w-cpqj, argument injection in `ansible-galaxy role install`). The Python 3.12+ pin (2.21.3, used by every fleet host and the sealed qualify env) was not affected.
