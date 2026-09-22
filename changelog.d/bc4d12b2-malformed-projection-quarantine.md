- Card `bc4d12b2`: malformed projection quarantine now checks exact process and
  tmux-session identities; previously it used substring process matching and did
  not inspect tmux sessions before moving a hash-fenced sync-conflict record.
