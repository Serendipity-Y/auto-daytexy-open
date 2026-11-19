"""Compatibility package for the moved workbench Web modules."""

from apps.workbench import webapp as _implementation

__path__ = _implementation.__path__
