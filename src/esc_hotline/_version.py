"""Single source of truth for the app version. Release CI overwrites this file
from the git tag (scripts/stamp_version.py) before building; the value here is
only what a plain source checkout reports."""
__version__ = "1.0.1"
