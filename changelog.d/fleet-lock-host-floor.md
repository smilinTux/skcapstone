### Fixed
- Fleet lock: raised 13 pins that sat below what a fleet host already ran (anyio, idna, markupsafe, numpy, protobuf, psycopg, psycopg-binary, pygments, ruff, sse-starlette, temporalio, types-protobuf, websockets), so the first rollout of the lock no longer moved one host down.
