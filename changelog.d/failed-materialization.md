### Fixed

- A failed clone no longer blocks its card forever. Materialization reuses an
  existing workspace directory rather than cloning, so a directory left behind
  by a failed clone was permanent: chiap03 held 634 unusable workspaces of 714
  (465 empty, 169 non-git), and card `d621aeec` reported "workspace repository
  does not match card binding" every cycle because its workspace contained
  only an empty nested clone. Such a directory is now discarded and
  re-materialized, but ONLY when it has no remote, no HEAD and a clean tree.
