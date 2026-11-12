Review completion now reads each `card_events` evidence file once per
`validate_review_completion` call instead of up to three times (verdict, CI
links, hosted checks), via one shared scan. Behaviour is unchanged (card
afe3ab3b, SKRSI P0.6).
