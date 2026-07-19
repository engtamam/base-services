"""Adapters — concrete implementations of ports, chosen by config (container.py).

Grouped by port. Real infra adapters import their driver LAZILY (inside the
constructor) so importing an adapter module never fails when an optional
dependency is absent.
"""
