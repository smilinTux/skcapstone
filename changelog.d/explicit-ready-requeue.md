### Fixed

- A governed move back to READY now requeues a producer after its prior review
  generation is retired, so fresh work can run without reviving stale review custody.
