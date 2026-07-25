"""Ports — the swappable interfaces the core depends on (Hexagonal architecture).

The core imports ONLY these interfaces; concrete adapters live in app/adapters/
and are chosen by config at boot (app/container.py). Importing this package pulls
in NO third-party driver.
"""
