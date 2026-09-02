"""A/B gain-report math over `memory_eval` records.

Groups records by `memory_mode`, computes per-group summaries, and the off-vs-
hivemind deltas with an effect size (Cohen's d, pooled SD) and a nonparametric
significance test (Mann-Whitney U, two-sided, normal approximation with tie +
continuity correction). PURE PYTHON via `statistics` — the dispatcher venv has
NO scipy/numpy. Below 2 samples per arm, Cohen's d and the p-value are `None`
(not a fabricated number). Renders the `memory_gain_report` markdown.

HARD INVARIANT: imports NO hivemind module. The off rows are the permanent
control arm; this module only reads, never rewrites, the sink.
"""

from __future__ import annotations

import statistics
from typing import Any

# Modes rendered in a stable order; any other observed mode is appended after.
_MODE_ORDER = ("off", "hivemind", "holographic")

# recall_mode arms rendered in a stable order; any other observed mode appended.
_RECALL_MODE_ORDER = ("off", "push", "pull")


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def group_by_mode(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-`memory_mode` summary. `recall_hit_rate` is `0.0` when the denominator
    (`sum(recall_calls)`) is `0`."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        mode = str(rec.get("memory_mode", "off"))
        buckets.setdefault(mode, []).append(rec)

    groups: dict[str, dict[str, Any]] = {}
    for mode, rows in buckets.items():
        tokens_out = [float(r.get("plan_tokens_out", 0) or 0) for r in rows]
        wall_ms = [float(r.get("plan_wall_ms", 0) or 0) for r in rows]
        decisions_reused = [float(r.get("decisions_reused", 0) or 0) for r in rows]
        prior_art_tokens = [float(r.get("prior_art_tokens", 0) or 0) for r in rows]
        rubric_score = [float(r.get("rubric_score", 0) or 0) for r in rows]
        sum_calls = sum(int(r.get("recall_calls", 0) or 0) for r in rows)
        sum_hits = sum(int(r.get("recall_hits", 0) or 0) for r in rows)
        groups[mode] = {
            "specs_completed": len(rows),
            "mean_plan_tokens_out": _mean(tokens_out),
            "median_plan_tokens_out": _median(tokens_out),
            "mean_plan_wall_ms": _mean(wall_ms),
            "recall_hit_rate": (sum_hits / sum_calls) if sum_calls else 0.0,
            "mean_decisions_reused": _mean(decisions_reused),
            "mean_prior_art_tokens": _mean(prior_art_tokens),
            "mean_rubric_score": _mean(rubric_score),
            "median_rubric_score": _median(rubric_score),
        }
    return groups


def group_by_recall_mode(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-`recall_mode` summary (push / pull / off). Records without an explicit
    `recall_mode` default to `"push"` (the legacy/default arm), so older sink files
    aggregate cleanly. Mirrors `group_by_mode`'s metric set so the recall-mode table
    is directly comparable."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        rmode = str(rec.get("recall_mode", "push") or "push")
        buckets.setdefault(rmode, []).append(rec)

    groups: dict[str, dict[str, Any]] = {}
    for rmode, rows in buckets.items():
        tokens_out = [float(r.get("plan_tokens_out", 0) or 0) for r in rows]
        wall_ms = [float(r.get("plan_wall_ms", 0) or 0) for r in rows]
        decisions_reused = [float(r.get("decisions_reused", 0) or 0) for r in rows]
        prior_art_tokens = [float(r.get("prior_art_tokens", 0) or 0) for r in rows]
        decisions_distilled = [float(r.get("decisions_distilled", 0) or 0) for r in rows]
        decisions_deduped = [float(r.get("decisions_deduped", 0) or 0) for r in rows]
        rubric_score = [float(r.get("rubric_score", 0) or 0) for r in rows]
        sum_calls = sum(int(r.get("recall_calls", 0) or 0) for r in rows)
        sum_hits = sum(int(r.get("recall_hits", 0) or 0) for r in rows)
        groups[rmode] = {
            "specs_completed": len(rows),
            "mean_plan_tokens_out": _mean(tokens_out),
            "mean_plan_wall_ms": _mean(wall_ms),
            "recall_hit_rate": (sum_hits / sum_calls) if sum_calls else 0.0,
            "mean_decisions_reused": _mean(decisions_reused),
            "mean_prior_art_tokens": _mean(prior_art_tokens),
            "mean_decisions_distilled": _mean(decisions_distilled),
            "mean_decisions_deduped": _mean(decisions_deduped),
            "mean_rubric_score": _mean(rubric_score),
            "median_rubric_score": _median(rubric_score),
        }
    return groups


def cohens_d(a: list[float], b: list[float]) -> float | None:
    """Pooled-SD effect size (mean(a) - mean(b)) / pooled_sd. `None` if either
    arm has < 2 samples or the pooled SD is 0."""
    if len(a) < 2 or len(b) < 2:
        return None
    sd_a = statistics.pstdev(a)
    sd_b = statistics.pstdev(b)
    n_a, n_b = len(a), len(b)
    pooled_var = ((n_a * sd_a ** 2) + (n_b * sd_b ** 2)) / (n_a + n_b)
    pooled_sd = pooled_var ** 0.5
    if pooled_sd == 0:
        return None
    return (statistics.fmean(a) - statistics.fmean(b)) / pooled_sd


def mann_whitney_u(a: list[float], b: list[float]) -> float | None:
    """Two-sided Mann-Whitney U p-value via the normal approximation with a tie
    correction and a 0.5 continuity correction. `None` if either arm has < 2
    samples. Pure Python (`statistics.NormalDist`)."""
    n1, n2 = len(a), len(b)
    if n1 < 2 or n2 < 2:
        return None

    combined = [(v, 0) for v in a] + [(v, 1) for v in b]
    combined.sort(key=lambda t: t[0])

    # Average ranks, accounting for ties.
    ranks = [0.0] * len(combined)
    tie_terms = 0.0
    i = 0
    while i < len(combined):
        j = i
        while j + 1 < len(combined) and combined[j + 1][0] == combined[i][0]:
            j += 1
        avg_rank = (i + 1 + j + 1) / 2.0  # 1-based ranks
        for k in range(i, j + 1):
            ranks[k] = avg_rank
        t = j - i + 1
        if t > 1:
            tie_terms += (t ** 3 - t)
        i = j + 1

    rank_sum_a = sum(rank for rank, (_, group) in zip(ranks, combined) if group == 0)
    u_a = rank_sum_a - n1 * (n1 + 1) / 2.0
    u_b = n1 * n2 - u_a
    u = min(u_a, u_b)

    n = n1 + n2
    mu = n1 * n2 / 2.0
    # Variance with tie correction.
    sigma_sq = (n1 * n2 / 12.0) * ((n + 1) - tie_terms / (n * (n - 1)))
    if sigma_sq <= 0:
        return 1.0
    sigma = sigma_sq ** 0.5

    # Continuity correction toward the mean.
    z = (u - mu + 0.5) / sigma if u < mu else (u - mu - 0.5) / sigma
    nd = statistics.NormalDist()
    p_two_sided = 2.0 * nd.cdf(-abs(z))
    return max(0.0, min(1.0, p_two_sided))


def _pct_delta(off_mean: float, other_mean: float) -> float | None:
    """Percent change from the off baseline. `None` when the off mean is 0."""
    if off_mean == 0:
        return None
    return (other_mean - off_mean) / off_mean * 100.0


def _delta(records_by_mode: dict[str, list[dict[str, Any]]], field: str) -> dict[str, Any]:
    if field == "rubric_score":
        # Exclude records where rubric_score == 0 (sentinel for "not A/B-scored"):
        # mixing unscored (all-zeros) records with scored ones silences pct_delta,
        # inflates Cohen's d, and degenerates Mann-Whitney. Only scored records
        # (rubric_score > 0) contribute to the rubric delta.
        off_all = records_by_mode.get("off", [])
        hive_all = records_by_mode.get("hivemind", [])
        off = [float(r.get(field, 0) or 0) for r in off_all if int(r.get(field, 0) or 0) > 0]
        hive = [float(r.get(field, 0) or 0) for r in hive_all if int(r.get(field, 0) or 0) > 0]
        rubric_scored_count = len(off) + len(hive)
    else:
        off = [float(r.get(field, 0) or 0) for r in records_by_mode.get("off", [])]
        hive = [float(r.get(field, 0) or 0) for r in records_by_mode.get("hivemind", [])]
        rubric_scored_count = None
    off_mean = _mean(off)
    hive_mean = _mean(hive)
    result: dict[str, Any] = {
        "field": field,
        "off_mean": off_mean,
        "hivemind_mean": hive_mean,
        "abs_delta": hive_mean - off_mean,
        "pct_delta": _pct_delta(off_mean, hive_mean),
        "cohens_d": cohens_d(off, hive),
        "p_value": mann_whitney_u(off, hive),
    }
    if rubric_scored_count is not None:
        result["rubric_scored_count"] = rubric_scored_count
    return result


def build_gain_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_mode: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        by_mode.setdefault(str(rec.get("memory_mode", "off")), []).append(rec)
    return {
        "artifact": "memory_gain_report",
        "record_count": len(records),
        "groups": group_by_mode(records),
        "recall_mode_groups": group_by_recall_mode(records),
        "deltas": {
            "plan_tokens_out": _delta(by_mode, "plan_tokens_out"),
            "plan_wall_ms": _delta(by_mode, "plan_wall_ms"),
            "rubric_score": _delta(by_mode, "rubric_score"),
        },
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _sorted_modes(groups: dict[str, dict[str, Any]]) -> list[str]:
    known = [m for m in _MODE_ORDER if m in groups]
    extra = sorted(m for m in groups if m not in _MODE_ORDER)
    return known + extra


def _sorted_recall_modes(groups: dict[str, dict[str, Any]]) -> list[str]:
    known = [m for m in _RECALL_MODE_ORDER if m in groups]
    extra = sorted(m for m in groups if m not in _RECALL_MODE_ORDER)
    return known + extra


def render_markdown(report: dict[str, Any]) -> str:
    groups = report.get("groups", {})
    recall_mode_groups = report.get("recall_mode_groups", {})
    deltas = report.get("deltas", {})
    lines: list[str] = []
    lines.append("# Memory Gain Report")
    lines.append("")
    lines.append(f"Records analyzed: {report.get('record_count', 0)}")
    lines.append("")
    lines.append("## Per memory_mode")
    lines.append("")
    lines.append("| memory_mode | specs | mean tokens_out | median tokens_out | mean wall_ms | recall_hit_rate | mean decisions_reused | mean prior_art_tokens | mean rubric_score | median rubric_score |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for mode in _sorted_modes(groups):
        g = groups[mode]
        lines.append(
            f"| {mode} | {g['specs_completed']} | {_fmt(g['mean_plan_tokens_out'])} | "
            f"{_fmt(g['median_plan_tokens_out'])} | {_fmt(g['mean_plan_wall_ms'])} | "
            f"{_fmt(g['recall_hit_rate'])} | {_fmt(g['mean_decisions_reused'])} | "
            f"{_fmt(g.get('mean_prior_art_tokens', 0.0))} | "
            f"{_fmt(g.get('mean_rubric_score', 0.0))} | "
            f"{_fmt(g.get('median_rubric_score', 0.0))} |"
        )
    lines.append("")
    if recall_mode_groups:
        lines.append("## Per recall_mode")
        lines.append("")
        lines.append("| recall_mode | specs | mean tokens_out | mean wall_ms | recall_hit_rate | mean decisions_reused | mean prior_art_tokens | mean distilled | mean deduped | mean rubric_score | median rubric_score |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for rmode in _sorted_recall_modes(recall_mode_groups):
            g = recall_mode_groups[rmode]
            lines.append(
                f"| {rmode} | {g['specs_completed']} | {_fmt(g['mean_plan_tokens_out'])} | "
                f"{_fmt(g['mean_plan_wall_ms'])} | {_fmt(g['recall_hit_rate'])} | "
                f"{_fmt(g['mean_decisions_reused'])} | {_fmt(g.get('mean_prior_art_tokens', 0.0))} | "
                f"{_fmt(g.get('mean_decisions_distilled', 0.0))} | {_fmt(g.get('mean_decisions_deduped', 0.0))} | "
                f"{_fmt(g.get('mean_rubric_score', 0.0))} | {_fmt(g.get('median_rubric_score', 0.0))} |"
            )
        lines.append("")
    lines.append("## off vs hivemind deltas")
    lines.append("")
    lines.append("| metric | off mean | hivemind mean | abs delta | pct delta | Cohen's d | Mann-Whitney p |")
    lines.append("|---|---|---|---|---|---|---|")
    for key in ("plan_tokens_out", "plan_wall_ms", "rubric_score"):
        d = deltas.get(key, {})
        pct = d.get("pct_delta")
        pct_text = "null" if pct is None else f"{pct:.1f}%"
        lines.append(
            f"| {key} | {_fmt(d.get('off_mean'))} | {_fmt(d.get('hivemind_mean'))} | "
            f"{_fmt(d.get('abs_delta'))} | {pct_text} | {_fmt(d.get('cohens_d'))} | {_fmt(d.get('p_value'))} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"
