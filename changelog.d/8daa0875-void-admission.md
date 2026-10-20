- Card `8daa0875`: the coord gate diagnostic now reads raw void events for a
  card's dependencies, so a dependency voided by event is reported as an
  unresolved `dependency` instead of being treated as satisfied (port of #590;
  the leaf-eligibility and voided-card paths are already correct on main and are
  now covered by regression tests).
