### Fixed

- A builder generation whose worker exited with no outcome of its own and left its workspace clean at the authorized base now releases its claim and gets a fresh offer, instead of waiting in `awaiting-evidence` forever. Any commit, edit, untracked file or owner outcome still keeps custody.
