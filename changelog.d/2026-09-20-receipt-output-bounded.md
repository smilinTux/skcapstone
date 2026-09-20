Bound and de-noise dispatcher output before it is embedded in a seat receipt, so
one noisy line can no longer black out all dispatch observability.

The seat dispatchers emit a structured JSON receipt per cycle carrying the
subprocess's raw stdout and stderr verbatim, with no bound. A single malformed
line in a card_events overlay file makes the CardStore fold emit an identical
warning once per folded card, which is hundreds of times per cycle. Those repeats
pushed the receipt past journald's 48KB per-message cap, so it arrived truncated
mid-string and was unparseable JSON. The practical consequence: `journalctl`
showed only systemd's own Starting and Finished lines while the seat burned two
minutes of CPU per cycle, and operators could not see why dispatch failed. One
unreadable line cost the observability of the whole fleet.

`condense_dispatcher_output()` collapses consecutive identical lines to one plus
a repeat count, then bounds each stream while keeping BOTH head and tail with an
explicit elision marker, because the tail usually holds the actual failure. The
collapsing is the part that matters most: it turns hundreds of identical fold
warnings back into one line and makes genuine errors visible again.

Applied at every point a receipt embeds dispatcher output, including a third one
in `seat_cycle_entrypoint.main()` that prints its own summary to stdout and is
captured by journald under the same cap.
