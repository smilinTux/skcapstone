- A BLOCKED verdict must now leave a warm handover: what was attempted or what to
  do next, and where any live work sits, including an explicit "none".
  `blocked_on=dependency` is exempt, and only that category, because its referent
  already IS the resume condition. Measured 2026-08-28 across 198 blocked cards: 7%
  said what was attempted, 3% said where the work was, and 0% said how to resume.
  Port of #273 onto the post-cut main.
- The handover rule accepts `reason=obsolete-<why>` / `superseded` closures (reason field
  only, referent still required), counts only real attempt or next-step statements (stray
  "once", "after", "when", "needs <word>" no longer qualify), accepts the explicit
  `attempted=`, `resume=` and `work_location=` keys, and the production, hosted review and
  cursor worker briefs now teach that format.
