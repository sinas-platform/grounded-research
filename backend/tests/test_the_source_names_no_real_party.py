"""Source text names no real party, no real document, no deployment vocabulary.

A list of names was tried twice in one day and was incomplete both times. The
first pass missed three undertakings, the second missed two more and two
filenames, and each gap was found only because somebody looked again. A list
of a deployment's vocabulary is also the thing this repository must not carry,
so it could never have been the answer.

What is checked instead is SHAPE. Both checks are deliberately narrow, because
the first draft of this file flagged `Regulatory Decision`, `Google Sheets`
and `The Court` and a guard that cries wolf gets turned off and then defends
nothing. Narrow means it will miss some; quiet means it will still be running
when it catches the next one.

  A FILENAME is suspect when it carries a collection's own scheme: the
  register-style `62018CJ0606.md`, a long multi-hyphen slug like
  `amazon-deliveroo-merger-inquiry--p04.md`, or a bare six-digit serial.
  Short descriptive names are what a fixture wants and are not matched.

  A PARTY is suspect in a case caption, meaning capitalised words either side
  of ` v ` or ` v. `. That is the one context where a proper name is certainly
  a party rather than a class name, an institution or a product.

The invented names this suite uses are listed, and that IS a list, but not the
kind that goes stale silently: adding one is a deliberate line in this file,
and forgetting to add it makes the suite fail loudly rather than pass quietly.

Bare identifiers are untouched on purpose. `C-606/18` and `AT.39796` name
nobody, a pattern cannot be tested without one, and this suite is full of them
for exactly that reason.

THE THIRD CHECK IS A LIST, AND DELIBERATELY SO. Shape cannot catch a leak of
the third kind: engine behaviour keyed on a name a deployment CHOSE — a
relationship called `supersedes`, a class called `Court Decision`. Those look
like ordinary identifiers, and the two found by accident in two days were
found by accident. So a small list of a deployment's chosen vocabulary is
named here, in one place, precisely so it appears nowhere else; the list is
short and fixed rather than exhaustive, because what defends the rule is a
check that stays on, and a check nobody can keep green gets deleted.

THE FOURTH AND FIFTH CHECKS ARE THE TWO WAYS THE ENGINE REACHED A FIELD BY
GUESSING ITS NAME, and they are shape again because a name list cannot help
here: the role names the engine uses for these meanings — `date`, `status`,
`superseded_by` — are spelled exactly like the properties a deployment might
declare for them, so banning the words would ban the vocabulary that replaced
them. What is bannable is the SHAPE of the guess, and there were only two.

  READING A FIELD BY A LITERAL NAME. `props.get("status")`,
  `annotations.get("issuing_body")`: a dictionary of a deployment's fields,
  subscripted by a name the engine wrote down. Which field carries a meaning
  is the deployment's to declare (`engine_role` on the property or the
  annotation, read through `services/declared_roles`); a literal here is the
  engine deciding for it. The engine's own row fields — `r.get("filename")`,
  `doc.get("title")` — are not this and are not matched: the receivers are
  named, and they are the four bags that hold another party's fields.

  MATCHING A FIELD NAME BY REGEX. `re.compile(r"date")`,
  `re.compile(r"jurisdiction|country|member_?state")`: a pattern made of
  nothing but bare words and alternation is not matching TEXT, it is matching
  a NAME, and the only names in reach are a deployment's. A regex with a
  character class, an escape or an anchor is doing ordinary string work and
  is not matched.

Neither shape can catch a list of names held in a constant and read
elsewhere — `_NUMBER_KEYS = ("case_number", "celex", ...)`, which was real and
is gone — because a tuple of strings looks exactly like the engine's own list
of its own columns. That gap is stated rather than papered over: what closes
it is the second check in `test_the_answer_reads_as_prose`, which reads
`answer_render`'s code and fails on those five spellings by name.

It runs over `app/` only — the engine. Two areas are out of scope by design:

  THE PACKAGE AND CONFIG LAYER is where a deployment's vocabulary is supposed
  to arrive. `_CONFIG_LAYER` names those modules; the schema that defines the
  declaration block has to be able to say what it replaced.

  TESTS are not scanned by this check. A fixture simulating a collection of
  legal documents is doing its job, and the two checks above already say what
  a fixture must not carry — a real party, a real filename.

`_PENDING` names modules that still hold a leak this check would fail on,
each with what it would take to clear it. It is not an amnesty: a pending
module that no longer holds one fails the suite, so an entry cannot outlive
what it excuses. IT IS NOW EMPTY, and the shape is kept rather than deleted
because the next finding wants somewhere to be written down that fails on
its own once it is fixed. The three it held were cleared like this:

  `services/ingestion_oneshot.py` — CLASS_RULES mapped five filename
  patterns to four class names. A class now declares its own filename rules
  (`document_class.filename_rules`, from the package), and the entity prompt
  is built from the declared entity types rather than listing kinds of thing
  by example.

  `services/citation_adjudicate.py` — _DEFAULT_DEFS listed five relationship
  names as the default set to adjudicate. The default is structural now:
  every definition with an unresolved row queued against it.

  `answer_rubric.py` — _KIND_WORDS read an Authorities group heading for
  vocabulary to decide what a complete citation is. That module scored
  answers for us while we worked; it was never part of the product and it
  has left the repository, so the leak left with it.
"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent

_INVENTED = {"Ashgrove", "Bellhaven", "Carwood", "Dunmore", "Kestrel",
             "Northmoor", "Phantom", "Systems", "Group", "Holdings", "Retail",
             "Utilities", "Authority", "Tribunal", "Case",
             # The same invented parties as a French fixture writes them.
             "Établissements", "Autorité", "Société"}

# A caption at the start of a sentence sweeps the sentence's first word into
# itself. These are the leads a fixture sentence starts with in the languages
# the fixtures are written in, not anyone's vocabulary, and leaving them out
# would make every "In X v Y" or "Dans X c/ Y" a false alarm.
_SENTENCE_LEAD = {"In", "The", "See", "On", "At", "For", "Per", "From",
                  "Dans", "Voir", "Selon", "Sur", "Par", "Contre"}

_STRING = re.compile(r'"([^"\n]{6,300})"')


def _invented(word: str) -> bool:
    """A party word is invented when every hyphen-joined part of it is: a
    fixture may write `Ashgrove-Bellhaven Group`, and the hyphen is part of
    the token now that a name may carry one."""
    return all(part in _INVENTED for part in word.strip(".").split("-") if part)

# A filename carrying a collection's scheme rather than a fixture's label.
_REGISTER = re.compile(r'"(\d{5}[A-Z]{2}\d{4}\.md)"')
_LONG_SLUG = re.compile(r'"([a-z0-9]+(?:-+[a-z0-9]+){3,}\.md)"')
_BARE_SERIAL = re.compile(r'"(\d{6,}\.md)"')

# A case caption: capitalised words, a versus marker, capitalised words.
#
# Both halves used to be English. The marker was `v`/`v.` only, and the words
# either side were `[A-Z][A-Za-z.]+`, which cannot match a capital carrying an
# accent: `Générale` matches `G` and then stops. So a French caption passed
# this check without a word, in the one place a capitalised name is certainly
# a party. A check that reads only one language is not a check on a collection
# that is not all in one language, and the cost of this one lands outside the run,
# where a leak is public and permanent.
#
# `c/`, `c.` and `contre` are how the same relation is written in French. The
# marker stays lower-case on purpose: an upper-case `C.` is an initial, and
# reading `Exhibit C. Smith` as a caption would invent a finding.
# A party token: a capital, then a LETTER, then anything a name may carry.
# The second character has to be a letter so an elided article is not read as
# the start of a name: `L'affaire Ashgrove c/ Autorité` would otherwise put
# `L'affaire` into the left-hand party, and a lead that is not in the
# invented-name set reads as a real one. The English leads are handled by
# `_SENTENCE_LEAD` because they are separate words; an elided article is not
# a separate word, so it is excluded here instead.
_CAP = r"[A-ZÀ-ÖØ-Þ]"
_CAP_REST = r"[A-Za-zÀ-ÖØ-öø-ÿ](?:[A-Za-zÀ-ÖØ-öø-ÿ.'’‐-]*)"
_VERSUS = r"(?:v\.?|c[./]|contre)"
_CAPTION = re.compile(
    rf"\b((?:{_CAP}{_CAP_REST}\s+){{0,3}}{_CAP}{_CAP_REST})\s+{_VERSUS}\s+"
    rf"({_CAP}{_CAP_REST}(?:\s+{_CAP}{_CAP_REST}){{0,3}})\b")

# ── a deployment's chosen vocabulary ─────────────────────────────────────────

APP = BACKEND / "app"

# Relationship-definition names one deployment chose. An engine that matches
# these is an engine that works for that deployment and silently does nothing
# for every other one: `supersedes` and `is_full_text_of` were matched by
# literal name in the superseded-source check, and the tier was found by
# walking names beginning `ranks_higher_than`. What replaced both is
# `spec.relationship_roles` in the package — the deployment says which of its
# relations mean what, and the engine reads the role.
_RELATIONSHIP_NAMES = (
    "is_full_text_of", "ranks_higher_than", "supersedes", "appealed_in",
    "issued_by", "in_jurisdiction", "cites_legal_instrument",
    "book_cites_decision", "article_review_cites_decision",
)

# Document-class and entity-type names one deployment chose. A class name in
# engine code is a rule that only fires for the collection that happens to use
# that spelling.
_CLASS_NAMES = (
    "Regulatory Decision", "Court Decision", "Bulletin article",
    "Conference summary", "Competition Authority", "Competition Decision",
    "Legal Instrument", "Commentary Source",
)

# Terms that cannot be anything but knowledge about one kind of corpus. Kept
# to a handful: "court", "decision" and "authority" are ordinary English and
# banning them would flag the sentence you are reading, which is how a guard
# gets switched off.
_DOMAIN_TERMS = ("case law", "case-law", "jurisprudence", "antitrust", "cartel")

# Names are matched as written and terms are not. A class is a proper name,
# so `Court Decision` is the deployment's and `a court decision` is English —
# matching the first case-insensitively flagged three ordinary sentences, and
# that is the way to a guard nobody keeps.
_VOCABULARY = re.compile(
    "|".join(re.escape(t) for t in _RELATIONSHIP_NAMES + _CLASS_NAMES)
    + "|(?i:" + "|".join(re.escape(t) for t in _DOMAIN_TERMS) + ")")

# Where a deployment's vocabulary is allowed to be named: the schema that
# defines the declaration block, the importer that writes it, and the settings
# that read it. Relative to `app/`.
_CONFIG_LAYER = frozenset({
    "config.py", "schemas/config.py", "schemas/package.py",
    "services/package.py",
})

# ── reaching a deployment's field by a name the engine wrote ─────────────────

# The bags that hold another party's fields. A row's own keys are the engine's
# and are read by literal name everywhere, legitimately; these four are not.
_FIELD_BAG = r"(?:props|properties|raw_props|annotations|annotation_values)"

# `props.get("status")`, `annotations["issuing_body"]`, and the same with a
# default. What the deployment calls the field carrying a meaning is declared
# on the property or the annotation and read through `declared_roles`.
_LITERAL_FIELD = re.compile(
    _FIELD_BAG + r"\s*(?:\.get\(\s*|\[\s*)[\"']([a-z][a-z0-9_]*)[\"']")

# A regex literal that is matching a NAME rather than text: bare words,
# alternation, an optional character or two, and nothing else. `r"date"` and
# `r"jurisdiction|country|member_?state"` were both of these; every regex the
# engine legitimately runs carries a class, an escape or an anchor.
_REGEX_LITERAL = re.compile(r"re\.(?:compile|match|search|fullmatch)\(\s*"
                            r"r?[\"']([^\"'\n]+)[\"']")
_NAME_SHAPED = re.compile(r"[a-z][a-z0-9_?|]{2,}")

# Modules that still key on a deployment's vocabulary, with what clearing each
# one needs. Every entry is a finding, not an exception — see the staleness
# check below, which fails the moment an entry stops being true.
_PENDING: dict[str, str] = {}


def _name_matching_regexes(line: str):
    """The regex literals on this line that are matching a field name."""
    for pattern in _REGEX_LITERAL.findall(line):
        if _NAME_SHAPED.fullmatch(pattern):
            yield pattern


def _sources():
    for d in ("app", "tests"):
        for f in sorted((BACKEND / d).rglob("*.py")):
            if f.name == Path(__file__).name:
                continue
            yield f, f.read_text(encoding="utf8").splitlines()


def test_no_fixture_names_a_document_from_a_real_collection():
    """A fixture needs a filename. It does not need one that exists."""
    bad = []
    for f, lines in _sources():
        for n, line in enumerate(lines, 1):
            for rx, why in ((_REGISTER, "register scheme"),
                            (_LONG_SLUG, "multi-hyphen slug"),
                            (_BARE_SERIAL, "bare serial")):
                for m in rx.finditer(line):
                    bad.append(f"{f.relative_to(BACKEND)}:{n}: "
                               f"{m.group(1)} ({why})")
    assert not bad, (
        "these carry a collection's naming scheme; a fixture wants a short "
        "descriptive name:\n  " + "\n  ".join(bad))


def test_no_case_caption_names_a_real_party():
    """`X v Y` is the one place a capitalised name is certainly a party."""
    bad = []
    for f, lines in _sources():
        for n, line in enumerate(lines, 1):
            for lit in _STRING.findall(line):
                for m in _CAPTION.finditer(lit):
                    words = [w for w in (m.group(1) + " " + m.group(2)).split()
                             if w not in _SENTENCE_LEAD]
                    if any(not _invented(w) for w in words):
                        bad.append(f"{f.relative_to(BACKEND)}:{n}: "
                                   f"{m.group(0)}")
    assert not bad, (
        "these read as a real case caption; a fixture wants an invented "
        "party:\n  " + "\n  ".join(bad))


def _engine_modules(include_pending: bool = False):
    """Every engine module this check covers, as (relative path, lines).

    Alembic versions are excluded: a migration is a record of what was done on
    a date, and editing one to change its prose would change a file that has
    already run everywhere.
    """
    for f in sorted(APP.rglob("*.py")):
        rel = f.relative_to(APP).as_posix()
        if rel.startswith("alembic/") or rel in _CONFIG_LAYER:
            continue
        if not include_pending and rel in _PENDING:
            continue
        yield rel, f.read_text(encoding="utf8").splitlines()


def test_no_engine_module_names_a_deployments_vocabulary():
    """Engine behaviour keyed on a name a deployment chose works for that
    deployment and silently does nothing for any other. The declaration goes
    in the package; the engine reads the role."""
    bad = []
    for rel, lines in _engine_modules():
        for n, line in enumerate(lines, 1):
            for m in _VOCABULARY.finditer(line):
                bad.append(f"app/{rel}:{n}: {m.group(0)}")
    assert not bad, (
        "these name a deployment's vocabulary in engine code; declare it in "
        "the package (spec.relationship_roles for a relationship's meaning) "
        "and read the declaration instead:\n  " + "\n  ".join(bad))


def test_no_engine_module_reads_a_deployments_field_by_a_literal_name():
    """Which property carries a date, a status, what replaced a source, or
    who issued it is the deployment's to declare. An engine that writes the
    name down works for the collection that happens to spell it that way and
    is silent for every other — and silent is indistinguishable from a corpus
    that holds nothing."""
    bad = []
    for rel, lines in _engine_modules():
        for n, line in enumerate(lines, 1):
            if line.lstrip().startswith("#"):
                continue  # a module has to be able to say what it replaced
            for m in _LITERAL_FIELD.finditer(line):
                bad.append(f"app/{rel}:{n}: {m.group(0)}")
    assert not bad, (
        "these read a deployment's field by a name this repository chose; "
        "declare the meaning on the property or the annotation "
        "(`engine_role`) and read it through services/declared_roles:\n  "
        + "\n  ".join(bad))


def test_no_engine_module_finds_a_field_by_matching_its_name():
    """The other half of the same guess. A pattern of bare words is not
    matching text, it is matching a name — `date` found a date property,
    `jurisdiction|country|member_?state` found a jurisdiction — and a name it
    fails to match is a feature that goes quiet."""
    bad = []
    for rel, lines in _engine_modules():
        for n, line in enumerate(lines, 1):
            if line.lstrip().startswith("#"):
                continue
            for pattern in _name_matching_regexes(line):
                bad.append(f"app/{rel}:{n}: {pattern}")
    assert not bad, (
        "these match a field NAME with a regex; the deployment declares what "
        "a field means and the engine reads the declaration:\n  "
        + "\n  ".join(bad))


def test_the_pending_list_names_only_modules_that_are_still_dirty():
    """An exemption that outlives what it excuses is how a guard rots. A
    pending module with nothing left to find must leave the list."""
    stale = []
    for rel in sorted(_PENDING):
        path = APP / rel
        if not path.exists():
            stale.append(f"{rel}: no such module")
            continue
        if not _VOCABULARY.search(path.read_text(encoding="utf8")):
            stale.append(f"{rel}: clean now")
    assert not stale, (
        "_PENDING names modules that no longer hold a finding; remove "
        "them:\n  " + "\n  ".join(stale))


def test_the_vocabulary_check_can_see_an_offender():
    """A check that matches nothing cannot be told from a clean tree. The
    pattern is exercised against the two shapes it exists for and against
    prose that must not trip it."""
    assert _VOCABULARY.search("WHERE rd.name IN ('is_full_text_of')")
    assert _VOCABULARY.search("path = 'issued_by/(^ranks_higher_than_court)*'")
    assert _VOCABULARY.search('CLASS_RULES = [(r"^x", "Regulatory Decision")]')
    assert _VOCABULARY.search("settled case-law says")
    for ok in ("the court held", "a decision of the authority",
               "superseded by a later source", "the full text of the subject",
               "jurisdiction_note", "the issuing body"):
        assert not _VOCABULARY.search(ok), ok


def test_the_field_checks_can_see_the_two_guesses_they_replaced():
    """Both patterns are exercised against the code that was actually there
    and against the code that replaced it, so a narrowing that empties either
    one fails here rather than in six months' silence."""
    # What was there.
    assert _LITERAL_FIELD.search('status = unwrap(props.get("status"))')
    assert _LITERAL_FIELD.search('unwrap(props.get("superseded_by"))')
    assert _LITERAL_FIELD.search('annotations.get("issuing_body")')
    assert _LITERAL_FIELD.search('v = annotation_values["authority_tier"]')
    assert list(_name_matching_regexes('_DATE_KEY = re.compile(r"date")'))
    assert list(_name_matching_regexes(
        're.compile(r"jurisdiction|country|member_?state", re.IGNORECASE)'))
    # What replaced it, and the ordinary work that must not trip either.
    for ok in ('name = by_class.get(class_name)',
               'return props.get(name)',
               'roles.value(roles.status, props, class_name)',
               'fn = r.get("filename")',
               'entry["properties"][str(name)] = value',
               'raw = (doc or {}).get("properties") or {}'):
        assert not _LITERAL_FIELD.search(ok), ok
    for ok in ('re.fullmatch(r"\\d+", s)', 're.compile(r"^[a-z]")',
               're.split(r"[;,]", text)', 're.compile(r"revision_\\d+")'):
        assert not list(_name_matching_regexes(ok)), ok


def test_the_checks_can_see_an_offender():
    """A guard that matches nothing cannot be told from a clean tree, which is
    the failure this repository keeps meeting. Both patterns are exercised
    against text that must trip them and text that must not, so a narrowing
    that silently empties them fails here first."""
    assert _REGISTER.search('"62018CJ0606.md"')
    assert _LONG_SLUG.search('"amazon-deliveroo-merger-inquiry--p04.md"')
    assert _BARE_SERIAL.search('"108587.md"')
    for ok in ('"a.md"', '"owed.md"', '"a-judgment.md"', '"an-inquiry--p04.md"'):
        assert not (_REGISTER.search(ok) or _LONG_SLUG.search(ok)
                    or _BARE_SERIAL.search(ok)), ok

    assert _CAPTION.search("In Ferriere Nord v Commission the tribunal held")
    assert _CAPTION.search("Strintzis Lines Shipping v. Commission")
    assert not _CAPTION.search("the tribunal held in Case C-606/18")
    # The shape that passed before: an accented capital, and a marker that is
    # not `v`. Captions in another language are the reason this is here.
    assert _CAPTION.search("Société Ashgrove c/ Autorité")
    assert _CAPTION.search("Établissements Dunmore c. Kestrel")
    assert _CAPTION.search("Bellhaven contre Northmoor")
    # An initial is not a marker, and a lone capital is not a party.
    assert not _CAPTION.search("see Exhibit C. Smith for the schedule")
    # An elided article is not the start of a party name. Before this, the
    # caption swallowed the lead and then reported it as a real one.
    m = _CAPTION.search("L'affaire Société Ashgrove c/ Autorité")
    assert m and "affaire" not in m.group(0)
    caption = _CAPTION.search("In Ashgrove Systems v Authority")
    words = [w for w in caption.group(0).replace(" v ", " ").split()
             if w not in _SENTENCE_LEAD]
    assert caption and all(w in _INVENTED for w in words), words
    # A lead in another language is a lead, not a party; a hyphen-joined
    # invented name is still invented.
    for lead in ("Dans Ashgrove c/ Bellhaven", "Voir Ashgrove contre Bellhaven"):
        m = _CAPTION.search(lead)
        words = [w for w in (m.group(1) + " " + m.group(2)).split()
                 if w not in _SENTENCE_LEAD]
        assert m and all(_invented(w) for w in words), (lead, words)
    assert _invented("Ashgrove-Bellhaven") and not _invented("Ashgrove-Nord")
