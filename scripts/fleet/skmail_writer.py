#!/usr/bin/env python3
"""Compatibility import and CLI for the packaged SKMail writer."""

from skcapstone.fleet.skmail.skmail_writer import (  # noqa: F401
    PRIORITIES,
    append,
    main,
    os,
    quarantine_foreign,
)

if __name__ == "__main__":
    raise SystemExit(main())
