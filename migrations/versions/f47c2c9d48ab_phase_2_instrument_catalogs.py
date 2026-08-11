"""phase 2 instrument catalogs

Revision ID: f47c2c9d48ab
Revises: c02df1421a01
Create Date: 2026-08-11 00:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "f47c2c9d48ab"
down_revision: str | None = "c02df1421a01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instrument_catalog_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("total_symbols", sa.Integer(), nullable=False),
        sa.Column("candidate_symbols", sa.Integer(), nullable=False),
        sa.Column("instrument_count", sa.Integer(), nullable=False),
        sa.Column("excluded_count", sa.Integer(), nullable=False),
        sa.Column("catalog_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "candidate_symbols = instrument_count + excluded_count",
            name=op.f("ck_instrument_catalog_versions_complete_candidate_accounting"),
        ),
        sa.CheckConstraint(
            "excluded_count >= 0",
            name=op.f("ck_instrument_catalog_versions_nonnegative_excluded_count"),
        ),
        sa.CheckConstraint(
            "candidate_symbols > 0",
            name=op.f("ck_instrument_catalog_versions_positive_candidate_symbols"),
        ),
        sa.CheckConstraint(
            "instrument_count > 0",
            name=op.f("ck_instrument_catalog_versions_positive_instrument_count"),
        ),
        sa.CheckConstraint(
            "total_symbols > 0",
            name=op.f("ck_instrument_catalog_versions_positive_total_symbols"),
        ),
        sa.CheckConstraint(
            "length(content_sha256) = 64",
            name=op.f("ck_instrument_catalog_versions_valid_content_sha256"),
        ),
        sa.CheckConstraint(
            "environment IN ('testnet', 'production')",
            name=op.f("ck_instrument_catalog_versions_valid_environment"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instrument_catalog_versions")),
        sa.UniqueConstraint(
            "environment",
            "content_sha256",
            name=op.f("uq_instrument_catalog_versions_environment_content_sha256"),
        ),
        sa.UniqueConstraint(
            "id",
            "environment",
            name=op.f("uq_instrument_catalog_versions_id_environment"),
        ),
    )
    op.create_index(
        "ix_instrument_catalog_versions_environment_created",
        "instrument_catalog_versions",
        ["environment", "created_at"],
        unique=False,
    )
    op.create_table(
        "instrument_catalog_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False),
        sa.Column("catalog_version_id", sa.Uuid(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_artifacts_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "environment IN ('testnet', 'production')",
            name=op.f("ck_instrument_catalog_observations_valid_environment"),
        ),
        sa.ForeignKeyConstraint(
            ["catalog_version_id", "environment"],
            ["instrument_catalog_versions.id", "instrument_catalog_versions.environment"],
            name=op.f(
                "fk_instrument_catalog_observations_catalog_version_id_"
                "instrument_catalog_versions"
            ),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instrument_catalog_observations")),
    )
    op.create_index(
        "ix_instrument_catalog_observations_environment_time",
        "instrument_catalog_observations",
        ["environment", "observed_at"],
        unique=False,
    )
    op.create_table(
        "instrument_catalog_review_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sequence_number", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False),
        sa.Column("catalog_version_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('APPROVE', 'REJECT')",
            name=op.f("ck_instrument_catalog_review_events_valid_action"),
        ),
        sa.CheckConstraint(
            "environment IN ('testnet', 'production')",
            name=op.f("ck_instrument_catalog_review_events_valid_environment"),
        ),
        sa.CheckConstraint(
            "length(actor) > 0",
            name=op.f("ck_instrument_catalog_review_events_nonempty_actor"),
        ),
        sa.CheckConstraint(
            "length(reason) > 0",
            name=op.f("ck_instrument_catalog_review_events_nonempty_reason"),
        ),
        sa.ForeignKeyConstraint(
            ["catalog_version_id", "environment"],
            ["instrument_catalog_versions.id", "instrument_catalog_versions.environment"],
            name=op.f(
                "fk_instrument_catalog_review_events_catalog_version_id_"
                "instrument_catalog_versions"
            ),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instrument_catalog_review_events")),
        sa.UniqueConstraint(
            "sequence_number",
            name=op.f("uq_instrument_catalog_review_events_sequence_number"),
        ),
    )
    op.create_index(
        "ix_instrument_catalog_review_events_version_sequence",
        "instrument_catalog_review_events",
        ["catalog_version_id", "sequence_number"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_instrument_catalog_review_events_version_sequence",
        table_name="instrument_catalog_review_events",
    )
    op.drop_table("instrument_catalog_review_events")
    op.drop_index(
        "ix_instrument_catalog_observations_environment_time",
        table_name="instrument_catalog_observations",
    )
    op.drop_table("instrument_catalog_observations")
    op.drop_index(
        "ix_instrument_catalog_versions_environment_created",
        table_name="instrument_catalog_versions",
    )
    op.drop_table("instrument_catalog_versions")
