### Fixed

- Rollout now writes the production dispatcher scripts (`skfleet-rotate.py`, `skfleet-worker-wrapper.py`) into `~/.skenv/bin` with the shebang the drift gate pins. pip writes the shebang from the interpreter it ran under (`.../python3` on a Python 3.14 venv), so every 3.14 host halted on `changed:dispatcher` with an identical script body (ziowk01).
