- GLM gateway responses marked `malformed_response` or
  `empty_upstream_response` now use the bounded Pi retry path; unrelated 502s
  remain terminal.
