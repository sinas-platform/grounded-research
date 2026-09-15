"""Answer quality rubric: an LLM judge over a rendered answer and its question.

answer_regress scores an answer against a gold standard — were the right
sources cited. Expert review of a set of published answers found a second
kind of defect that no gold standard catches, because it is about the shape
of the answer rather than its sources: the conclusion missing or last; the
later parts of a multi-part question barely answered; a legal test dissolved
into prose instead of set out as ordered conditions; commentary and opinions
presented at the same level as judgments; authorities cited by filename;
the same holding restated twice; superseded law presented as current.

This module scores those nine things, 0/1/2 each with a one-line reason, so
a change to the drafter can be judged on all answers at once rather than by
reading them one at a time. A model judges; where the text itself settles a
point (a filename is a filename), a deterministic pre-check caps the score
so the judge cannot be generous about it.

    python -m app.answer_rubric score <dir>            # <name>.md -> <name>.rubric.json
    python -m app.answer_rubric compare <base> <cand>  # per-criterion deltas

An answer file is markdown whose first `# ` heading is the question; a
sidecar `<name>.question.txt` overrides it. The judge is reached the way
the drafter is — the configured Sinas agent — so the scorer runs on the
deployment's model without a second provider setup.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

JUDGE_AGENT = "sgr/retrieval-planner-agent"
SCORES = (0, 1, 2)

# Name -> what the judge is told to look for. The wording is the rubric;
# changing it changes every score after it, so it lives in one place.
CRITERIA: dict[str, str] = {
    "conclusion_first": (
        "The answer opens with its conclusion — the direct answer to the "
        "question — before the analysis, instead of leaving it to the end "
        "or omitting it. 2: a conclusion covering every part of the "
        "question comes first; 1: a conclusion exists but comes late or "
        "covers only some parts; 0: no conclusion."),
    "parts_answered": (
        "Every part of the question is answered with substance: at least "
        "one claim per part that actually addresses it, not a passing "
        "mention. 2: every part answered with substance; 1: some parts thin "
        "or only touched; 0: a part is not answered at all."),
    "tests_as_conditions": (
        "Where a source states a legal test with two or more conditions, "
        "the answer sets the conditions out as an ordered list and says "
        "whether they are cumulative, instead of dissolving them into "
        "prose. 2: every test is set out as ordered conditions; 1: "
        "conditions are named but not ordered, or only some tests are set "
        "out; 0: a test the question turns on is dissolved into prose or "
        "absent. If the question calls for no test, score 2."),
    "authorities_labelled": (
        "Material that is not binding authority — commentary, advocate "
        "general or similar advisory opinions, party submissions, regulator "
        "decisions, sources from another jurisdiction — is labelled as "
        "such, and no rule rests on such a source alone. 2: every such "
        "source is labelled and none carries a rule alone; 1: labels are "
        "partly present; 0: commentary or submissions are presented as the "
        "law."),
    "citations_complete": (
        "Every authority cited carries a name, a case or instrument number "
        "and a date, plus an ECLI and paragraph number where the source "
        "has them. Never a filename and never a bare identifier. 2: "
        "complete throughout; 1: mostly named but numbers or dates often "
        "missing; 0: authorities identified by filename or by a bare "
        "number."),
    "no_duplicate_authorities": (
        "Each authority appears once per proposition: the same holding is "
        "not restated in two claims and the same source is not cited twice "
        "for the same point. 2: no repetition; 1: one repeated authority or "
        "near-duplicate claim; 0: several."),
    "no_off_topic": (
        "Every claim contributes to answering the question. 2: nothing off "
        "topic; 1: one or two claims describe a source without contributing "
        "to the answer; 0: several off-topic claims or a digression."),
    "no_contradictions": (
        "The claims are consistent with one another, and where sources "
        "point in different directions the answer reconciles them into a "
        "position. 2: consistent; 1: an unreconciled tension; 0: claims "
        "contradict each other outright."),
    "currency_flagged": (
        "Superseded, repealed or replaced instruments, earlier rulings "
        "overtaken later in the same case, and sources from a jurisdiction "
        "other than the question's are flagged as such, and the answer "
        "says as at what date the law is stated. 2: flagged wherever it "
        "arises and dated; 1: partly; 0: superseded or other-jurisdiction "
        "material presented as current law. If nothing needs flagging and "
        "the answer is dated, score 2."),
}

# Column codes for the table; same order as CRITERIA.
SHORT = {"conclusion_first": "concl", "parts_answered": "parts",
         "tests_as_conditions": "tests", "authorities_labelled": "label",
         "citations_complete": "cite", "no_duplicate_authorities": "dedup",
         "no_off_topic": "topic", "no_contradictions": "consis",
         "currency_flagged": "curr"}

Invoke = Callable[[str], Awaitable[str]]


class RubricError(ValueError):
    """The judge replied with JSON that is not a rubric verdict."""


# ── reading an answer ────────────────────────────────────────────────────────

def split_answer(md: str) -> tuple[str | None, str]:
    """(question, body): the question is the first `# ` heading."""
    lines = (md or "").splitlines()
    for i, line in enumerate(lines):
        if line.startswith("# "):
            return line[2:].strip(), "\n".join(lines[i + 1:]).strip()
        if line.strip():
            break
    return None, (md or "").strip()


def load_answer(path: Path) -> tuple[str, str]:
    """Question and body for `<name>.md`; a `<name>.question.txt` sidecar
    wins over the heading. No question anywhere is an error, not a guess."""
    question, body = split_answer(path.read_text())
    sidecar = path.with_suffix(".question.txt")
    if sidecar.exists() and sidecar.read_text().strip():
        question = sidecar.read_text().strip()
    if not question:
        raise ValueError(f"{path.name}: no `# ` heading and no {sidecar.name}")
    return question, body


_CLAIM_START = re.compile(r"^\s*(?:\*\*)?\d+\.(?:\*\*)?\s+")


def claims(body: str) -> list[str]:
    """The answer as a list of claims: numbered paragraphs (`**1.** …` or
    `1. …`) when it has them, otherwise every non-heading paragraph."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", body or "") if p.strip()]
    numbered = [p for p in paras if _CLAIM_START.match(p)]
    if numbered:
        return [_CLAIM_START.sub("", p, count=1) for p in numbered]
    return [p for p in paras if not p.lstrip().startswith("#")]


_PART_SPLIT = re.compile(
    r",?\s+\b(?:and|or)\s+(?=(?:what|how|whether|which|when|where|who|why|"
    r"does|do|is|are|can|could|must|may|should|would|will|to what extent)\b)",
    re.I)


def question_parts(question: str) -> list[str]:
    """The question's parts, deterministically: one per question mark, and a
    sentence that asks two things joined by "and what / and how / …" splits
    again. The judge scores parts_answered against exactly this list, so
    two runs of the scorer on the same question see the same parts."""
    parts: list[str] = []
    for sent in re.split(r"(?<=\?)\s+", (question or "").strip()):
        sent = sent.strip()
        if not sent:
            continue
        for piece in _PART_SPLIT.split(sent):
            piece = piece.strip().rstrip("?").strip()
            if piece:
                parts.append(piece[0].upper() + piece[1:] + "?")
    return parts or [question.strip()]


# ── deterministic pre-checks ─────────────────────────────────────────────────

_FILENAME = re.compile(r"(?<![\w/])[\w][\w.\-]*\.(?:md|pdf|txt|html?|docx?)\b")
_CASE_NAME = re.compile(
    r"\b[A-Z][\w&'.\-]*(?:\s+[\w&'.\-]+){0,7}\s+v\.?\s+[A-Z][\w&'.\-]*"   # X v Y
    r"|\bCase\s+[A-Z]*[./-]?\s?\d[\w./-]*\s*(?:\(|[–—-]\s)[A-Z]")         # Case AT.1234 (Caption)
_INSTRUMENT = re.compile(
    r"\b(?:Regulation|Directive|Decision|Act|Code|Charter|Convention|Treaty|"
    r"Guidelines|Notice|Statute)\b")
_NUMBER = re.compile(
    r"\b[A-Z]{1,2}-\d{1,4}/\d{2,4}(?:\s?P)?\b"          # court dockets T-135/09, C-550/07 P
    r"|\bCase\s+\d{1,4}/\d{2,4}\b"                       # Case 155/79
    r"|\b(?:AT|COMP|IV|M)[./]\s?\d{3,6}(?:\.\d+)?\b"     # regulator case numbers
    r"|\b(?:No\.?\s+)?\d{1,4}/\d{4}\b"                   # instrument numbers 1/2003
    r"|\b\d{4}/\d{1,4}\b"                                # 2018/1725
    r"|\bECLI:[A-Z]{2}:[A-Z0-9]+:\d{4}:[A-Z0-9.]+\b"
    r"|\bEU:[CTF]:\d{4}:\d+\b")
_ECLI = re.compile(r"\bECLI:[A-Z]{2}:[A-Z0-9]+:\d{4}:[A-Z0-9.]+\b|\bEU:[CTF]:\d{4}:\d+\b")
_MONTH = (r"(?:January|February|March|April|May|June|July|August|September|"
          r"October|November|December)")
_DATE = re.compile(rf"\b\d{{1,2}}\s+{_MONTH}\s+\d{{4}}\b|\b{_MONTH}\s+\d{{4}}\b"
                   r"|\b\d{4}-\d{2}-\d{2}\b")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_PARA = re.compile(r"\b(?:para(?:graph)?s?\.?|points?|recital)\s+\(?\d+", re.I)


def citation_precheck(body: str) -> dict:
    """What the text settles about citations before any judge reads it.

    A filename is not a citation, whatever the judge thinks of the prose
    around it; and a claim that names no authority and gives no number
    cites nothing. The result caps `citations_complete`: 2 leaves the
    judge free, 1 and 0 are ceilings."""
    cl = claims(body)
    filenames = sorted({m.group(0) for m in _FILENAME.finditer(body or "")})
    per = []
    for i, c in enumerate(cl, start=1):
        prose = _FILENAME.sub(" ", c)
        # a year inside "1/2003" is the instrument's number, not a date
        undated = _NUMBER.sub(" ", prose)
        per.append({
            "claim": i,
            "name": bool(_CASE_NAME.search(prose) or _INSTRUMENT.search(prose)),
            "number": bool(_NUMBER.search(prose)),
            "date": bool(_DATE.search(prose)),
            "year": bool(_DATE.search(prose) or _YEAR.search(undated)),
            "ecli": bool(_ECLI.search(prose)),
            "para": bool(_PARA.search(prose)),
        })
    named = sum(1 for p in per if p["name"] and p["number"])
    share = named / len(per) if per else 0.0
    if filenames:
        cap = 0 if share < 0.5 else 1
    else:
        cap = 2 if share >= 0.8 else 1 if share >= 0.3 else 0
    return {
        "claims": len(per),
        "filename_citations": filenames,
        "with_name_and_number": named,
        "with_date": sum(1 for p in per if p["date"]),
        "with_year_only": sum(1 for p in per if p["year"] and not p["date"]),
        "with_ecli": sum(1 for p in per if p["ecli"]),
        "with_paragraph": sum(1 for p in per if p["para"]),
        "cap": cap,
        "per_claim": per,
    }


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 3}


def duplicate_precheck(body: str, threshold: float = 0.5) -> dict:
    """Near-duplicate claims (token overlap ≥ threshold) and authorities
    cited from more than two claims. Only the duplicate pairs cap
    `no_duplicate_authorities` — one source may legitimately carry several
    propositions, so repeated numbers are evidence for the judge, not a
    ceiling."""
    cl = claims(body)
    toks = [_tokens(_FILENAME.sub(" ", c)) for c in cl]
    pairs = []
    for i in range(len(cl)):
        for j in range(i + 1, len(cl)):
            if not toks[i] or not toks[j]:
                continue
            sim = len(toks[i] & toks[j]) / len(toks[i] | toks[j])
            if sim >= threshold:
                pairs.append({"claims": [i + 1, j + 1], "overlap": round(sim, 2)})
    cited: dict[str, list[int]] = {}
    for i, c in enumerate(cl, start=1):
        for m in _NUMBER.finditer(_FILENAME.sub(" ", c)):
            key = re.sub(r"\s+", " ", m.group(0)).removeprefix("No. ").removeprefix("No ")
            cited.setdefault(key, [])
            if i not in cited[key]:
                cited[key].append(i)
    repeated = {k: v for k, v in cited.items() if len(v) > 2}
    return {
        "near_duplicate_pairs": pairs,
        "authorities_cited_thrice_or_more": repeated,
        "cap": 2 if not pairs else 1 if len(pairs) == 1 else 0,
    }


_CURRENCY_MARKERS = re.compile(
    r"\b(?:repealed|superseded|replaced by|no longer in force|since replaced|"
    r"predecessor|as at|stated as at|set aside|overturned|reversed on appeal|"
    r"under national law|national law)\b", re.I)


def currency_precheck(body: str) -> dict:
    """Evidence only: does the answer use any currency or jurisdiction
    language at all, and does it carry a "law stated as at" date. Which
    instruments are superseded is domain knowledge the judge supplies."""
    markers = sorted({m.group(0).lower() for m in _CURRENCY_MARKERS.finditer(body or "")})
    return {"markers": markers,
            "dated": bool(re.search(r"\b(?:law\s+)?stated\s+as\s+at\b", body or "", re.I))}


def prechecks(body: str) -> dict:
    return {"citations": citation_precheck(body),
            "duplicates": duplicate_precheck(body),
            "currency": currency_precheck(body)}


# ── the judge ────────────────────────────────────────────────────────────────

def _prompt(question: str, body: str, parts: list[str], pre: dict) -> str:
    crit = "\n".join(f"- {k}: {v}" for k, v in CRITERIA.items())
    plist = "\n".join(f"{i}. {p}" for i, p in enumerate(parts, start=1))
    evidence = {
        "citations": {k: v for k, v in pre["citations"].items() if k != "per_claim"},
        "duplicates": pre["duplicates"],
        "currency": pre["currency"],
    }
    return (
        "Score the ANSWER below against the QUESTION on nine criteria. You "
        "are judging the quality of a legal research answer, not its "
        "correctness: whether it is shaped so that a reader can rely on it. "
        "For every criterion give an integer score 0, 1 or 2 and ONE line "
        "of reason that names the claim numbers deciding it.\n\n"
        f"CRITERIA:\n{crit}\n\n"
        "QUESTION PARTS — score parts_answered per part, against exactly "
        f"this list, in this order:\n{plist}\n\n"
        "PRE-CHECKS computed from the text (evidence, not a verdict):\n"
        + json.dumps(evidence) + "\n\n"
        'Reply ONLY JSON: {"criteria": {'
        + ", ".join(f'"{k}": {{"score": 0|1|2, "reason": "<one line>"}}'
                    for k in CRITERIA)
        + '}, "parts": [{"part": "<part text>", "score": 0|1|2, "reason": '
        '"<one line>"}], "verdict": "<one line a reader should know about '
        'this answer\'s quality>"}\n\n'
        f"QUESTION:\n{question}\n\nANSWER:\n{body}"
    )


def _rubric_json(reply: str) -> dict:
    """The judge's reply as JSON, or JSONDecodeError naming what broke."""
    cleaned = (reply or "").strip().strip("`").removeprefix("json").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise json.JSONDecodeError("no JSON object in reply", cleaned or "", 0)
    return json.loads(cleaned[start:end + 1])


def _validate(data: dict, parts: list[str]) -> dict:
    """A verdict with every criterion scored 0/1/2 and a line of reason, and
    one entry per question part. Raises RubricError naming what is missing
    so the retry can hand the judge its own mistake."""
    if not isinstance(data, dict):
        raise RubricError("top level is not an object")
    crit = data.get("criteria")
    if not isinstance(crit, dict):
        raise RubricError('missing "criteria" object')
    out: dict[str, dict] = {}
    for name in CRITERIA:
        entry = crit.get(name)
        if not isinstance(entry, dict):
            raise RubricError(f'criterion "{name}" missing')
        score = entry.get("score")
        if isinstance(score, bool) or score not in SCORES:
            raise RubricError(f'criterion "{name}": score must be 0, 1 or 2, got {score!r}')
        reason = str(entry.get("reason") or "").strip()
        if not reason:
            raise RubricError(f'criterion "{name}": reason missing')
        out[name] = {"score": int(score), "reason": reason.splitlines()[0]}
    raw_parts = data.get("parts")
    if not isinstance(raw_parts, list) or len(raw_parts) != len(parts):
        raise RubricError(f'"parts" must list exactly {len(parts)} entries, one per question part')
    scored_parts = []
    for i, (want, got) in enumerate(zip(parts, raw_parts), start=1):
        if not isinstance(got, dict):
            raise RubricError(f"part {i} is not an object")
        score = got.get("score")
        if isinstance(score, bool) or score not in SCORES:
            raise RubricError(f"part {i}: score must be 0, 1 or 2, got {score!r}")
        scored_parts.append({"part": want, "score": int(score),
                             "reason": str(got.get("reason") or "").strip().splitlines()[0]
                             if str(got.get("reason") or "").strip() else ""})
    verdict = str(data.get("verdict") or "").strip().splitlines()
    return {"criteria": out, "parts": scored_parts,
            "verdict": verdict[0] if verdict else ""}


def apply_caps(judged: dict, pre: dict) -> dict:
    """The judge's scores with the deterministic ceilings applied. A cap
    that bites is recorded next to the judge's own score, so a reader can
    see which of the two is talking."""
    caps = {"citations_complete": pre["citations"]["cap"],
            "no_duplicate_authorities": pre["duplicates"]["cap"]}
    out = {}
    for name, entry in judged.items():
        judge = entry["score"]
        cap = caps.get(name)
        final = min(judge, cap) if cap is not None else judge
        row = {"score": final, "reason": entry["reason"]}
        if cap is not None and cap < judge:
            row["judge_score"] = judge
            row["cap"] = cap
            row["reason"] += f" [capped at {cap} by pre-check]"
        out[name] = row
    return out


async def score_answer(question: str, body: str, invoke: Invoke,
                       agent: str = JUDGE_AGENT) -> dict:
    """One judged rubric for one answer. `invoke(message) -> reply` is the
    model; the caller decides which."""
    parts = question_parts(question)
    pre = prechecks(body)
    reply = await invoke(_prompt(question, body, parts, pre))
    # One retry on a malformed or incomplete reply, handing the judge the
    # error it made; a second failure still raises. Same shape as the
    # drafter: the retry is cheap, silence is not.
    try:
        judged = _validate(_rubric_json(reply), parts)
    except (json.JSONDecodeError, RubricError) as exc:
        reply = await invoke(
            "Your previous reply was not a valid rubric verdict: "
            + str(exc)[:200]
            + ". Send the same judgement again as strictly valid JSON with "
            "every one of the nine criteria scored 0, 1 or 2 with a reason, "
            f"exactly {len(parts)} entries under \"parts\", and a verdict. "
            'Escape every quotation mark inside a string as \\", and use no '
            "line breaks inside a string.\n\nPREVIOUS REPLY:\n" + reply[:60000])
        judged = _validate(_rubric_json(reply), parts)
    criteria = apply_caps(judged["criteria"], pre)
    total = sum(c["score"] for c in criteria.values())
    return {
        "question": question,
        "question_parts": parts,
        "criteria": criteria,
        "parts_answered": judged["parts"],
        "prechecks": pre,
        "total": total,
        "max": 2 * len(CRITERIA),
        "verdict": f"{total}/{2 * len(CRITERIA)} — {judged['verdict']}",
        "judge": {"agent": agent},
    }


def _sinas_invoke(agent: str) -> Invoke:
    """The drafter's own path to the model: the configured Sinas agent."""
    from app.services.query_runner import _Sinas

    client = _Sinas()

    async def invoke(message: str) -> str:
        return await client.invoke(agent, message)

    return invoke


# ── CLI ──────────────────────────────────────────────────────────────────────

def answer_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("*.md")
                  if not p.name.endswith(".rubric.md"))


def rubric_path(answer: Path) -> Path:
    return answer.with_name(answer.stem + ".rubric.json")


def _header() -> str:
    cols = " ".join(f"{SHORT[k]:>6}" for k in CRITERIA)
    return f"{'answer':12} {cols} {'total':>6}  verdict"


def _row(name: str, r: dict) -> str:
    cols = " ".join(f"{r['criteria'][k]['score']:>6}" for k in CRITERIA)
    return f"{name[:11]:12} {cols} {r['total']:>3}/{r['max']:<2}  {r['verdict'].split(' — ', 1)[-1][:70]}"


def _means(rows: list[dict]) -> str:
    n = len(rows)
    cols = " ".join(
        f"{sum(r['criteria'][k]['score'] for r in rows) / n:>6.2f}" for k in CRITERIA)
    return f"{'mean':12} {cols} {sum(r['total'] for r in rows) / n:>5.1f}"


async def score_dir(directory: Path, agent: str = JUDGE_AGENT,
                    invoke: Invoke | None = None) -> list[tuple[str, dict]]:
    """Score every answer in the directory, writing `<name>.rubric.json`
    beside each. One at a time: a judge call is seconds and the point is
    the table, not throughput."""
    invoke = invoke or _sinas_invoke(agent)
    files = answer_files(directory)
    if not files:
        raise SystemExit(f"no *.md answers in {directory}")
    print(_header())
    print("-" * 100)
    rows = []
    for path in files:
        question, body = load_answer(path)
        r = await score_answer(question, body, invoke, agent=agent)
        rubric_path(path).write_text(json.dumps(r, indent=1, ensure_ascii=False))
        rows.append((path.stem, r))
        print(_row(path.stem, r), flush=True)
    print("-" * 100)
    print(_means([r for _, r in rows]))
    return rows


def _load_rubrics(directory: Path) -> dict[str, dict]:
    return {p.name.removesuffix(".rubric.json"): json.loads(p.read_text())
            for p in sorted(directory.glob("*.rubric.json"))}


def compare(baseline: dict[str, dict], candidate: dict[str, dict]) -> dict:
    """Per-criterion deltas, candidate minus baseline, over the answers both
    directories scored. Returns {"answers": {name: {criterion: delta}},
    "mean": {criterion: mean delta}, "total": {name: delta}}."""
    names = sorted(set(baseline) & set(candidate))
    answers, totals = {}, {}
    for n in names:
        b, c = baseline[n], candidate[n]
        answers[n] = {k: c["criteria"][k]["score"] - b["criteria"][k]["score"]
                      for k in CRITERIA}
        totals[n] = c["total"] - b["total"]
    mean = {k: (sum(answers[n][k] for n in names) / len(names)) if names else 0.0
            for k in CRITERIA}
    return {"answers": answers, "mean": mean, "total": totals,
            "only_baseline": sorted(set(baseline) - set(candidate)),
            "only_candidate": sorted(set(candidate) - set(baseline))}


def print_compare(baseline: dict[str, dict], candidate: dict[str, dict]) -> None:
    d = compare(baseline, candidate)
    names = sorted(d["answers"])
    if not names:
        print("no answer scored in both directories")
        return
    print(f"{'criterion':26} " + " ".join(f"{n[:6]:>6}" for n in names) + f" {'mean':>6}")
    print("-" * (34 + 7 * len(names)))
    for k in CRITERIA:
        cells = " ".join(f"{d['answers'][n][k]:>+6d}" for n in names)
        print(f"{k:26} {cells} {d['mean'][k]:>+6.2f}")
    print("-" * (34 + 7 * len(names)))
    cells = " ".join(f"{d['total'][n]:>+6d}" for n in names)
    print(f"{'total':26} {cells} {sum(d['total'].values()) / len(names):>+6.2f}")
    for label in ("only_baseline", "only_candidate"):
        if d[label]:
            print(f"{label.replace('_', ' ')}: {', '.join(d[label])}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.answer_rubric")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("score", help="score every <name>.md in a directory")
    sc.add_argument("dir", type=Path)
    sc.add_argument("--agent", default=JUDGE_AGENT,
                    help="Sinas agent to judge with (default: the drafter's)")
    cp = sub.add_parser("compare", help="per-criterion deltas between two scored directories")
    cp.add_argument("baseline", type=Path)
    cp.add_argument("candidate", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "score":
        asyncio.run(score_dir(args.dir, agent=args.agent))
    else:
        print_compare(_load_rubrics(args.baseline), _load_rubrics(args.candidate))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
