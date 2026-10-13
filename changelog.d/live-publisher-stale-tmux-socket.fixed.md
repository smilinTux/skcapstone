- Treat stale tmux socket files as absent only after checking the kernel Unix
  listener table. An absent default socket can coexist with fully audited live
  runtime servers; hidden or unmapped servers and failed live probes still
  refuse authoritative publication. No socket files are removed or servers reset.
