"""invoice-mgmt: template-driven invoice extraction and filling."""

__version__ = "0.1.0"

from .extract import detect_template, extract  # noqa: E402,F401
from .fill import fill  # noqa: E402,F401
from .template import list_templates, load_template  # noqa: E402,F401
