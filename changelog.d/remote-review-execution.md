### Added
- Governed remote review execution: the native dispatcher on the authority host can now place
  independent reviews on approved worker hosts through sknoded, the way builder jobs already go.
  Admission is checked on the destination host, receipts may name an approved worker host and
  reject any other, reviewer independence is still enforced, and a single coordinator remains.
  Production policy adds a chiap01 quota, keeps chiap02's, caps chiap08 at one worker with an
  8 GiB memory floor, and excludes chiap09/chiap10. Ships disabled; the canary is chiap03.
  Motivation: all review capacity was the authority host, whose memory is reserved by
  application services, so reviews queued while worker hosts sat idle.
