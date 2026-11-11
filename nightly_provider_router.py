"""Compatibility imports; operational router is maintained in pipeline_produtos.py.

Existing test suites and code importing this module continue to work.
Do not add provider quota logic here.
"""
from pipeline_produtos import Slot, DurableRouter, configured_slots

__all__ = ["Slot", "DurableRouter", "configured_slots"]
