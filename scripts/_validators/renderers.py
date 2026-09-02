from __future__ import annotations

from typing import Any, Callable

from .common import string_list


def render_artifact(artifact: str, data: dict[str, Any]) -> str:
    renderers: dict[str, Callable[[dict[str, Any]], str]] = {
        "tasks": render_tasks,
        "review-log": render_review_log,
        "constitution-review": render_constitution_review,
        "handoff": render_handoff,
        "requirements": render_requirements,
        "design": render_design,
    }
    if artifact not in renderers:
        raise ValueError(f"unsupported artifact: {artifact}")
    return renderers[artifact](data)


def render_tasks(data: dict[str, Any]) -> str:
    title = str(data.get("title", "Tasks")).strip()
    tasks = data.get("tasks") if isinstance(data.get("tasks"), list) else []
    id_to_number = {
        str(task.get("id", f"T{index}")): index
        for index, task in enumerate(tasks, start=1)
        if isinstance(task, dict)
    }

    lines = [
        f"# {title} — Tasks",
        "",
        "Each task is self-contained: repo, files, steps, shell verification, and a binary done signal.",
        "Dependencies are explicit. Tasks with no `Depends on` can start immediately.",
        "",
        "---",
        "",
    ]

    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            continue
        repo = str(task.get("repo", "")).strip()
        files = ", ".join(f"`{item}`" for item in string_list(task.get("files")))
        tdd = task.get("tdd") if isinstance(task.get("tdd"), dict) else {}
        tdd_mode = str(tdd.get("mode", "required")).strip()
        tdd_value = "required"
        if tdd_mode == "exempt":
            reason = str(tdd.get("reason", "")).strip() or "config-only"
            tdd_value = f"exempt ({reason})"

        depends_on = ", ".join(str(id_to_number.get(ref, ref)) for ref in string_list(task.get("depends_on"))) or "none"
        parallel_with = ", ".join(str(id_to_number.get(ref, ref)) for ref in string_list(task.get("parallel_with"))) or "none"

        lines.extend(
            [
                f"- [ ] {index}. {str(task.get('title', '')).strip()}",
                f"  - **Repo:** `{repo}`",
                f"  - **Files:** {files}",
                f"  - **TDD:** `{tdd_value}`",
                "  - **Steps:**",
            ]
        )
        for step_index, step in enumerate(task.get("steps") or [], start=1):
            step_text = ""
            if isinstance(step, dict):
                step_text = str(step.get("text", "")).strip()
            elif isinstance(step, str):
                step_text = step.strip()
            lines.append(f"    {step_index}. {step_text}")

        lines.extend(["  - **Verify:**", "    ```sh"])
        for verify in task.get("verify") or []:
            command = ""
            if isinstance(verify, dict):
                command = str(verify.get("command", "")).strip()
            elif isinstance(verify, str):
                command = verify.strip()
            lines.append(f"    {command}")
        lines.extend(
            [
                "    ```",
                f"  - **Done when:** {str(task.get('done_when', '')).strip()}",
                f"  - **Depends on:** {depends_on}",
                f"  - **Parallel with:** {parallel_with}",
            ]
        )
        human_gate = str(task.get("human_gate", "")).strip()
        if human_gate:
            lines.append(f"  - **HUMAN GATE:** {human_gate}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_review_log(data: dict[str, Any]) -> str:
    title = str(data.get("title", "Review Log")).strip()
    lines = [f"# {title} — Review Log", ""]

    findings = data.get("findings") if isinstance(data.get("findings"), list) else []
    if findings:
        lines.extend(["## Findings", ""])
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            lines.append(f"### {finding.get('id', '')} — {finding.get('title', '')}")
            lines.append("")
            lines.append(f"- Class: `{finding.get('class', '')}`")
            lines.append(f"- Summary: {finding.get('summary', '')}")
            lines.append("")

    amendments = data.get("amendments") if isinstance(data.get("amendments"), list) else []
    if amendments:
        lines.extend(["## Amendments", ""])
        for amendment in amendments:
            if not isinstance(amendment, dict):
                continue
            lines.append(f"- {amendment.get('id', '')}: {amendment.get('summary', '')}")
        lines.append("")

    verdict = data.get("verdict") if isinstance(data.get("verdict"), dict) else {}
    lines.extend(["## Verdict", "", f"- Status: `{verdict.get('status', '')}`", f"- Summary: {verdict.get('summary', '')}"])
    return "\n".join(lines).rstrip() + "\n"


def render_constitution_review(data: dict[str, Any]) -> str:
    verdict = str(data.get("verdict", "")).strip()
    lines = [
        f"# {data.get('spec', 'Spec')} — Constitution Review",
        "",
        f"- Verdict: `{verdict}`",
        f"- Summary: {data.get('summary', '')}",
        f"- Model assisted: `{str(data.get('model_assisted', False)).lower()}`",
        "",
        "## Checked Constitutions",
        "",
    ]
    checked = string_list(data.get("checked_constitutions"))
    if checked:
        for item in checked:
            lines.append(f"- `{item}`")
    else:
        lines.append("- none")

    lines.extend(["", "## Principle Results", ""])
    results = data.get("principle_results") if isinstance(data.get("principle_results"), list) else []
    if results:
        for result in results:
            if not isinstance(result, dict):
                continue
            lines.append(f"### {result.get('principle_id', '')}")
            lines.append("")
            lines.append(f"- Status: `{result.get('status', '')}`")
            lines.append(f"- Severity: `{result.get('severity', '')}`")
            lines.append(f"- Summary: {result.get('summary', '')}")
            evidence = string_list(result.get("evidence"))
            if evidence:
                lines.append("- Evidence:")
                for item in evidence:
                    lines.append(f"  - {item}")
            remediation = str(result.get("remediation", "")).strip()
            if remediation:
                lines.append(f"- Remediation: {remediation}")
            lines.append("")
    else:
        lines.append("- No principle results.")
        lines.append("")

    decisions = string_list(data.get("required_decisions"))
    if decisions:
        lines.extend(["## Required Decisions", ""])
        for item in decisions:
            lines.append(f"- {item}")
        lines.append("")

    follow_ups = string_list(data.get("follow_up_actions"))
    if follow_ups:
        lines.extend(["## Follow-Up Actions", ""])
        for item in follow_ups:
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_handoff(data: dict[str, Any]) -> str:
    files_written = ", ".join(f"`{item}`" for item in string_list(data.get("files_written")))
    lines = [
        "```text",
        "BUILDER HANDOFF",
        f"Phase: {data.get('phase', '')}",
        f"Summary: {data.get('summary', '')}",
        f"Files written: {files_written}",
        f"Used model: {data.get('used_model', '')}",
        f"Model advice: {data.get('model_advice', '')}",
        f"Next phase: {data.get('next_phase', '')}",
        f"Next command: {data.get('next_command', '')}",
        "```",
    ]
    return "\n".join(lines) + "\n"


def render_requirements(data: dict[str, Any]) -> str:
    title = str(data.get("title", "Requirements")).strip()
    lines = [f"# {title} — Requirements", ""]
    requirements = data.get("requirements") if isinstance(data.get("requirements"), list) else []
    for index, requirement in enumerate(requirements, start=1):
        if not isinstance(requirement, dict):
            continue
        requirement_id = str(requirement.get("id", f"R{index}")).strip()
        heading_number = requirement_id[1:] if requirement_id.startswith("R") and requirement_id[1:].isdigit() else str(index)
        lines.extend(
            [
                f"### Requirement {heading_number} — {requirement.get('title', '')}",
                "",
                f"**User story:** {requirement.get('user_story', '')}",
                "",
                "**EARS acceptance criteria:**",
                "",
            ]
        )
        for criterion in requirement.get("acceptance") or []:
            lines.append(f"- {criterion}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_design(data: dict[str, Any]) -> str:
    title = str(data.get("title", "Design")).strip()
    lines = [f"# {title} — Design", "", "## Responsibility Allocation", "", "| Surface | Keep | Change | Why |", "| --- | --- | --- | --- |"]
    for row in data.get("responsibility_allocation") or []:
        if not isinstance(row, dict):
            continue
        lines.append(f"| {row.get('surface', '')} | {row.get('keep', '')} | {row.get('change', '')} | {row.get('why', '')} |")

    lines.extend(["", "## Core Changes", ""])
    for change in data.get("core_changes") or []:
        if isinstance(change, dict):
            lines.append(f"### {change.get('title', '')}")
            lines.append("")
            lines.append(str(change.get("summary", "")).strip())
            lines.append("")
        elif isinstance(change, str):
            lines.append(f"- {change}")
    if lines[-1] != "":
        lines.append("")

    lines.extend(["## Telemetry Strategy", ""])
    for item in data.get("telemetry_strategy") or []:
        lines.append(f"- {item}")
    lines.extend(["", "## Verification Strategy", "", "```sh"])
    for item in data.get("verification_strategy") or []:
        if isinstance(item, dict):
            lines.append(str(item.get("command", "")).strip())
        else:
            lines.append(str(item).strip())
    lines.extend(["```", ""])
    return "\n".join(lines).rstrip() + "\n"
