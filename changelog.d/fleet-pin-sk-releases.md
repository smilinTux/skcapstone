### Changed

- **The fleet installs released builds of the rest of the SK set.** `deploy/fleet-constraints.txt`
  now pins capauth, skseed, skdashboard, skharness, skskills and skcomms at their PyPI releases, so
  the auto-rollout replaces the git and editable builds chi hosts ran. Each pin was dry-run in every
  rollout host's live venv and moves only the SK package. skchat-sovereign, sksecurity, skbrain and
  skvault are held back because they would move third-party packages (Pillow, fastapi,
  cryptography, mcp, click, typer) under running services.
