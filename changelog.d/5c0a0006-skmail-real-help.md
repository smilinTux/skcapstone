- `skmail --help`, `-h`, `help` and `<command> --help` print real help with examples and
  exit 0 without touching the mailbox directory. `skmail send` with too few arguments (and
  `read`/`ack` without a name) now exits 2 with a one-line reason plus the usage text instead
  of crashing with `unbound variable`, and an unknown command prints the usage. Message bodies
  are never mistaken for help flags. Port of #340 onto the packaged script
  `src/skcapstone/fleet/skmail/skmail`.
