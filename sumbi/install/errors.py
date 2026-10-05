"""Errors deliberately omit local paths and file contents."""


class InstallError(Exception):
    """A plan cannot be inspected or applied safely."""
