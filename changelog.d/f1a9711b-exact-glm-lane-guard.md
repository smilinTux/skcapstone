- Card `f1a9711b`: exact GLM lane guard, adapted onto current main. Before,
  nothing on an SKLegal card could state "GLM, at this exact size, never any
  other provider" in one label, so incident `inc-e5ab3dea` dispatched a GLM-only
  card through the shared pool and Qwen answered it. `sk-glm-s`/`sk-glm-m`/
  `sk-glm-l` are now valid exact GLM-only no-fallback selectors: they pin the
  GLM lane in lane affinity, act as a size source, are resolved as a model pin
  against the fresh gateway snapshot at dispatch and last-moment admission, and
  hold (`no-free-lane:glm`) instead of falling back when the GLM lane has no
  slot. Provider-neutral `sk-s`/`sk-m`/`sk-l`/`sk-xl` and Qwen-first behavior
  are unchanged. Card creation, amendment, and dispatch share one classifier
  (`routing_guard.classify_card_routing`) that also rejects provider-family
  compatibility labels and multiple buckets at the claim gate.
