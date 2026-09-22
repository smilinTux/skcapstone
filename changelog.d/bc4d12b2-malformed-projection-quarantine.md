- Card `bc4d12b2`: malformed projection quarantine now recognizes only values
  of governed identity flags and fails closed when tmux cannot be probed;
  previously unrelated argument values or assignment suffixes could look live,
  and unavailable or permission-denied tmux probes were treated as proof of
  session absence.
