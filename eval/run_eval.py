"""Memory-accuracy eval : storage-decision precision/recall and label accuracy on the labelled set,
plus a confidence-threshold sweep that validates the 0.6 storage threshold.

    python -m eval.run_eval                          # runs the real extraction model (needs provider keys)
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings
from app.graphs.nodes.memory_ops import extract_candidates, filter_candidates, score_candidates

HERE = Path(__file__).parent
NOW = datetime(2026, 10, 17, 9, 30, tzinfo=UTC)


def prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": round(p, 3), "recall": round(r, 3), "f1": round(f1, 3), "tp": tp, "fp": fp, "fn": fn}


async def run(thresholds: list[float]) -> dict:
    from app.llm.litellm_provider import LiteLLMProvider

    items = json.loads((HERE / "labelled_set.json").read_text())
    settings = Settings()
    llm = LiteLLMProvider(settings)
    rows = []
    for item in items:
        try:
            cands = await extract_candidates(llm, message=item["message"], now=NOW, tz=settings.default_timezone, context=item.get("context", ""))
        except Exception as exc:  # noqa: BLE001
            cands, err = [], str(exc)
        else:
            err = None
        scored = score_candidates(cands)
        rows.append({"item": item, "scored": scored, "error": err})

    # storage decision at each threshold
    sweep = {}
    for th in thresholds:
        s = Settings(_env_file=None, confidence_threshold=th)
        tp = fp = fn = tn = 0
        for row in rows:
            accepted, _ = filter_candidates([c.model_copy() for c in row["scored"]], s)
            predicted = bool(accepted)
            expected = row["item"]["store"]
            tp += predicted and expected
            fp += predicted and not expected
            fn += (not predicted) and expected
            tn += (not predicted) and not expected
        sweep[str(th)] = {**prf(tp, fp, fn), "tn": tn}

    # label accuracy on expected candidates (matched by key)
    label_total = {"certainty": 0, "subject": 0, "type": 0}
    label_hits = {"certainty": 0, "subject": 0, "type": 0}
    missed_keys, extra_keys = [], []
    for row in rows:
        got = {c.key: c for c in row["scored"]}
        expected_keys = {e["key"] for e in row["item"]["expected"]}
        for e in row["item"]["expected"]:
            c = got.get(e["key"])
            if c is None:
                missed_keys.append((row["item"]["id"], e["key"]))
                continue
            for label in ("certainty", "subject", "type"):
                label_total[label] += 1
                label_hits[label] += getattr(c, label) == e[label]
        extra_keys += [(row["item"]["id"], k) for k in got if k not in expected_keys]
    label_acc = {k: round(label_hits[k] / label_total[k], 3) if label_total[k] else None for k in label_total}

    from app.prompts.templates import PROMPT_VERSION

    report = {
        "when": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "model": settings.small_model,
        "prompt_version": PROMPT_VERSION,
        "items": len(items),
        "storage_decision_by_threshold": sweep,
        "default_threshold": "0.6",
        "label_accuracy_on_expected_candidates": label_acc,
        "candidate_extraction": {"expected": sum(len(r["item"]["expected"]) for r in rows), "missed": missed_keys, "unexpected": extra_keys},
        "errors": [(r["item"]["id"], r["error"]) for r in rows if r["error"]],
    }
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "latest.json").write_text(json.dumps(report, indent=2, default=str))
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--thresholds", default="0.3,0.5,0.6,0.7,0.9")
    args = ap.parse_args()
    report = asyncio.run(run([float(t) for t in args.thresholds.split(",")]))
    print(f"model={report['model']} prompts={report['prompt_version']} items={report['items']}")
    print("threshold  precision  recall  f1     tp fp fn tn")
    for th, m in report["storage_decision_by_threshold"].items():
        print(f"{th:>9}  {m['precision']:9.3f}  {m['recall']:6.3f}  {m['f1']:5.3f}  {m['tp']:2d} {m['fp']:2d} {m['fn']:2d} {m['tn']:2d}")
    print("label accuracy:", report["label_accuracy_on_expected_candidates"])
    print("missed candidates:", report["candidate_extraction"]["missed"])
    print("unexpected candidates:", report["candidate_extraction"]["unexpected"])
    if report["errors"]:
        print("errors:", report["errors"])


if __name__ == "__main__":
    main()
