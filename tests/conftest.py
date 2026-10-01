# Every test runs behind the outbound network block in network_guard.py.
from network_guard import _reset_network_registrations, pytest_configure  # noqa: F401
