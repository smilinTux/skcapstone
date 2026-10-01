- Add an explicit operator retry for stopped, unchanged production builders
  awaiting evidence after infrastructure failure. Preserve the exact claim,
  request and failed-attempt evidence; authorize one fresh Pi unit attempt
  within the existing attempt limit. Candidate, source, process and replay
  checks refuse unsafe retry without inventing a producer verdict.
