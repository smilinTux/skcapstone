#!/usr/bin/env python3
"""Run the SKCapstone pull request preflight from a source checkout."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from skcapstone.fleet.pr_preflight import main

raise SystemExit(main())
