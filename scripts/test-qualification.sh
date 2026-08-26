#!/usr/bin/env bash
# scripts/test-qualification.sh - Qualification lane runner with prerequisite validation
#
# This script runs the full SKCapstone test matrix including integration,
# e2e, and network-dependent tests. It validates prerequisites before execution
# and provides machine-readable receipts for test results.
#
# Usage:
#   bash scripts/test-qualification.sh [OPTIONS]
#
# Options:
#   --only-hermetic       Run only hermetic tests (same as default pytest)
#   --only-network        Run only network-dependent tests
#   --only-integration    Run only integration tests
#   --only-e2e            Run only e2e tests
#   --skip-prereq-check   Skip prerequisite validation (not recommended)
#   --verbose             Enable verbose pytest output
#   --help                Show this help message
#
# Exit codes:
#   0  - All tests passed
#   1  - Prerequisite check failed
#   2  - Tests failed
#   3  - Tests timed out
#   4  - Invalid arguments
#
# See also: docs/TESTING.md, card ed7155a9

set -euo pipefail

# ---------------------------------------------------------------------------
# Colors and formatting
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

pass() { echo -e "${GREEN}[PASS]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
info() { echo -e "${BLUE}[INFO]${NC} $*"; }

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
SKIP_PREREQ_CHECK=false
VERBOSE=false
PYTEST_ARGS=""
MARKER_EXPRESSION=""

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case $1 in
        --only-hermetic)
            MARKER_EXPRESSION="not integration and not e2e and not network"
            shift
            ;;
        --only-network)
            MARKER_EXPRESSION="network"
            shift
            ;;
        --only-integration)
            MARKER_EXPRESSION="integration"
            shift
            ;;
        --only-e2e)
            MARKER_EXPRESSION="e2e"
            shift
            ;;
        --skip-prereq-check)
            SKIP_PREREQ_CHECK=true
            shift
            ;;
        --verbose)
            VERBOSE=true
            PYTEST_ARGS="${PYTEST_ARGS} -vv -s"
            shift
            ;;
        --help)
            grep '^#' "$0" | tail -n +2 | sed 's/^# //; s/^#//'
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use --help for usage information"
            exit 4
            ;;
    esac
done

# If no marker specified, run all tests (full qualification)
if [[ -z "${MARKER_EXPRESSION}" ]]; then
    MARKER_EXPRESSION="not (not integration and not e2e and not network)"
fi

# ---------------------------------------------------------------------------
# Prerequisite validation
# ---------------------------------------------------------------------------
PREREQ_FAILED=0

check_docker() {
    if ! command -v docker &> /dev/null; then
        warn "Docker not found - Docker provider tests will be skipped"
        return 0
    fi

    if ! docker info &> /dev/null; then
        fail "Docker daemon not running - Docker tests will fail"
        PREREQ_FAILED=1
        return 1
    fi

    pass "Docker daemon is running"
    return 0
}

check_systemd() {
    if ! command -v systemctl &> /dev/null; then
        info "systemd not available - systemd tests will be skipped"
        return 0
    fi

    if ! systemctl --user list-units &> /dev/null; then
        warn "systemd user session not available - systemd tests may fail"
        return 0
    fi

    pass "systemd user session is available"
    return 0
}

check_syncthing() {
    if ! command -v syncthing &> /dev/null; then
        info "Syncthing not installed - sync backend tests will be skipped"
        return 0
    fi

    pass "Syncthing is installed"
    return 0
}

check_network() {
    # Check if we can bind to a TCP port
    local port
    port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("", 0)); print(s.getsockname()[1]); s.close()' 2>/dev/null || echo "")
    if [[ -z "${port}" ]]; then
        fail "Cannot bind TCP ports - network tests will fail"
        PREREQ_FAILED=1
        return 1
    fi

    pass "Network stack is available (can bind to TCP ports)"
    return 0
}

check_python() {
    if ! command -v python3 &> /dev/null; then
        fail "python3 not found"
        PREREQ_FAILED=1
        return 1
    fi

    local version
    version=$(python3 --version | awk '{print $2}')
    pass "Python ${version} is available"
    return 0
}

check_pytest() {
    if ! command -v pytest &> /dev/null; then
        fail "pytest not found - install with: pip install pytest"
        PREREQ_FAILED=1
        return 1
    fi

    pass "pytest is available"
    return 0
}

check_package() {
    if ! python3 -c "import skcapstone" 2>/dev/null; then
        fail "skcapstone not installed - install with: pip install -e ."
        PREREQ_FAILED=1
        return 1
    fi

    local version
    version=$(python3 -c "import skcapstone; print(skcapstone.__version__)" 2>/dev/null || echo "unknown")
    pass "skcapstone ${version} is installed"
    return 0
}

run_prereq_checks() {
    info "Running prerequisite checks..."

    check_python
    check_pytest
    check_package
    check_network
    check_docker
    check_systemd
    check_syncthing

    if [[ ${PREREQ_FAILED} -eq 1 ]]; then
        echo ""
        fail "Prerequisite check failed - aborting"
        echo ""
        echo "To skip prerequisite checks (not recommended), use: --skip-prereq-check"
        exit 1
    fi

    echo ""
    pass "All prerequisite checks passed"
    echo ""
}

# ---------------------------------------------------------------------------
# Test execution
# ---------------------------------------------------------------------------
run_tests() {
    info "Starting qualification test run..."
    info "Marker expression: ${MARKER_EXPRESSION}"
    echo ""

    local pytest_cmd
    pytest_cmd="python3 -m pytest tests/ --strict-markers -m \"${MARKER_EXPRESSION}\" ${PYTEST_ARGS}"

    info "Running: ${pytest_cmd}"
    echo ""

    # Run pytest and capture exit code
    if eval "${pytest_cmd}"; then
        echo ""
        pass "All tests passed"
        return 0
    else
        local exit_code=$?
        echo ""
        if [[ ${exit_code} -eq 124 ]]; then
            fail "Tests timed out (exit code 124)"
            return 3
        else
            fail "Tests failed (exit code ${exit_code})"
            return 2
        fi
    fi
}

# ---------------------------------------------------------------------------
# Receipt generation
# ---------------------------------------------------------------------------
generate_receipt() {
    local receipt_file
    receipt_file=".skcapstone/test-receipt-$(date +%Y%m%d-%H%M%S).json"

    info "Generating test receipt: ${receipt_file}"

    cat > "${receipt_file}" <<EOF
{
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "marker_expression": "${MARKER_EXPRESSION}",
  "python_version": "$(python3 --version)",
  "skcapstone_version": "$(python3 -c 'import skcapstone; print(skcapstone.__version__)' 2>/dev/null || echo 'unknown')",
  "exit_code": ${1},
  "hostname": "$(hostname)",
  "user": "${USER:-unknown}",
  "prereq_checks_skipped": ${SKIP_PREREQ_CHECK}
}
EOF

    pass "Receipt written to ${receipt_file}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    echo -e "${BLUE}=== SKCapstone Qualification Test Runner ===${NC}"
    echo ""

    if [[ "${SKIP_PREREQ_CHECK}" == "false" ]]; then
        run_prereq_checks
    else
        warn "Skipping prerequisite checks (--skip-prereq-check)"
        echo ""
    fi

    local test_exit_code=0
    if run_tests; then
        test_exit_code=0
    else
        test_exit_code=$?
    fi

    generate_receipt "${test_exit_code}"

    echo ""
    echo -e "${BLUE}=== Qualification run complete ===${NC}"
    echo ""

    exit ${test_exit_code}
}

# Run main
main "$@"
