### Changed

- Cut Niobe dispatcher cycle time on the production board without changing any dispatch decision: worker liveness collection, failed requalification claim checks and profile harvest each reuse one CardStore instead of replaying the shared legacy overlay per card, harvest hashes each Node dependency artifact once per batch, and the candidate scan lists the worker log and worker exit directories once per cycle instead of once per card.
