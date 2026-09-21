### Fixed

- A card whose `base_ref` is a full ref can be cloned again. `git clone
  --branch` takes a branch or tag NAME, never a ref path, so a `base_ref` of
  `refs/heads/<name>` failed with "Could not find remote branch ... to clone"
  even though the branch existed and `ls-remote` found it. Measured on chiap03
  card `d621aeec`, which read as a dead binding when the only fault was this
  argument.
