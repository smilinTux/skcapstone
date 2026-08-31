# Editable Install Inventory Report
**Card**: ec108b74
**Date**: 2026-08-31
**Fleet**: chiap01, chiap02, chiap03, chiap04, chiap08

## Executive Summary

- **Total editable installs**: 27
- **Host breakdown**:
  - chiap01: 2
  - chiap02: 2
  - chiap03: 1
  - chiap04: 1
  - chiap08: 21
- **Classification**:
  - Service dependencies: 1 (skcomms on chiap04)
  - Development convenience: 4 (skcomms on 3 hosts, smilin-pdf on chiap02)
  - Unknown/Orphaned: 22 (all 21 sklegal packages on chiap08, sklegal-capauth on chiap01)

## Detailed Inventory

### chiap01 (2 installs)

| Package | Version | Location | Classification | Notes |
|---------|---------|----------|----------------|-------|
| skcomms | 0.2.17 | /home/skuser01/work/skcomms | Development convenience | Not running as service |
| sklegal-capauth | 0.1.0 | /mnt/cloud/onedrive/projects/DAVE-AI/sklegal/packages/capauth | Unknown | Part of orphaned worktree |

### chiap02 (2 installs)

| Package | Version | Location | Classification | Notes |
|---------|---------|----------|----------------|-------|
| skcomms | 0.2.17 | /home/skuser01/work/skcomms | Development convenience | Not running as service |
| smilin-pdf | 0.2.0 | /home/skuser01/worktrees/skpdf-5674f675 | Development convenience | Worktree package |

### chiap03 (1 install)

| Package | Version | Location | Classification | Notes |
|---------|---------|----------|----------------|-------|
| skcomms | 0.2.17 | /home/skuser01/work/skcomms | Development convenience | Not running as service |

### chiap04 (1 install) - **SERVICE DEPENDENCY**

| Package | Version | Location | Classification | Service | PID |
|---------|---------|----------|----------------|---------|-----|
| skcomms | 0.2.17 | /home/skuser01/work/skcomms | Service dependency | skcomms serve | 1634955 |

**CRITICAL**: This is the ONLY service-imported editable install in the fleet. The running skcomms serve process imports from this editable install.

### chiap08 (21 installs)

| Package | Version | Location | Classification | Notes |
|---------|---------|----------|----------------|-------|
| skchat-sovereign | 0.14.266.dev26+gb606d822c | /home/skuser01/work/skchat | Unknown | Development version |
| skcomms | 0.2.16 | /home/skuser01/work/skcomms | Unknown | Stale version (0.2.16 vs 0.2.17 elsewhere) |
| sklegal-agents | 0.1.0 | /home/skuser01/work/sklegal-f080-clean/packages/agents | Orphaned | Part of f080-clean worktree |
| sklegal-api | 0.1.0 | /home/skuser01/work/sklegal-f080-clean/services/api | Orphaned | Part of f080-clean worktree |
| sklegal-audit | 0.1.0 | /home/skuser01/work/sklegal-f080-clean/packages/audit | Orphaned | Part of f080-clean worktree |
| sklegal-calendar | 0.1.0 | /home/skuser01/work/sklegal-f080-clean/packages/connectors/calendar | Orphaned | Part of f080-clean worktree |
| sklegal-capauth | 0.1.0 | /home/skuser01/work/sklegal-f080-clean/packages/capauth | Orphaned | Part of f080-clean worktree |
| sklegal-client-communication-connector | 0.1.0 | /home/skuser01/work/sklegal-f080-clean/packages/connectors/client_communication | Orphaned | Part of f080-clean worktree |
| sklegal-connectors-base | 0.1.0 | /home/skuser01/work/sklegal-f080-clean/packages/connectors/base | Orphaned | Part of f080-clean worktree |
| sklegal-domain | 0.1.0 | /home/skuser01/work/sklegal-f080-clean/packages/domain | Orphaned | Part of f080-clean worktree |
| sklegal-email | 0.1.0 | /home/skuser01/work/sklegal-f080-clean/packages/connectors/email | Orphaned | Part of f080-clean worktree |
| sklegal-filing-connector | 0.1.0 | /home/skuser01/work/sklegal-f080-clean/packages/connectors/filing | Orphaned | Part of f080-clean worktree |
| sklegal-hammertime | 0.1.0 | /home/skuser01/work/sklegal-f080-clean/packages/connectors/hammertime | Orphaned | Part of f080-clean worktree |
| sklegal-legal-sources | 0.1.0 | /home/skuser01/work/sklegal-f080-clean/packages/connectors/legal_sources | Orphaned | Part of f080-clean worktree |
| sklegal-migration | 0.1.0 | /home/skuser01/work/sklegal-f080-clean/packages/migration | Orphaned | Part of f080-clean worktree |
| sklegal-model-gateway | 0.1.0 | /home/skuser01/work/sklegal-f080-clean/packages/model_gateway | Orphaned | Part of f080-clean worktree |
| sklegal-persistence | 0.1.0 | /home/skuser01/work/sklegal-f080-clean/packages/persistence | Orphaned | Part of f080-clean worktree |
| sklegal-policies | 0.1.0 | /home/skuser01/work/sklegal-f080-clean/packages/policies | Orphaned | Part of f080-clean worktree |
| sklegal-retrieval | 0.1.0 | /home/skuser01/work/sklegal-f080-clean/packages/retrieval | Orphaned | Part of f080-clean worktree |
| sklegal-service-connector | 0.1.0 | /home/skuser01/work/sklegal-f080-clean/packages/connectors/service | Orphaned | Part of f080-clean worktree |
| sklegal-worker | 0.1.0 | /home/skuser01/work/sklegal-f080-clean/services/worker | Orphaned | Part of f080-clean worktree |
| sklegal-workflow | 0.1.0 | /home/skuser01/work/sklegal-f080-clean/packages/workflow | Orphaned | Part of f080-clean worktree |

## Prioritization

### Priority 1: Critical - Service Dependency
- **skcomms on chiap04**: Currently imported by running service
  - Action: Replace with built wheel or release
  - Target: Zero service-imported editable installs

### Priority 2: High - Known Outage Root Cause
- **sklegal packages on chiap08**: 21 packages from orphaned worktree
  - Action: Either remove (if not used) or rebuild from proper source
  - Risk: Orphaned worktree means no version control visibility

### Priority 3: Medium - Development Convenience
- **skcomms on chiap01, chiap02, chiap03**: Not imported by services
  - Action: Can remain for development if documented
  - Policy: Distinguish developer machines from service hosts

### Priority 4: Low - Cleanup
- **sklegal-capauth on chiap01**: One-off from old worktree
  - Action: Remove or rebuild
- **smilin-pdf on chiap02**: Development package
  - Action: Can remain for development

## Previous Incidents

### Incident 1: chiap04 skdashboard outage (2026-08-30)
- **Cause**: Worker e2a2e808 pip installed -e its workspace into shared .skenv
- **Effect**: When workspace changed, static/overview.html vanished, dashboard returned HTTP 500
- **Resolution**: Replaced with released skdashboard 0.1.91 wheel

### Incident 2: chiap08 sklegal orphaned worktree (2026-08-31)
- **Cause**: 15+ sklegal packages installed from /tmp/sklegal-* directory that was later moved
- **Effect**: Live legal product running from code git could not see
- **Status**: Referenced in card e6096b6e

## Recommendation

1. **Immediate**: Replace skcomms editable install on chiap04 with built version
2. **Short-term**: Audit and replace all sklegal packages on chiap08
3. **Long-term**: Implement guard mechanism to prevent future editable installs into shared interpreters
