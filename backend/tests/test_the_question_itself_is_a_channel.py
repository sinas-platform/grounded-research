"""The question's own words are a retrieval channel of their own.

Every other channel's input is written by a model — the anchors it names,
the text queries it composes — and a second run of the same question writes
them differently: measured on 18 September 2026, no two samples shared a
text query, and two runs' retrieved sets overlapped by half. The question's
words are the one input that is the same every time. Ranked by the index on
their own they put the chapter that decides one question at rank 1 and the
two documents the review had asked for at 30 and 55; fused with the
other channels they are the floor retrieval stands on.

They also reach what the entity channels cannot: a sampled 29% of the
collection has no recognised mention of a real entity.

What these pin:

- the expression is every distinct word of the question, lower-cased, in
  first-seen order, so the same question is always the same expression;
- word characters are Unicode, so no language is special and nothing is a
  stopword;
- the plan carries the question, so a stored plan replays what it ran;
- retrieval reads it as its own channel, ranks length-normalised, and says
  so in the document's reason.
"""

from __future__ import annotations

import inspect

from app import retrieval_first as rf


def test_the_terms_are_the_questions_own_words_once_each_in_order():
    assert rf.question_terms(
        "Whose interests prevail when a survey captures private correspondence?"
    ) == "whose | interests | prevail | when | survey | captures | private | correspondence"


def test_the_same_question_is_the_same_expression():
    q = "Can two companies agree not to hire from each other?"
    assert rf.question_terms(q) == rf.question_terms(q)
    assert rf.question_terms(q) == "can | two | companies | agree | not | hire | from | each | other"


def test_short_words_and_bare_numbers_are_left_out():
    assert rf.question_terms("Is Article 7 of the Compact a bar?") == "article | the | compact | bar"


def test_no_language_is_special():
    assert rf.question_terms("Peut-on saisir la correspondance privée des salariés ?") == (
        "peut | saisir | correspondance | privée | des | salariés")
    assert rf.question_terms("Mag de Commissie e-mails van werknemers doorzoeken?") == (
        "mag | commissie | mails | van | werknemers | doorzoeken")


def test_nothing_in_is_nothing_out():
    assert rf.question_terms("") == ""
    assert rf.question_terms("a v b") == ""


def test_the_plan_carries_the_question():
    from app import hypotheses

    assert '"question": question' in inspect.getsource(hypotheses.plan)


def test_retrieval_reads_the_question_as_its_own_channel():
    from app import hypotheses

    src = inspect.getsource(hypotheses.rank)
    assert "question_terms(str(plan.get(\"question\")" in src
    assert "the question's words" in src
    assert "to_tsquery('simple', :t), 1)" in inspect.getsource(hypotheses._text_words), "length-normalised rank"
