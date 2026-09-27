"""Entry point for `python -m validator ...`. Exits with the code cli.main returns."""

import sys

from .cli import main

sys.exit(main())
