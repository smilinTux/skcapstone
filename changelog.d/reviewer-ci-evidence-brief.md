### Fixed

- The reviewer brief now states the CI evidence a terminal PASS needs before
  `coord complete` will accept it. The requirement was documented in
  `AGENTS.md` and appeared nowhere in the prompt the reviewer actually
  receives, so reviewers recorded a verdict and stopped, and 60 review cards
  sat reviewed-but-open across skcoord, sklegal and skgateway.
