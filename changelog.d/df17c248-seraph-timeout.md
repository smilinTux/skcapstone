- Card `df17c248`: make Seraph dispatcher timeouts durable and parent-visible;
  previously the child allowed 420 seconds inside a 310-second orchestrator
  wait, so the parent could kill it before process-group cleanup, the terminal
  receipt, and the nonzero result were emitted.
