# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Pass/fail verdict for `eval grade` against per-metric thresholds."""

import logging
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import click

from google.agents.cli._agent_platform_types import types
from google.agents.cli._output import Console

FAIL_UNDER_FORM = "METRIC=SCORE, e.g. 'final_response_quality=0.8'"


@dataclass(frozen=True)
class CaseScore:
    case_index: int
    score: float


@dataclass(frozen=True)
class MetricVerdict:
    name: str
    mean_score: float | None
    threshold: float | None
    num_cases_error: int
    errored_cases: tuple[int, ...]
    below_threshold: tuple[CaseScore, ...]
    # Share of graded cases at or above the threshold; None without a
    # threshold or per-case scores.
    pass_rate: float | None

    @property
    def below_mean(self) -> bool:
        if self.threshold is None:
            return False
        return self.mean_score is None or self.mean_score < self.threshold

    @property
    def passed(self) -> bool:
        return not self.below_mean and not self.num_cases_error


@dataclass(frozen=True)
class Verdict:
    metrics: tuple[MetricVerdict, ...]

    @property
    def passed(self) -> bool:
        return bool(self.metrics) and all(m.passed for m in self.metrics)


def parse_fail_under(values: Sequence[str]) -> dict[str, float]:
    """Parse repeated `--fail-under METRIC=SCORE` values into per-metric thresholds.

    A bare SCORE is rejected: metrics score on different scales, so one number
    can't be a meaningful bar for all of them.
    """
    thresholds: dict[str, float] = {}
    for value in values:
        name, _, number = value.rpartition("=")
        name = name.strip()
        if not name:
            raise click.BadParameter(
                f"{value!r} is missing a metric name. Metrics score on different "
                "scales (0-1 for built-ins, 1-5 for the scaffolded judge), so "
                f"name the metric each threshold is for: {FAIL_UNDER_FORM}.",
                param_hint="'--fail-under'",
            )
        try:
            threshold = float(number)
        except ValueError:
            threshold = math.nan
        # float() also accepts 'nan' and 'inf'; a NaN threshold could never fail.
        if not math.isfinite(threshold):
            raise click.BadParameter(
                f"{value!r} is not a finite number. Use {FAIL_UNDER_FORM}.",
                param_hint="'--fail-under'",
            )
        thresholds[name] = threshold
    return thresholds


def judge(
    result: types.EvaluationResult,
    config_thresholds: Mapping[str, float],
    fail_under: Mapping[str, float],
) -> Verdict:
    """Compare each metric's mean score with its threshold and collect failing cases.

    A `--fail-under` threshold overrides the config's for the same metric. See
    `_matching_name` for how a threshold's name is matched to a metric.
    """
    summaries = {
        m.metric_name: m for m in result.summary_metrics or [] if m and m.metric_name
    }
    per_case = _case_scores(result)
    used_config: set[str] = set()
    used_flags: set[str] = set()
    metrics = []
    for name, summary in summaries.items():
        configured = flagged = None
        if (config_name := _matching_name(name, config_thresholds)) is not None:
            used_config.add(config_name)
            configured = config_thresholds[config_name]
        if (flag_name := _matching_name(name, fail_under)) is not None:
            used_flags.add(flag_name)
            flagged = fail_under[flag_name]
        threshold = _threshold_for(name, configured, flagged)
        metrics.append(_judge_metric(name, summary, threshold, per_case.get(name, {})))

    _warn_on_unused_thresholds("--fail-under", fail_under, used_flags)
    _warn_on_unused_thresholds("eval config 'thresholds'", config_thresholds, used_config)
    return Verdict(metrics=tuple(metrics))


def _matching_name(metric_name: str, names: Iterable[str]) -> str | None:
    """The threshold name that applies to a metric, or None.

    An exact match ignoring case wins. Only without one does a name match the
    metric's name minus the `_vN` suffix the eval service adds to built-ins, so
    `final_response_quality` covers `final_response_quality_v1` while custom
    `quality_v1` and `quality_v2` keep their own thresholds. Among equal
    matches the last wins, so a repeated flag overrides an earlier one.
    """
    lowered = metric_name.lower()
    candidates = list(reversed(list(names)))
    for wanted in (lowered, re.sub(r"_v\d+$", "", lowered)):
        for name in candidates:
            if name.lower() == wanted:
                return name
    return None


def _warn_on_unused_thresholds(source: str, names: Iterable[str], used: set[str]) -> None:
    unused = sorted(n for n in names if n not in used)
    if unused:
        logging.warning(
            "Ignoring %s for metric(s) that weren't graded: %s.",
            source,
            ", ".join(unused),
        )


def _case_scores(
    result: types.EvaluationResult,
) -> dict[str, dict[int, float | None]]:
    """Map metric name to each case's score, None where the case errored.

    A case errored if any of its response candidates did, so a later
    candidate's score never hides an earlier error.
    """
    scores: dict[str, dict[int, float | None]] = {}
    for position, case in enumerate(result.eval_case_results or []):
        index = case.eval_case_index if case.eval_case_index is not None else position
        for candidate in case.response_candidate_results or []:
            for name, metric_result in (candidate.metric_results or {}).items():
                case_scores = scores.setdefault(name, {})
                score = metric_result.score
                if metric_result.error_message is not None or score is None:
                    case_scores[index] = None
                elif index not in case_scores or case_scores[index] is not None:
                    case_scores[index] = float(score)
    return scores


def _threshold_for(
    metric_name: str,
    configured: float | None,
    flagged: float | None,
) -> float | None:
    if flagged is None:
        return configured
    if configured is not None and configured != flagged:
        logging.info(
            "--fail-under %g overrides the eval config threshold %g for %s.",
            flagged,
            configured,
            metric_name,
        )
    return flagged


def _judge_metric(
    name: str,
    summary: types.AggregatedMetricResult,
    threshold: float | None,
    case_scores: Mapping[int, float | None],
) -> MetricVerdict:
    graded = {i: s for i, s in case_scores.items() if s is not None}
    below = ()
    pass_rate = None
    if threshold is not None:
        below = tuple(CaseScore(i, s) for i, s in sorted(graded.items()) if s < threshold)
        if graded:
            pass_rate = (len(graded) - len(below)) / len(graded)
    return MetricVerdict(
        name=name,
        mean_score=summary.mean_score,
        threshold=threshold,
        num_cases_error=summary.num_cases_error or 0,
        errored_cases=tuple(sorted(i for i, s in case_scores.items() if s is None)),
        below_threshold=below,
        pass_rate=pass_rate,
    )


def fill_pass_rates(result: types.EvaluationResult, verdict: Verdict) -> bool:
    """Set `pass_rate` from the thresholds on summaries the SDK left without one.

    The SDK computes it only for service-side metrics, as the share of cases
    scoring exactly 1.0, so local custom metrics otherwise never get one.

    Returns:
        Whether any summary was changed.
    """
    by_name = {m.name: m for m in verdict.metrics}
    changed = False
    for summary in result.summary_metrics or []:
        if not summary or summary.pass_rate is not None:
            continue
        metric = by_name.get(summary.metric_name or "")
        if metric and metric.pass_rate is not None:
            summary.pass_rate = metric.pass_rate
            changed = True
    return changed


def print_verdict(verdict: Verdict, report_path: str, console: Console) -> None:
    """Print what failed, one next step, and a final `RESULT:` line."""
    failures = [m for m in verdict.metrics if not m.passed]
    if failures:
        console.print("\nFailures:", markup=False)
        for metric in failures:
            console.print(f"  {_failure_detail(metric)}", markup=False)
        console.print(
            f"Next: open {report_path} to read the failing cases, fix the agent, "
            "rerun `agents-cli eval run`, then `agents-cli eval compare "
            "<previous>.json <new>.json`.",
            markup=False,
        )
    console.print(f"RESULT: {_result_line(verdict)}", markup=False)


def _failure_detail(metric: MetricVerdict) -> str:
    parts = []
    if metric.below_mean:
        cases = ", ".join(
            f"case {c.case_index} ({c.score:.2f})" for c in metric.below_threshold
        )
        parts.append(
            f"below threshold {metric.threshold:g}" + (f": {cases}" if cases else "")
        )
    if metric.num_cases_error:
        errored = _plural(metric.num_cases_error, "case") + " errored"
        if metric.errored_cases:
            errored += " (" + ", ".join(f"case {i}" for i in metric.errored_cases) + ")"
        parts.append(errored)
    return f"{metric.name}: " + "; ".join(parts)


def _result_line(verdict: Verdict) -> str:
    if not verdict.metrics:
        return "FAIL (no metric results returned)"
    if not verdict.passed:
        reasons = []
        for metric in verdict.metrics:
            if metric.below_mean:
                reasons.append(_score_vs_threshold(metric, "<"))
            if metric.num_cases_error:
                reasons.append(
                    f"{metric.name}: {_plural(metric.num_cases_error, 'case')} errored"
                )
        return f"FAIL ({'; '.join(reasons)})"
    gated = [m for m in verdict.metrics if m.threshold is not None]
    if not gated:
        return (
            "PASS (no thresholds set; add 'thresholds' to the eval config or pass "
            "--fail-under to gate on scores)"
        )
    return f"PASS ({'; '.join(_score_vs_threshold(m, '>=') for m in gated)})"


def _score_vs_threshold(metric: MetricVerdict, op: str) -> str:
    if metric.mean_score is None:
        return f"{metric.name} has no score"
    return f"{metric.name} {metric.mean_score:.2f} {op} {metric.threshold:.2f}"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"
