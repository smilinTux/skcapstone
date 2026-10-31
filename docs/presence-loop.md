# PRES-6: proactive presence loop

Card `32ea0e0e` (parent epic `3915bd8a`). Module: `src/skcapstone/presence_loop.py`.

## What it does
Runs on the daemon's existing `TaskScheduler` (no second thread) as task `presence_loop`.
Every 15 minutes it reads the agent's Nextcloud Talk room list (`GET /ocs/v2.php/apps/spreed/api/v4/room`)
and flags interactions waiting on the agent.

## Detection
A room is pending when its last message:
- is not from the agent, not a system message, not a bot or bridged actor;
- is older than the threshold (default 15 min);
- and the room is a one-to-one DM (type 1), or a group/public room (type 2/3) with `unreadMention`.

Read-only, note-to-self (6), changelog (4) and former one-to-one (5) rooms are ignored.
Detection never marks anything read.

## Outreach triggers
- Keyed on `(room token, message id)` and persisted in `<home>/presence/state.json`: one outreach per message.
  A new message in the same room is a new trigger.
- Quiet hours (default 22:00 to 07:00 local) and a per-tick cap (default 3) **defer**; deferred items fire
  on the first allowed tick.
- A failed send is not recorded, so it retries next tick.
- Default channel: `notifications.notify` (desktop or headless sk-alert) telling the operator what is waiting.
- Opt-in: `SKCAPSTONE_PRESENCE_TALK_NUDGE=1` also posts a reply in the Talk room:
  "Still here. I saw this and haven't answered yet; it's next on my list."

## Configuration (environment)
| Variable | Default | Meaning |
|---|---|---|
| `SKCAPSTONE_PRESENCE_ENABLED` | off | register the task at all |
| `SKCAPSTONE_PRESENCE_MODE` | `dry-run` | `live` sends outreach; anything else is dry-run |
| `SKCAPSTONE_PRESENCE_INTERVAL` | 900 | seconds, floor 60 |
| `SKCAPSTONE_PRESENCE_THRESHOLD_MIN` | 15 | minutes a message must wait |
| `SKCAPSTONE_PRESENCE_MAX_PER_TICK` | 3 | outreach cap per tick |
| `SKCAPSTONE_PRESENCE_QUIET_START` / `_END` | 22 / 7 | local hours; equal values disable |
| `SKCAPSTONE_PRESENCE_TALK_NUDGE` | off | also reply in the Talk room (live only) |
| `NEXTCLOUD_TALK_SERVER_URL` / `_USERNAME` / `_APP_PASSWORD` | | agent's Talk credentials; missing = not registered |

## Dry run
`python -m skcapstone.presence_loop` runs one forced dry-run tick and prints a JSON report. Sends nothing, writes no state.
