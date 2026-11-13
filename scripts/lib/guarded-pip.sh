#!/bin/bash
# guarded-pip.sh: sourced by scripts/install.sh.
#
# install.sh used to run every `pip install` with stderr sent to /dev/null and, on
# any failure, retry without extras or fall back to PyPI. With a constraints file
# in force (PIP_CONSTRAINT, see constraints/security-floors.txt) that turns a
# constraint conflict into a silent downgrade of the install: a node loses the
# skcomms extras or skips sksecurity entirely and still reports success.
#
# guarded_pip_install keeps pip's output and separates the two failure kinds:
#   * dependency or constraint conflict (ResolutionImpossible and friends): print
#     the pip output with a clear message and EXIT THE WHOLE INSTALL with 3. There
#     is no fallback, because the fallback is exactly the silent downgrade.
#   * any other failure (package missing from the index, no network, no such
#     extra): print the tail of pip's output and return 1 so the caller may try its
#     next source.
#
# Requires $PIP to be set to the pip executable.

guarded_pip_install() {
    local log rc=0
    log="$(mktemp "${TMPDIR:-/tmp}/sk-pip.XXXXXX")" || return 1
    "$PIP" install "$@" >"$log" 2>&1 || rc=$?
    if [[ "$rc" -eq 0 ]]; then
        rm -f "$log"
        return 0
    fi
    if grep -qiE 'ResolutionImpossible|conflicting dependencies|Cannot install .* because|constraint' "$log"; then
        {
            echo ""
            echo "ERROR: pip found a dependency or constraint conflict while running:"
            echo "         pip install $*"
            echo "       constraints in force: ${PIP_CONSTRAINT:-none}"
            echo "       pip output (last 40 lines):"
            tail -n 40 "$log" | sed 's/^/         /'
            echo ""
            echo "       Not retrying without extras and not falling back to PyPI: that would"
            echo "       silently install less than you asked for. Fix the conflict (usually a"
            echo "       dependency cap that has not been released yet, see"
            echo "       constraints/security-floors.txt), or set SK_SKIP_SECURITY_FLOORS=1 to"
            echo "       install without the floors and accept the known vulnerabilities."
        } >&2
        rm -f "$log"
        exit 3
    fi
    {
        echo "  pip failed (exit $rc) for: pip install $*"
        tail -n 8 "$log" | sed 's/^/    /'
    } >&2
    rm -f "$log"
    return 1
}
