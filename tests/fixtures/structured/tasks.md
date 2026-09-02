# Structured Artifact Modernization — Tasks

Each task is self-contained: repo, files, steps, shell verification, and a binary done signal.
Dependencies are explicit. Tasks with no `Depends on` can start immediately.

---

- [ ] 1. Lock the artifact contract
  - **Repo:** `builder/`
  - **Files:** `standards/builder-contract.md`, `standards/builder-workflow.md`, `README.md`
  - **TDD:** `exempt (config-only)`
  - **Steps:**
    1. Add the canonical artifact matrix to the contract.
    2. Add the dual-write and drift rules to workflow and standards.
  - **Verify:**
    ```sh
    grep -n "tasks.yaml" /path/to/project/standards/builder-contract.md
    grep -n "dual-write" /path/to/project/standards/builder-workflow.md
    ```
  - **Done when:** Contract and workflow document the canonical artifacts.
  - **Depends on:** none
  - **Parallel with:** none

- [ ] 2. Build the schema and renderer foundation
  - **Repo:** `builder/`
  - **Files:** `schemas/tasks.schema.yaml`, `scripts/render-spec-artifacts.py`, `scripts/validate-spec.py`, `tests/test_structured_artifacts.sh`
  - **TDD:** `required`
  - **Steps:**
    1. Write a failing renderer test for the golden tasks fixture.
    2. Implement the renderer and validator dispatcher.
  - **Verify:**
    ```sh
    bash /path/to/project/tests/test_structured_artifacts.sh
    python3 /path/to/project/scripts/validate-spec.py --list-checks
    ```
  - **Done when:** Renderer and validator foundation are in place.
  - **Depends on:** 1
  - **Parallel with:** none
