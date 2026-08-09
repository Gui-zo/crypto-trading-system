"""Storage abstractions (raw-payload retention, object storage).

Interfaces here are deliberately narrow so a local filesystem implementation can
be swapped for S3 in a later phase without touching call sites.
"""
