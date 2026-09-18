"""answer_claim.claim_kind / claim_type: one domain-neutral vocabulary

Three of the stored kinds were the vocabulary of one kind of corpus:
`legal_principle`, `factual`, `procedural`. An engine over maintenance
reports or clinical guidance has rules, applications, facts and procedures
and nothing it would call a legal principle, so the engine's own enumeration
had a deployment's subject matter written into it. The replacements say what
the claim DOES — `rule`, `application`, `fact`, `procedure` — and a
deployment that wants its readers to see its own words renames them where it
renders.

MIGRATED RATHER THAN MAPPED ON READ. Six readers key off these values:
section derivation, the two-strike rule, the objection ledger, the renderer,
`review_export` and `run_export`. A read-time map would have to live in all
six for as long as any row predates this change — that is, forever — and each
copy is a new place the old vocabulary survives, which is exactly the leak
being closed. The values are a closed enumeration this engine writes itself,
never user text, so rewriting them is a bounded UPDATE over one table, and it
is reversible: the mapping is one-to-one in both directions.

`application` is new — nothing maps onto it, and nothing needs to: it is a
kind the drafter may now choose, not a renaming of one it had.

Revision ID: 0042
Revises: 0041
"""

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None

#: old → new. One-to-one, so `downgrade` is the same table read backwards.
_RENAMES = (
    ("legal_principle", "rule"),
    ("factual", "fact"),
    ("procedural", "procedure"),
)

#: Both columns carry the same vocabulary — `claim_type` is the older, coarser
#: one several readers still have, and it held the same three words.
_COLUMNS = ("claim_kind", "claim_type")


def _rewrite(pairs) -> None:
    for column in _COLUMNS:
        for old, new in pairs:
            op.execute(
                f"UPDATE answer_claim SET {column} = '{new}' "  # noqa: S608
                f"WHERE {column} = '{old}'"
            )


def upgrade() -> None:
    _rewrite(_RENAMES)


def downgrade() -> None:
    _rewrite([(new, old) for old, new in _RENAMES])
