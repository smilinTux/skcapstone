### Fixed

- Producer workers now run only the smallest tests the card's TDD story.
  The production brief let a builder run the full repository suite, which
  outlasted the worker timeout, so builders died before reporting. The brief
  now asks for each required test command once, and if it times out, to keep
  its output and report the exact command instead of repeating it.
