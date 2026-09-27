"""
Check implementations.

Importing this package imports both modules, and each registers its
functions by name as it loads: `standing.REGISTRY` holds the named checks
that a rule refers to with `check:`, and `requirements.REGISTRY` holds the
requirement types that gates and money rules list under `requires:`.
"""

from . import requirements, standing  # noqa: F401
