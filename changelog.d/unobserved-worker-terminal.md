- Card `33f64a43`: native admission now recovers capacity after systemd collects a
  started worker only when its exact invocation has one matching start and
  terminal journal record; ambiguous histories stay charged.
