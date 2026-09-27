"""Entry point for `python -m daemon`. The commands are in daemon/cli.py."""

import sys

from .cli import main

sys.exit(main())
