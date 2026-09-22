- Card `bc4d12b2`: malformed projection quarantine now recognizes only values
  of governed identity flags and fails closed when tmux cannot be probed;
  previously unrelated argument values or assignment suffixes could look live,
  and an unavailable tmux executable was treated as proof of session absence.
