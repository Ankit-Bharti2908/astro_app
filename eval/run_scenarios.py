"""Scenario eval (§16): multi-turn journeys run against a live /chat, then reviewed.

Two things are checked per step, and they answer different questions.

**Structural checks** read the API's own report of the turn — the route it took, the language
it replied in, which memories reached the prompt — and the Shared Brain afterwards. These are
exact: a memory is in the graph or it is not. They catch the failures that matter most and
cannot be argued with.

**The reviewer** reads the reply itself and answers one statement about it, because the thing a
user actually notices is not in any field. "Two contradictory goals are active" is a graph
state; "the assistant told me I'm both switching jobs and not switching jobs" is the defect.
Only the second one is what the user experiences, and only prose can carry it.

    python -m eval.run_scenarios                      # everything, with the reviewer
    python -m eval.run_scenarios --only B07,B08       # a subset
    python -m eval.run_scenarios --no-judge           # structural only, no reviewer calls
    python -m eval.run_scenarios --base-url http://localhost:8000

Writes eval/results/scenarios.md and eval/results/scenarios.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml
from pydantic import BaseModel

HERE = Path(__file__).parent
RESULTS = HERE / "results"

JUDGE_SYSTEM = """<task>
You are reviewing one reply from an astrology chat assistant that keeps a long-term memory of
its users. You are given the conversation so far, the reply under review, and one statement
about that reply. Decide whether the statement holds. Return JSON only.
</task>

<rules>
- Judge the reply as written, against the statement as written. Nothing else is at issue —
  not the reply's style, not its astrology, not whether you would have said it differently.
- Judge what the reply SAYS, not what the system might hold internally.
- Judge ONLY the statement. If it asks whether the reply mentions something, the single
  question is whether it mentions it — not whether what it says is accurate, well dated or
  well phrased. Do not add criteria of your own.
- Relative dates resolve against <today>, which gives you the year. "Next year" means the
  year AFTER the current one, and a reply naming that year is agreeing with the user, not
  contradicting them.
- A statement about what the reply does NOT do holds unless the reply plainly does it.
- When the reply is ambiguous between holding and not holding, say it does not hold and give
  the ambiguity as the reason. A reviewer that resolves doubt in the system's favour measures
  nothing.
</rules>

<output>
- holds: true or false
- why: one sentence, quoting the words that decided it
</output>"""

JUDGE_USER = """<today>
{today}
</today>

<conversation>
{transcript}
</conversation>

<reply_under_review>
{reply}
</reply_under_review>

<statement>
{statement}
</statement>"""


def _today_block() -> str:
    now = datetime.now(UTC)
    return (f"{now:%d %B %Y}. The current year is {now.year}; "
            f'"next year" means {now.year + 1}, "last year" means {now.year - 1}.')


class Verdict(BaseModel):
    holds: bool = False
    why: str = ""


@dataclass
class Check:
    kind: str          # "expect" | "brain" | "judge"
    name: str
    ok: bool
    detail: str = ""


@dataclass
class StepResult:
    say: str
    reply: str = ""
    route: str = ""
    language: str = ""
    context_used: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    error: str | None = None

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


@dataclass
class ScenarioResult:
    id: str
    title: str
    proves: str
    known_issue: str | None
    steps: list[StepResult] = field(default_factory=list)

    @property
    def failures(self) -> list[tuple[int, Check]]:
        return [(i + 1, c) for i, s in enumerate(self.steps) for c in s.failures]

    @property
    def status(self) -> str:
        if not self.failures:
            return "pass"
        return "known" if self.known_issue else "fail"


# ---- the brain, as the API reports it -------------------------------------------

def _alternatives(key: str) -> list[str]:
    return [k.strip() for k in key.split("|")]


def _find(memories: list[dict], key: str) -> dict | None:
    wanted = _alternatives(key)
    for m in memories:
        if m["key"] in wanted:
            return m
    return None


ATTR_FIELDS = {
    "_value": "value", "_life_area": "life_area", "_source": "source",
    "_type": "type", "_valid_until": "valid_until", "_mention_count": "mention_count",
}


def check_memory_attrs(mem: dict, attrs: dict) -> list[str]:
    """Why a memory failed its expectations, or an empty list. `_`-prefixed keys read fields."""
    bad = []
    for attr, want in attrs.items():
        if attr == "_valid_until_set":
            if bool(mem.get("valid_until")) is not bool(want):
                bad.append(f"valid_until {'set' if want else 'unset'} expected, got {mem.get('valid_until')!r}")
        elif attr == "_mention_count_at_least":
            if mem.get("mention_count", 0) < want:
                bad.append(f"mention_count >= {want} expected, got {mem.get('mention_count')}")
        elif attr == "_confidence_at_least":
            if mem.get("confidence", 0) < want:
                bad.append(f"confidence >= {want} expected, got {mem.get('confidence')}")
        elif attr in ATTR_FIELDS:
            got = mem.get(ATTR_FIELDS[attr])
            if want is not None and str(want).lower() not in str(got).lower():
                bad.append(f"{ATTR_FIELDS[attr]}={got!r}, expected to contain {want!r}")
        else:  # an entry in the attributes blob, e.g. target_year
            got = (mem.get("attributes") or {}).get(attr)
            if got != want:
                bad.append(f"attributes.{attr}={got!r}, expected {want!r}")
    return bad


def check_brain(spec: dict, active: list[dict], all_mem: list[dict], profile: dict | None) -> list[Check]:
    out: list[Check] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        out.append(Check("brain", name, ok, detail))

    if "memory_count" in spec:
        add(f"memory_count == {spec['memory_count']}", len(active) == spec["memory_count"],
            f"got {len(active)}: {[m['key'] for m in active]}")
    for key, attrs in (spec.get("active") or {}).items():
        mem = _find(active, key)
        if mem is None:
            add(f"active {key}", False, f"not found; active: {[m['key'] for m in active]}")
            continue
        bad = check_memory_attrs(mem, attrs or {})
        add(f"active {key}", not bad, "; ".join(bad))
    if "active_exactly" in spec:
        want = {k for key in spec["active_exactly"] for k in _alternatives(key)}
        got = {m["key"] for m in active}
        add("active_exactly", bool(got) and got <= want and len(got) == len(spec["active_exactly"]),
            f"got {sorted(got)}, expected {spec['active_exactly']}")
    for key in spec.get("absent") or []:
        add(f"absent {key}", _find(active, key) is None, "still active")
    for area in spec.get("no_active_in_area") or []:
        hits = [m["key"] for m in active if m["life_area"] == area]
        add(f"no active memory in {area}", not hits, f"found {hits}")
    for key, n in (spec.get("superseded") or {}).items():
        wanted = _alternatives(key)
        got = [m for m in all_mem if m["key"] in wanted and m["status"] == "superseded"]
        add(f"superseded {key} == {n}", len(got) == n, f"got {len(got)}")
    for field_name, want in (spec.get("profile") or {}).items():
        got = (profile or {}).get(field_name)
        add(f"profile.{field_name} == {want!r}", got == want, f"got {got!r}")
    return out


def check_expect(spec: dict, body: dict) -> list[Check]:
    out: list[Check] = []
    used = body.get("context_used") or []
    reply = body.get("response") or ""

    def add(name: str, ok: bool, detail: str = "") -> None:
        out.append(Check("expect", name, ok, detail))

    if "route" in spec:
        add(f"route == {spec['route']}", body.get("route") == spec["route"], f"got {body.get('route')}")
    if "language" in spec:
        add(f"language == {spec['language']}", body.get("language") == spec["language"], f"got {body.get('language')}")
    for key in spec.get("context_includes") or []:
        add(f"context has {key}", key in used, f"got {used}")
    for key in spec.get("context_excludes") or []:
        add(f"context lacks {key}", key not in used, f"got {used}")
    if spec.get("no_memory_context"):
        leaked = [k for k in used if k.startswith("memory:")]
        add("no memories in context", not leaked, f"got {leaked}")
    if "degraded" in spec:
        add(f"degraded == {spec['degraded']}", (body.get("degraded") or []) == spec["degraded"], f"got {body.get('degraded')}")
    for word in spec.get("reply_excludes") or []:
        add(f"reply lacks {word!r}", word.lower() not in reply.lower(), "present in the reply")
    return out


# ---- running ----------------------------------------------------------------------

class Runner:
    def __init__(self, client: httpx.AsyncClient, judge, *, verbose: bool) -> None:
        self.client = client
        self.judge = judge
        self.verbose = verbose

    async def chat(self, user_id: str, session_id: str, message: str, *, ephemeral: bool = False) -> dict:
        r = await self.client.post("/chat", json={
            "user_id": user_id, "session_id": session_id, "message": message, "ephemeral": ephemeral,
        })
        r.raise_for_status()
        return r.json()

    async def brain(self, user_id: str) -> tuple[list[dict], list[dict], dict | None]:
        active = (await self.client.get(f"/users/{user_id}/memories")).json()
        every = (await self.client.get(f"/users/{user_id}/memories", params={"status": "all"})).json()
        pr = await self.client.get(f"/users/{user_id}")
        return active, every, (pr.json() if pr.status_code == 200 else None)

    async def review(self, transcript: list[str], reply: str, statement: str) -> Check:
        if self.judge is None:
            return Check("judge", statement, True, "skipped (--no-judge)")
        from app.llm.provider import LLMMessage
        try:
            v = await self.judge.structured(
                "judge",
                [
                    LLMMessage("system", JUDGE_SYSTEM),
                    LLMMessage("user", JUDGE_USER.format(
                        today=_today_block(),
                        transcript="\n".join(transcript[-8:]), reply=reply, statement=statement.strip())),
                ],
                Verdict,
            )
        except Exception as exc:  # noqa: BLE001 — a reviewer outage is not a system failure
            return Check("judge", statement, True, f"reviewer unavailable: {type(exc).__name__}")
        return Check("judge", statement.strip(), v.holds, v.why)

    async def run(self, sc: dict) -> ScenarioResult:
        suffix = uuid.uuid4().hex[:8]
        users = {"default": f"ev-{sc['id'].lower()}-{suffix}"}
        res = ScenarioResult(sc["id"], sc["title"], sc.get("proves", ""), sc.get("known_issue"))
        transcript: list[str] = []

        for step in sc["steps"]:
            user_id = users.setdefault(step.get("as_user", "default"), f"ev-{sc['id'].lower()}-{step.get('as_user')}-{suffix}")
            sr = StepResult(say=step["say"])
            try:
                body = await self.chat(user_id, step.get("session", "s1"), step["say"], ephemeral=bool(step.get("ephemeral")))
            except Exception as exc:  # noqa: BLE001
                sr.error = f"{type(exc).__name__}: {exc}"
                sr.checks.append(Check("expect", "request succeeded", False, sr.error))
                res.steps.append(sr)
                continue

            sr.reply = body.get("response", "")
            sr.route = body.get("route", "")
            sr.language = body.get("language", "")
            sr.context_used = body.get("context_used") or []
            transcript += [f"user: {step['say']}", f"assistant: {sr.reply}"]

            sr.checks += check_expect(step.get("expect") or {}, body)
            if "reply_excludes" in step:  # also accepted at step level
                sr.checks += check_expect({"reply_excludes": step["reply_excludes"]}, body)
            if step.get("brain"):
                active, every, profile = await self.brain(user_id)
                sr.checks += check_brain(step["brain"], active, every, profile)
            if step.get("judge"):
                sr.checks.append(await self.review(transcript, sr.reply, step["judge"]))

            if self.verbose:
                mark = "·" if not sr.failures else "✗"
                print(f"    {mark} [{sr.route:13}] {step['say'][:62]}")
                for c in sr.failures:
                    print(f"        {c.kind}: {c.name} — {c.detail}")
            res.steps.append(sr)
        return res


# ---- reporting --------------------------------------------------------------------

def to_markdown(results: list[ScenarioResult], meta: dict) -> str:
    counts = {s: sum(1 for r in results if r.status == s) for s in ("pass", "known", "fail")}
    lines = [
        "# Scenario eval",
        "",
        f"{len(results)} multi-turn journeys run against a live `/chat`, "
        f"{meta['checks']} checks, reviewer `{meta['judge_model']}`.",
        f"Run {meta['when']} · chat models `{meta['models']}` · brain `{meta['brain']}` · "
        f"prompts `{meta['prompt_version']}` · {meta['seconds']}s.",
        "",
        f"**{counts['pass']} passed · {counts['fail']} failed · {counts['known']} known issue"
        f"{'s' if counts['known'] != 1 else ''}**",
        "",
        "Each scenario checks the API's own report of the turn and the Shared Brain afterwards,",
        "both exactly; a reviewer then reads the reply itself, because what a user notices is the",
        "prose, not the fields. A scenario marked *known issue* fails for a reason already",
        "understood and written down — it is listed apart from a regression, not hidden.",
        "",
        "| | Scenario | Proves | Result |",
        "|---|---|---|---|",
    ]
    icon = {"pass": "✅", "fail": "❌", "known": "⚠️"}
    for r in results:
        verdict = {"pass": "pass", "fail": f"**{len(r.failures)} failed**", "known": "known issue"}[r.status]
        lines.append(f"| {icon[r.status]} | **{r.id}** {r.title} | {r.proves} | {verdict} |")
    lines.append("")

    for r in results:
        lines += [f"## {icon[r.status]} {r.id} — {r.title}", "", f"*{r.proves}*", ""]
        if r.known_issue:
            lines += ["> **Known issue.** " + " ".join(r.known_issue.split()), ""]
        for i, s in enumerate(r.steps, start=1):
            lines.append(f"**{i}. “{s.say}”** — `{s.route}` · `{s.language}`"
                         + (f" · context `{', '.join(s.context_used)}`" if s.context_used else ""))
            lines.append("")
            if s.reply:
                reply = " ".join(s.reply.split())
                lines += ["> " + (reply[:400] + ("…" if len(reply) > 400 else "")), ""]
            for c in s.checks:
                if c.kind == "judge":
                    lines.append(f"- {'✅' if c.ok else '❌'} *reviewer:* {c.name} — {c.detail}")
                elif not c.ok:
                    lines.append(f"- ❌ `{c.name}` — {c.detail}")
            if any(c.kind != "judge" for c in s.checks) and not s.failures:
                lines.append(f"- ✅ {sum(1 for c in s.checks if c.kind != 'judge')} structural checks passed")
            lines.append("")
    return "\n".join(lines)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--only", default="", help="comma-separated scenario ids")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    spec = yaml.safe_load((HERE / "scenarios.yaml").read_text())
    scenarios = spec["scenarios"]
    if args.only:
        wanted = {s.strip().upper() for s in args.only.split(",")}
        scenarios = [s for s in scenarios if s["id"].upper() in wanted]

    judge = None
    judge_model = "none (--no-judge)"
    if not args.no_judge:
        from app.config import Settings
        from app.llm.litellm_provider import LiteLLMProvider
        settings = Settings()
        judge = LiteLLMProvider(settings)
        judge_model = settings.strong_model

    started = time.monotonic()
    async with httpx.AsyncClient(base_url=args.base_url, timeout=180) as client:
        health = (await client.get("/health")).json()
        print(f"→ {args.base_url} · brain {health['brain']['backend']} · "
              f"models {health['llm']['models']} · prompts {health['prompt_version']}\n")
        runner = Runner(client, judge, verbose=not args.quiet)
        results = []
        for sc in scenarios:
            print(f"  {sc['id']} {sc['title']}")
            results.append(await runner.run(sc))

    meta = {
        "when": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "models": health["llm"]["models"],
        "brain": health["brain"]["backend"],
        "prompt_version": health["prompt_version"],
        "judge_model": judge_model,
        "seconds": round(time.monotonic() - started),
        "checks": sum(len(s.checks) for r in results for s in r.steps),
    }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "scenarios.md").write_text(to_markdown(results, meta))
    (RESULTS / "scenarios.json").write_text(json.dumps(
        {"meta": meta, "results": [
            {"id": r.id, "title": r.title, "status": r.status,
             "steps": [{"say": s.say, "route": s.route, "language": s.language,
                        "context_used": s.context_used, "reply": s.reply,
                        "checks": [vars(c) for c in s.checks]} for s in r.steps]}
            for r in results]}, indent=2, default=str))

    counts = {s: sum(1 for r in results if r.status == s) for s in ("pass", "known", "fail")}
    print(f"\n{counts['pass']} passed · {counts['fail']} failed · {counts['known']} known "
          f"· {meta['checks']} checks · {meta['seconds']}s")
    for r in results:
        if r.status == "fail":
            print(f"  ❌ {r.id} {r.title}")
            for step_no, c in r.failures:
                print(f"       step {step_no} {c.kind}: {c.name} — {c.detail}")
    print(f"\nwrote {RESULTS / 'scenarios.md'}")
    return 1 if counts["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
