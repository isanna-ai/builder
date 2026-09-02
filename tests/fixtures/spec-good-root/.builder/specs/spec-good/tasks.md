# Good Fixture — Tasks

Each task is self-contained: repo, files, steps, shell verification, and a binary done signal.
Dependencies are explicit. Tasks with no `Depends on` can start immediately.

---

- [ ] 1. Validate the good fixture
  - **Repo:** `builder/`
  - **Files:** `tests/fixtures/spec-good/tasks.yaml`, `tests/test_validator.sh`
  - **TDD:** `required`
  - **Steps:**
    1. Write a failing validator regression for the canonical task fixture.
    2. Run the validator regression suite.
  - **Verify:**
    ```sh
    bash /path/to/project/tests/test_structured_artifacts.sh
    bash /path/to/project/tests/test_validator.sh
    ```
  - **Done when:** The canonical task fixture passes validator checks.
  - **Depends on:** none
  - **Parallel with:** none
