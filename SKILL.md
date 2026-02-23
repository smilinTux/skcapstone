# SKCapstone - Sovereign Agent Framework

The complete sovereign agent framework with identity, memory, trust,
security, and P2P sync.

## Install

```bash
pip install skcapstone
skcapstone init --name <agent-name>
```

## Commands

- `skcapstone status` -- show all pillar status
- `skcapstone init --name NAME` -- initialize a new agent
- `skcapstone sync setup` -- set up Syncthing for P2P memory sync
- `skcapstone sync pair DEVICE-ID` -- pair with another device
- `skcapstone sync push` -- push state to sync mesh
- `skcapstone sync pull` -- pull state from peers
- `skcapstone audit` -- run security audit
- `skcapstone token issue --subject NAME --cap CAPABILITY` -- issue auth token

## Pillars

| Pillar   | Purpose               |
| -------- | --------------------- |
| Identity | CapAuth GPG identity  |
| Memory   | Persistent agent memory |
| Trust    | Cloud 9 + FEB + OOF   |
| Security | Audit + threat detection |
| Sync     | Sovereign Singularity P2P |

## Author

smilinTux -- staycuriousANDkeepsmilin
