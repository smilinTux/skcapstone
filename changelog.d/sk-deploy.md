- **Production stops being a working tree.** `skcapstone deploy` rolls a node
  onto a release manifest kept in its own repository: pre-flight refuses any
  release whose tag is not on its default branch, a fresh non-editable venv is
  built with uv and verified before anything points at it, `~/.skenv` switches
  with one atomic rename, only the units whose packages changed restart, and a
  failed smoke switches back. New doctor checks `deploy:editable`,
  `deploy:exec-in-tree` and `deploy:manifest`, and an `sk-deploy-drift` timer
  that alerts once per change in the findings. `scripts/install.sh` installs
  editable only with `--dev`, and then into `~/.skenv-dev`. See docs/DEPLOY.md.
