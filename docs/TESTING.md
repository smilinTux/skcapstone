# Testing Guide

This document describes SKCapstone's two-lane testing approach: hermetic unit tests for default/local/PR runs, and a separate qualification lane for integration and host-backed tests.

## Test Lanes

### Hermetic Lane (Default)

The hermetic lane runs by default in CI, locally, and on PRs. These tests:

- Require no live SK services, identities, providers, or network
- Use only in-memory fixtures and mocked external dependencies
- Bind no TCP ports or sockets
- Spawn no real processes (except for isolated test fixtures like GPG key generation)
- Complete in under 5 minutes on a clean runner
- Provide fast feedback on code changes

**Run hermetic tests:**

```bash
# Default pytest run - hermetic only
pytest tests/

# Explicit hermetic run
pytest tests/ -m "not integration and not e2e and not network"

# With coverage
pytest tests/ -m "not integration and not e2e and not network" --cov=skcapstone
```

**What runs in the hermetic lane:** 6,513 tests

**Markers excluded from hermetic lane:**

- `integration` - cross-component tests requiring real services/network
- `e2e` - live end-to-end tests requiring installed CLI/running daemon
- `network` - tests requiring network stack, sockets, or TCP ports

### Qualification Lane

The qualification lane runs the full test matrix including integration, e2e, and network-dependent tests. These tests:

- Require a qualified host with Docker, systemd, Syncthing, or other services
- May bind TCP ports and create real network connections
- May spawn real subprocesses
- Validate integration points and host-specific behavior
- Take 10-30 minutes depending on host configuration

**Run qualification tests:**

```bash
# Run all tests (hermetic + qualification)
pytest tests/

# Run only qualification tests
pytest tests/ -m "integration or e2e or network"

# Run specific qualification categories
pytest tests/ -m network          # Network/socket tests
pytest tests/ -m integration      # Integration tests
pytest tests/ -m e2e              # End-to-end tests

# Run via qualification script (validates prerequisites first)
bash scripts/test-qualification.sh
```

**What runs in the qualification lane:** 185 tests

## Prerequisites for Qualification Lane

Before running qualification tests, ensure your host has:

- Docker daemon running (for Docker provider tests)
- systemd user session available (for systemd tests)
- Syncthing installed (for sync backend tests, optional)
- Network access allowed (for network tests)
- Free TCP ports in dynamic range (49152-65535)

The `scripts/test-qualification.sh` script validates these prerequisites before execution.

## Test Markers

The following pytest markers are defined in `pyproject.toml`:

| Marker | Description | Count | Hermetic? |
|--------|-------------|-------|-----------|
| (none) | Default unit tests | 6,513 | Yes |
| `network` | Tests requiring network stack/sockets | 159 | No |
| `integration` | Cross-component tests with real services | 26 | No |
| `e2e` | Live end-to-end tests with installed CLI | 7 | No |

## Network-Dependent Tests

Tests marked with `@pytest.mark.network` include:

- `tests/test_daemon.py` - HTTP daemon with real server and `urllib` requests
- `tests/test_dashboard.py` - Dashboard HTTP server and client connections
- `tests/test_ws.py` - WebSocket connections via real TCP sockets
- `tests/fleet/test_operator_http.py` - Operator-plane HTTP surface

These tests bind to free TCP ports on 127.0.0.1 and make real HTTP/WebSocket connections. They are excluded from the hermetic lane but can run on any host with a standard network stack.

## Integration Tests

Tests marked with `@pytest.mark.integration` are in `tests/integration/`:

- `test_consciousness_e2e.py` - Full consciousness pipeline
- `test_notification_e2e.py` - Notification system end-to-end
- `test_skills_registry.py` - Skills discovery from filesystem

These tests exercise real component interactions and may require specific SK services or configurations.

## End-to-End Tests

Tests marked with `@pytest.mark.e2e` require a fully installed and configured SKCapstone:

- `tests/test_e2e_automated.py` - Live daemon with real agent home

These tests are skipped by default and require explicit opt-in:

```bash
SKCAPSTONE_E2E=1 pytest tests/test_e2e_automated.py -v -s --timeout=360
```

## CI Configuration

### GitHub Actions

The `.github/workflows/pytest.yml` workflow runs only hermetic tests:

```yaml
python -m pytest tests/ \
  --strict-markers \
  -m "not integration and not e2e and not network" \
  --cov=skcapstone --cov-report=xml --cov-report=term-missing
```

This ensures PR checks complete quickly and don't fail due to unavailable host services.

### Local Development

When developing locally:

1. Run hermetic tests frequently during development:
   ```bash
   pytest tests/ -q
   ```

2. Run qualification tests before committing significant changes:
   ```bash
   bash scripts/test-qualification.sh
   ```

3. Run a specific test file:
   ```bash
   pytest tests/test_daemon.py -v  # Runs all tests in file (may be non-hermetic)
   pytest tests/test_daemon.py -v -m "not network"  # Hermetic only
   ```

## Process Cleanup and Timeouts

Per card ed7155a9, all process-spawning tests must:

1. Have a bounded timeout (pytest-timeout plugin or explicit deadline)
2. Implement termination escalation (SIGTERM → SIGKILL)
3. Assert process cleanup (no orphan processes after test)
4. Provide machine-readable skip/failure reasons

Example pattern:

```python
def test_with_child_process(tmp_path):
    proc = subprocess.Popen([...])
    try:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, f"Child failed: {err}"
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    # Assert no orphan processes
    assert not _has_orphan_processes()
```

## Adding New Tests

When adding new tests:

1. **Default to hermetic**: Use `tmp_path` fixtures, mocks, and in-memory dependencies
2. Mark non-hermetic tests: Add `@pytest.mark.network`, `@pytest.mark.integration`, or `@pytest.mark.e2e`
3. Document external requirements: Add comments explaining what services/ports are needed
4. Ensure cleanup: All spawned processes must be terminated with timeouts and assertions

**Hermetic test example:**

```python
def test_hermetic_unit(tmp_path):
    """Pure unit test with no external dependencies."""
    result = my_function(tmp_path / "test.txt")
    assert result == expected
```

**Network test example:**

```python
import pytest

pytestmark = pytest.mark.network

def test_http_server(tmp_path):
    """Starts real HTTP server and connects to it."""
    port = _find_free_port()
    with start_server(port):
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/")
        assert resp.status == 200
```

## Troubleshooting

### Hermetic tests fail due to "address already in use"

This indicates a test is not properly hermetic (it's binding a port). Check:
- Does the test or its fixtures call `socket.bind()`, `socket.listen()`, or `start_server()`?
- Is the test file missing the `@pytest.mark.network` marker?

### Qualification tests fail with "connection refused"

The required service is not running. Check:
- Docker: `docker ps`
- systemd: `systemctl --user status`
- Syncthing: `syncthing --version`

### Tests hang indefinitely

A spawned process is not terminating. Check:
- Does the test use `proc.communicate(timeout=N)`?
- Is there a `finally` block with `proc.terminate()`/`proc.kill()`?
- Is there a cleanup assertion checking for orphan processes?

## See Also

- `pyproject.toml` - Pytest configuration and markers
- `.github/workflows/pytest.yml` - CI hermetic test run
- `scripts/test-qualification.sh` - Qualification lane runner
- Card ed7155a9 - Hermetic test separation implementation
