Seraph and ATLAS role dispatch resolve the deployed dispatcher under the USER
home again. Both call sites passed the ESTATE home as the artifact base, which
built a path no rollout has ever written, so the `is_file()` guard reported
`seraph_dispatcher_missing` and suppressed every batch: the independent-review
lane dispatched nothing for 19 hours while `awaiting_review` climbed past 400.

The tests could not have caught it, because they passed one `tmp_path` as both
the estate home and the user home, collapsing the two bases onto the same file.
They now keep the homes distinct the way a live host does, so passing the estate
home again fails the suite instead of shipping.
