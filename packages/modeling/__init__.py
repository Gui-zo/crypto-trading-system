"""Model construction and provenance capture.

Sits between the pure forecasting logic in :mod:`domain.funding_model` and the
persistence layer. Everything here touches the filesystem or Git, which is why
it is not in ``packages/domain``.
"""
