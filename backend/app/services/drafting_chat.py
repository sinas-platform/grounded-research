"""The drafting conversation — one chat per answer, not a call per round.

Drafting used to be stateless. Every revision round was a fresh one-shot call
that re-sent the whole world — the passages, the playbook, the structure
rules, every current claim with its evidence, and the round's feedback — to a
model that held no memory of what it had written or why. Two things followed,
and both were measured on live runs.

*It could not argue.* A two-way objection loop exists (`services/objections`):
the review asks for something, and the drafter may REFUSE with a reason rather
than obey or go silent. On the run that motivated this module, 7 objections
were raised, 0 were answered, 0 were ruled on, and `kept_with_reason` was 0 in
all ten revision rounds. A call with no record of its own reasoning has
nothing to refuse with; "I already considered that source and here is why it
cannot carry the point" is not a thing a stateless call can say.

*It could not cache.* Over two hours the drafting agent showed 14 calls,
131,710 prompt tokens, 131,685 of them written to cache and 0 read back. The
provider is not at fault: Sinas' Anthropic provider sets three cache
breakpoints, including a ROLLING one on the last message, precisely so that a
sequence of calls reuses the previous call's prefix. A freshly assembled body
each round gives it nothing to roll onto.

So drafting is a conversation:

- **turn one is the brief** and carries everything durable: the task, the
  structure rules, the house playbook, the question and its parts, and the
  verified passages. It is sent once and never sent again within a chat. It is
  the cached prefix, which is why it ends by asking for a one-word
  acknowledgement rather than for work: a brief that can be replayed
  byte-for-byte is a brief a new chat can start from and read back from cache.
- **every later turn carries only what is new** — this round's feedback, the
  objections outstanding against it, or a batch of newly extracted passages.
  Never the playbook, never the passages again.
- **the assistant's own turns are its position.** Its claims are not read back
  to it; it can see them. What it is given is the engine's numbering, because
  sequence numbers are assigned by the engine after reordering and are the
  only thing about its own answer the drafter cannot know.
- **context is capped.** After `max_exchanges` feedback turns the conversation
  restarts from the same brief — the same bytes, so the prefix is still
  cached — with one summary turn standing in for the rounds that were dropped.

The chat id is persisted by the caller, so a resumed run continues the same
conversation rather than opening a second one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

_log = logging.getLogger(__name__)

#: What turn one asks for. Work is asked for in turn two, so that turn one can
#: be replayed into a fresh chat without paying for a whole draft to be thrown
#: away — see the rollover in `turn`.
ACK = "READY"

ACK_INSTRUCTION = (
    "\n\nDo not write any claims yet. Reply with the single word "
    f"{ACK} and nothing else. The task comes in the next message, and "
    "everything above stays true for the whole of this conversation.\n"
)


class _Client(Protocol):
    """The three calls this needs from the Sinas client, and no more."""

    async def chat_create(self, agent: str, title: str) -> str: ...

    async def chat_send(self, chat_id: str, content: str,
                        agent: str = "") -> str: ...

    async def chat_messages(self, chat_id: str) -> list[dict]: ...


@dataclass
class Exchange:
    """One feedback turn and what the drafter did about it.

    `note` is the caller's one line about the reply — what was revised,
    dropped, refused. It is what a summary turn is built from: the transcript
    itself cannot say whether a patch was applied, and a summary assembled
    from the engine's own record is both shorter and truer than one assembled
    from the model's words.
    """

    label: str
    note: str = ""


@dataclass
class DraftingChat:
    """One conversation, for the whole life of one answer."""

    client: _Client
    agent: str
    #: Set by `start`; the caller persists it and hands it back on a resume.
    chat_id: str | None = None
    title: str = "[query-run] drafting"
    max_exchanges: int = 4
    #: Turn one AS SENT, byte for byte, so a restart can replay it exactly —
    #: an identical prefix is the whole of what makes the cache hit. Empty on
    #: an object that rejoined an existing chat without composing a brief; it
    #: is then read back off the chat itself, where it is the first message.
    opening: str = ""
    exchanges: list[Exchange] = field(default_factory=list)
    #: Rounds already folded into a summary turn, oldest first.
    summarised: list[Exchange] = field(default_factory=list)
    #: How many chats this conversation has occupied. More than one means the
    #: cap was reached and the brief was replayed.
    chats: int = 0
    #: Set when `start` adopted a chat id rather than opening one, which is
    #: what a resumed run does.
    resumed: bool = False

    # ── opening ──────────────────────────────────────────────────────────────

    async def start(self, brief: str, chat_id: str | None = None) -> str:
        """Open the conversation on this brief, or rejoin an open one.

        A `chat_id` from a previous attempt at the same answer — passed here,
        or already carried on the object by a `restore` — is adopted as it
        stands: the brief is NOT re-sent, because it is already the first
        message of that chat and sending it twice would both double the
        cached prefix and invite the drafter to start over. Returns the chat
        id the caller must persist.
        """
        self.opening = brief + ACK_INSTRUCTION
        chat_id = chat_id or self.chat_id
        if chat_id:
            self.chat_id = chat_id
            self.resumed = True
            self.chats = max(1, self.chats)
            return chat_id
        await self._open()
        return self.chat_id or ""

    async def _open(self) -> None:
        """A fresh chat carrying turn one and nothing else."""
        self.chat_id = await self.client.chat_create(self.agent, self.title)
        self.chats += 1
        await self.client.chat_send(self.chat_id, self.opening, self.agent)

    async def _opening_message(self) -> str:
        """Turn one as it was actually sent.

        Composed here when this object opened the chat, and otherwise read
        back off the chat, where it is the first message. A later stage joins
        the conversation to send a round and has no reason to rebuild a brief
        it is not going to send — until the cap is reached, and then the exact
        bytes are the one thing that matters.
        """
        if self.opening:
            return self.opening
        if not self.chat_id:
            return ""
        try:
            messages = await self.client.chat_messages(self.chat_id)
        except Exception:  # noqa: BLE001
            _log.warning("could not read the brief back from chat %s",
                         self.chat_id, exc_info=True)
            return ""
        for m in messages or []:
            if isinstance(m, dict) and m.get("role") == "user" and m.get("content"):
                self.opening = str(m["content"])
                return self.opening
        return ""

    # ── turns ────────────────────────────────────────────────────────────────

    async def ask(self, content: str) -> str:
        """A turn that is not a feedback round: the draft ask, a retry, a
        batch of newly extracted passages. It does not count against the cap.
        """
        return await self._send(content)

    async def prepare(self) -> None:
        """Make room for the round about to be sent, if the cap is spent.

        Separate from `turn` because a round is not always one message: new
        passages go first, in a turn of their own, and compacting after them
        would open a chat the passages had never reached. Idempotent — after a
        compaction there are no rounds left to fold.
        """
        if len(self.exchanges) >= self.max_exchanges:
            await self._compact()

    async def turn(self, content: str, label: str = "") -> str:
        """One feedback round, and the drafter's reply to it.

        This is what the cap counts. When the conversation is already carrying
        `max_exchanges` rounds whole, it is restarted from the same brief with
        a summary turn in their place, and only then is this round sent.
        """
        await self.prepare()
        self.exchanges.append(Exchange(label=label or f"round {self.rounds}"))
        return await self._send(content)

    def note(self, note: str) -> None:
        """What the last round's reply actually did, in one line.

        Recorded by the caller because only the caller knows: a patch that
        parsed is not a patch that applied, and a summary built from what the
        engine did is the one worth carrying forward.
        """
        if self.exchanges:
            self.exchanges[-1].note = note

    @property
    def rounds(self) -> int:
        """Feedback rounds this conversation has carried, summarised included."""
        return len(self.summarised) + len(self.exchanges) + 1

    async def _send(self, content: str) -> str:
        if not self.chat_id:
            raise RuntimeError("the drafting conversation was never opened")
        return await self.client.chat_send(self.chat_id, content, self.agent)

    # ── carrying the conversation across stages and restarts ─────────────────

    def state(self) -> dict[str, Any]:
        """What has to survive for a later stage — or a resumed run — to
        continue this conversation rather than start a second one."""
        return {
            "chat_id": self.chat_id,
            "chats": self.chats,
            "exchanges": [{"label": e.label, "note": e.note}
                          for e in self.exchanges],
            "summarised": [{"label": e.label, "note": e.note}
                           for e in self.summarised],
        }

    def restore(self, state: dict[str, Any] | None) -> DraftingChat:
        """Adopt a stored state. The rounds matter as much as the id: without
        them the cap counts from zero every time a stage rebuilds the object,
        and the conversation grows without bound."""
        state = state or {}
        self.chat_id = str(state.get("chat_id") or "") or self.chat_id
        self.chats = int(state.get("chats") or 0)
        self.exchanges = [Exchange(str(e.get("label") or ""),
                                   str(e.get("note") or ""))
                          for e in (state.get("exchanges") or [])
                          if isinstance(e, dict)]
        self.summarised = [Exchange(str(e.get("label") or ""),
                                    str(e.get("note") or ""))
                           for e in (state.get("summarised") or [])
                           if isinstance(e, dict)]
        return self

    # ── the cap ──────────────────────────────────────────────────────────────

    async def _compact(self) -> None:
        """Start again from the same brief, with a summary in place of the
        rounds that were carried whole.

        The brief is re-sent unchanged, which is the point: the provider's
        cache is keyed on the request prefix, so an identical system prompt
        followed by an identical first message reads back what the previous
        chat wrote rather than paying for it again. Everything after it — the
        rounds, the patches, the arguments — is what gets dropped, and those
        are the cheap part of the transcript.
        """
        if not await self._opening_message():
            # Without the exact bytes there is no prefix to restart on, and a
            # restart that paraphrases the brief would be a drafter asked to
            # patch an answer it can no longer see. Carrying the rounds whole
            # costs tokens; this would cost the answer.
            _log.warning("drafting conversation cannot be compacted: turn one "
                         "is not recoverable from chat %s", self.chat_id)
            return
        folded = list(self.exchanges)
        self.summarised.extend(folded)
        self.exchanges = []
        await self._open()
        await self._send(summary_turn(self.summarised))
        _log.info("drafting conversation compacted after %d round(s); "
                  "chat %s", len(folded), self.chat_id)


def summary_turn(done: list[Exchange]) -> str:
    """The rounds already argued, one line each. Pure.

    Deliberately terse and deliberately about OUTCOMES. The drafter does not
    need the wording of a request it has already answered; it needs to know
    that it answered it, so that it neither re-argues a settled point nor
    forgets a reason it has already given.
    """
    lines = "\n".join(
        f"- {e.label}: {e.note or 'no change recorded'}" for e in done)
    return (
        "EARLIER IN THIS CONVERSATION — the rounds so far, in summary. The "
        "messages themselves are not carried forward; this stands in for "
        "them, and the brief above is unchanged and still binding.\n"
        f"{lines}\n\n"
        "Do not re-argue anything settled here, and do not redraft the "
        "answer. Reply with the single word "
        f"{ACK} and wait for the next round."
    )


# ── what a feedback turn says ────────────────────────────────────────────────


def objections_block(open_points: list[dict[str, Any]]) -> str:
    """The requests outstanding against this answer, each with the id a reply
    names. Pure.

    The refusal was already a legal move and nothing ever made it. The reason
    is visible here: the id used to reach the drafter buried inside the prose
    of one feedback line among ten, in a one-shot call that had no memory of
    ever having been asked. Presented as its own block, with both answers
    spelled out under it, refusing costs the drafter one object.
    """
    if not open_points:
        return ""
    lines = []
    for p in open_points:
        subject = str(p.get("subject") or "").strip()
        asked = str(p.get("asked") or "").strip()
        weight = (" — the review calls this essential: "
                  + str(p.get("why_essential") or "").strip()
                  if p.get("why_essential") else "")
        lines.append(f'- [{p.get("id")}] {subject}: {asked}{weight}')
    return (
        "\n\nOUTSTANDING REQUESTS — each carries the id a reply names. For "
        "every one of them you must do ONE of two things, and silence is "
        "neither:\n"
        + "\n".join(lines)
        + "\n\nEither ACT on it — cite the source in a revised or added "
        'claim, or waive it in "waive" — or REFUSE it: put '
        '{"objection": "<the id>", "rationale": "<why this source cannot '
        'carry the point asked of it>"} in "refuse". If a drop or a keep IS '
        'your answer to one of these, put the same id on that "drop" or '
        '"keep" entry instead and its rationale answers the request. The '
        "brief says what each of those three moves commits you to.\n"
        "\nA REFUSAL ALSO CHANGES WHAT THE CLAIM SAYS. Refusing a request "
        "that a proposition rest on a source of higher standing means the "
        "higher-standing sources were opened for that point and did not "
        "state it. A reader cannot tell that from ordinary attribution, and "
        "a proposition the stronger sources do not carry may be mistaken, "
        "may have been overtaken, or may be the view of whoever wrote it. So "
        "when you refuse on those grounds, rewrite the claim to say that the "
        "stronger sources were read for this point and do not state it. "
        "Write it as the warning it is, in your own words.\n"
    )


#: The three things a drafter may still do about a claim the review has now
#: failed twice, in the words the round uses. One string, because the finding
#: line and the standing block must not drift into saying different things.
PERMITTED_MOVES = (
    "DROP it — put it in \"drop\" with a rationale; "
    "DEFEND it — revise it and bind it to evidence it does not already cite, "
    "a different passage or a different document; or "
    "REFUSE — put it in \"keep\" with a rationale saying why it stands as "
    "written."
)


def strike_block(rows: list[dict[str, Any]]) -> str:
    """The claims rewording is no longer a move on. Pure.

    A sentence asking the drafter not to reword a twice-failed claim already
    existed inside the finding line, and on the measured run it did not bite:
    one claim was reworded in round five, reworded again in round six, and
    dropped in round seven for a reason that was available in round two. The
    sentence is still there, because a rule is most useful at the point of
    decision. This is the standing version of it — named claims, the three
    moves, and the fact that a fourth is refused by the engine rather than
    discouraged in prose.
    """
    if not rows:
        return ""
    lines = []
    for r in rows:
        refused = int(r.get("rejected_rewords") or 0)
        lines.append(
            f"  - claim {r.get('sequence')}: "
            f"{int(r.get('findings') or 0)} findings"
            + (f"; {refused} revision(s) of it already refused for returning "
               "the same citation" if refused else ""))
    return (
        "\nCLAIMS ON THEIR SECOND FINDING — the review has now failed each of "
        "these more than once, so rewording them is no longer one of your "
        "moves. For each, do exactly one of three things: "
        + PERMITTED_MOVES
        + " A revision that comes back with the citation the claim already "
        "has is not applied to the answer and is recorded as refused.\n"
        + "\n".join(lines) + "\n"
    )


def numbering_key(rows: list[tuple[int, str]]) -> str:
    """How to address a claim in a patch. Pure.

    Not the answer read back. The drafter can see every claim it wrote; what
    it cannot see is the sequence number the engine gave it, because claims
    are reordered on the way into the database and renumbered again as rounds
    drop and add them. A patch keys on that number, so the mapping has to
    travel — and nothing else does: no passages, no rationales, no sections.
    """
    if not rows:
        return ""
    lines = "\n".join(f"  {seq}. {(text or '').strip()[:90]}"
                      for seq, text in rows)
    return ("\nHOW TO ADDRESS A CLAIM — the engine's numbering of the answer "
            "as it stands, and the opening words of each claim so you can "
            'match it to what you wrote. Use these numbers in "seq". This is '
            "not the answer being read back to you; you have it above.\n"
            + lines + "\n")


def removed_by_the_engine(removed: list[dict[str, Any]]) -> str:
    """Claims that left the answer without the drafter deciding it. Pure.

    The one thing the transcript genuinely cannot show. A claim whose evidence
    failed verification is deleted by the engine between rounds, and a drafter
    reading its own turns would still believe it is there.
    """
    if not removed:
        return ""
    lines = "\n".join(
        f"  - claim {r.get('sequence')}: {str(r.get('why') or '')[:200]}"
        for r in removed)
    return ("\nREMOVED FROM THE ANSWER SINCE YOUR LAST TURN — not by you, and "
            "not up for discussion. Their evidence did not survive "
            "verification. If a point one of them made still belongs in the "
            "answer, it has to be made again from a passage that holds.\n"
            + lines + "\n")
