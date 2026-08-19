"""alertmux-notify: a self-hosted SMTP notifier over the same normalised feed.

Deliberately separate from `api.py` (HTTP surface) and `mcp_server.py` (MCP
surface). The notifier is a poll-driven process concern: config, rules,
persistent seen-state, SMTP delivery and a runner that ties them together.
See `docs/ARCHITECTURE.md`'s "notify/" section and `docs/DECISIONS.md` for
the unmapped-severity decision this module exists to make impossible to
walk into.
"""
