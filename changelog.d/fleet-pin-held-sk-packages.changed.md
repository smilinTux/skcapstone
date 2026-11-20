The fleet constraints now pin skchat-sovereign 0.14.266, sksecurity 1.2.8,
skbrain 1.0.1 and skvault 0.1.0, the four packages held back on 2026-11-14.
New `brain` and `vault` extras give skbrain and skvault a pyproject
dependency. The pins move click to 8.1.8 and typer to 0.12.5 on every host
(skbrain's caps) and fastapi to 0.135 on chiap08 (skchat-sovereign's cap).
