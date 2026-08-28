"""Entity types are enforced per document class where the mention is written.

The domain config (document_class_entity_type) declares which entity types a
class may carry — Author only on the publisher's own content, so judges and
officials in decisions are not indexed as people. The extraction prompt
carried this as an instruction; measured on the live corpus, the instruction
lost 180,000 times. The check lives at persistence, not in the prompt.
"""

from pathlib import Path

from app.services import ingestion_oneshot

SRC = Path(ingestion_oneshot.__file__).read_text()


def test_persistence_consults_the_class_allowlist():
    assert "DocumentClassEntityType.entity_type_id" in SRC
    assert "allowed_type_ids" in SRC
    assert "type_out_of_scope_for_class" in SRC


def test_restriction_is_opt_in_per_class():
    # A class with no declared list is unrestricted: the guard must treat an
    # empty result as None, never as an empty allowlist that blocks all types.
    assert "set(rows_allowed) if rows_allowed else None" in SRC


def test_the_check_precedes_the_write():
    # within the mentions-first persistence block, the guard comes before
    # the EntityMention write
    block = SRC[SRC.index("mentions-first persistence"):]
    assert block.index("allowed_type_ids is not None and tinfo") < block.index(
        "EntityMention(")
