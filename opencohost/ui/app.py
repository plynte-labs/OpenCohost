"""OpenCohost main application module — thin re-export layer.

This file is kept for backward compatibility with main.py imports.
All application logic lives in ui.app_shell.VocalAIApp.
"""

from opencohost.ui.app_shell import VocalAIApp

__all__ = ["VocalAIApp"]
