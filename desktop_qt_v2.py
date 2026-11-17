"""Compatibility launcher for the full desktop with Codex / Claude Code buttons.

The earlier minimal tab draft had no wired actions. Use the existing full desktop
so task controls, conversations, approvals, quota and tray behavior stay intact.
"""
from desktop_qt import main

if __name__ == '__main__':
    raise SystemExit(main())
