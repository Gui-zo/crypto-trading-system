"""Database models and repositories.

Repositories **flush but never commit** — the CLI command owns the transaction, so
a command is one atomic unit and a failure halfway through leaves no partial
audit trail.
"""
