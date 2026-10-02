- Card `c1a30135`: run source and review inspection through a bounded native
  user service so the hardened coordinator can retain PrivateTmp and
  NoNewPrivileges without inheriting a conflicting user-namespace profile.
  Preserve the bwrap sandbox and fail closed with bounded output and exact-unit
  cleanup.
