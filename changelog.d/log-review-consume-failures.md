### Fixed

- Builder nodes now log why they refuse a remote review request
  (`REVIEW_CONSUME_REFUSED|node|card|request|reason`, once per request and
  reason). The consumer used to swallow every error from `consume_review`, so a
  review offered to chiap03 for 8f4b04d4 expired twice with nothing in the
  node's journal to say why.
