- Card `bc4d12b2`: malformed projection quarantine now recognizes only exact
  identity-bearing process arguments and fails closed when tmux cannot be
  probed; previously unrelated assignment suffixes could look live and an
  unavailable tmux executable was treated as proof that no session existed.
