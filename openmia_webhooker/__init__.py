"""OpenMIA webhook collectors used by local agent integrations."""

from .client import OpenMIAClient
from .config import OpenMIAConfig

__all__ = ["OpenMIAClient", "OpenMIAConfig"]
