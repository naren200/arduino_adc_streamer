"""Compatibility import for the profile-driven key=value status decoder."""
from serial_communication.profile_status import apply_status_line


def apply_testboard_status_line(status, line, context=None):
    if context is None:
        from config.boards import get_board_registry
        context = get_board_registry().compatibility_context('routed_status')
    return apply_status_line(status, line, context)
