from __future__ import annotations

from .common import CheckResult, ValidationContext
from .dependencies import run as run_dependencies
from .design import run as run_design
from .evidence import run as run_evidence
from .handoff import run as run_handoff
from .intent import run as run_intent
from .anchors import run as run_anchors
from .legacy import (
    run_decisions,
    run_phase_log,
    run_spec_yaml,
    run_system_model,
    run_tasks_md,
)
from .requirements import run as run_requirements
from .packet_fit import run as run_packet_fit
from .prompt_budget import run as run_prompt_budget
from .review_log import run as run_review_log
from .runner_ready import run as run_runner_ready
from .setup_decisions import run as run_setup_decisions
from .sync_artifacts import (
    run_implementation_baseline,
    run_ssot_delta,
    run_sync_result,
    run_sync_readmission_report,
    run_sync_scope,
)
from .tasks import run as run_tasks
from .traceability import run as run_traceability
from .utility_report import run as run_utility_report


CHECKS = [
    ("spec.yaml", run_spec_yaml),
    ("system-model.yaml", run_system_model),
    ("tasks.md", run_tasks_md),
    ("phase-log.yaml", run_phase_log),
    ("decisions.yaml", run_decisions),
    ("dependencies", run_dependencies),
    ("traceability.yaml", run_traceability),
    ("tasks", run_tasks),
    ("review-log", run_review_log),
    ("handoff", run_handoff),
    ("intent", run_intent),
    ("requirements", run_requirements),
    ("design", run_design),
    ("evidence", run_evidence),
    ("setup-decisions", run_setup_decisions),
    ("ssot-delta", run_ssot_delta),
    ("implementation-baseline", run_implementation_baseline),
    ("sync-readmission-report", run_sync_readmission_report),
    ("sync-scope", run_sync_scope),
    ("sync-result", run_sync_result),
    ("utility-report", run_utility_report),
    ("prompt_budget", run_prompt_budget),
    ("packet_fit", run_packet_fit),
    ("runner_ready", run_runner_ready),
    ("anchors", run_anchors),
]


def list_checks() -> list[str]:
    return [name for name, _ in CHECKS]


def run_checks(context: ValidationContext) -> list[CheckResult]:
    return [runner(context) for _, runner in CHECKS]
