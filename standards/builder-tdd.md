# Builder TDD Discipline

Reference document loaded by `/isanna-5-implement`, `/isanna-6-verify`, and `/isanna-debug`.
Not a prompt — a set of rules that implementation agents MUST follow.

These rules are hard stops, not suggestions. If a task is marked `TDD: required`,
there is no permission to skip, soften, defer, or reinterpret them.

---

## The Iron Law

> **No production code exists without a failing test that demands it.**

If you find yourself writing implementation code without a failing test,
stop immediately and write the test first. This is not optional.

---

## The Delete Rule

> If you wrote implementation before the test, **delete the implementation**
> and start over with the failing test.

Do not try to "backfill" tests after writing code. The test must fail first
to prove it actually tests the right behavior. A test written after implementation
that passes immediately proves nothing.

Delete only code introduced in the current task cycle.
If the current state mixes pre-existing work, human edits, or unrelated changes,
HALT and ask instead of guessing what to remove.

---

## Task Contract

Builder task planning and execution use first-class TDD metadata:

- Every task MUST declare `**TDD:** required` or `**TDD:** exempt (<reason>)`.
- `TDD: required` is mandatory for any new behavior, changed behavior, or bug fix.
- `TDD: exempt` is allowed only for these exact reasons:
  `refactor-only`, `delete-only`, `type-only`, `config-only`, `infrastructure-only`.
- A `TDD: required` task MUST include at least one test file in `Files:`.
- A `TDD: required` task MUST start its `Steps:` with the RED step.
- `Verify:` is for final GREEN verification only.
- A `TDD: required` task's `Verify:` MUST include a focused test command proving
  the changed behavior passes.
- A `TDD: required` task's `Verify:` MUST include the project verification command,
  OR when that command exceeds the host-verify budget, the narrowest command
  covering the changed surface, with the full suite deferred to the spec's final
  verification task.
- RED evidence belongs in `Steps:` execution and `phase-log.yaml` evidence.
- RED evidence MUST come from actually running the focused verify command
  host-side (captured command + exit code + timestamp), not typed into YAML.
- Verify success is judged by exit code alone: a focused verify MUST exit 0 only
  when the behavior exists. Zero-hit assertions MUST be negated (`! grep …`).

> **See also: `builder-contract.md` — Evidence Schema** for the normative
> field definitions (`task_id`, `step`, `command`, `exit_code`, `output_summary`)
> and per-mode shapes for `tdd.mode: required` and `exempt`.

If a task violates this contract, the task is invalid. Do not implement it.
Send it back to planning.

---

## Red-Green-Refactor

Every code change follows this exact cycle:

### 1. RED — Write a Failing Test

```text
Test("rejects duplicate idempotency key")
  key = random_id()
  process_request(data={item: "A"}, idempotency_key=key)
  result = process_request(data={item: "A"}, idempotency_key=key)
  expect(result.status).to_equal("duplicate")
```

**Run the test.** It MUST fail. A first-run pass has two possible causes, and you
do not get to assume which one it is:

1. **The behavior is already implemented.** The deliverable exists — built by an
   earlier spec, added by hand, or shipped before this spec was written. A spec's
   declared status does not know this: `planned` means nobody updated the file, not
   that the code is absent.
2. **The test is wrong.** It asserts nothing, exercises the wrong path, or passes
   against an empty implementation.

**Tell them apart by reading the target code, not the test.** Open the file the
task names and look for the behavior. If it is there, **STOP the task** and report
an **already-shipped** outcome with what you found: the file, the symbol, and the
command that already passes. Do not edit the test until it goes red, and do not
reimplement what is already there — that is how shipped functionality gets rebuilt
from a stale spec.

Only once you have confirmed the code does NOT have the behavior is the test the
thing that is wrong. Fix the test, then proceed.

> The whole-spec form of this check is `isanna verify --spec <feature>`: it runs
> that spec's own acceptance commands from `tasks.yaml`. All green on a spec that
> has not been implemented is the same signal at spec scale.

**Verify the failure reason.** The test should fail _for the expected reason_
(e.g., "function not found" or "assertion failed: expected 'duplicate', got 'success'"),
not for an unrelated error (import failure, syntax error).

### 2. GREEN — Write Minimal Implementation

Write the **smallest amount of code** that makes the test pass. Nothing more.

- Don't generalize.
- Don't handle edge cases you don't have tests for yet.
- Don't refactor yet.

**Run the test.** It MUST pass. If it doesn't, fix the implementation — don't
touch the test unless the test itself is wrong.

### 3. REFACTOR — Clean Up with Safety Net

Now that tests pass, you may refactor:

- Extract helpers
- Rename for clarity
- Remove duplication

**Run all tests after refactoring.** Everything MUST still pass.
If anything fails, your refactor changed behavior — revert it.

---

## When TDD Applies

| Task type                   | TDD required? | Notes                                       |
| --------------------------- | ------------- | ------------------------------------------- |
| New behavior / feature      | **Yes**       | Always RED → GREEN → REFACTOR               |
| Bug fix                     | **Yes**       | Failing test reproduces the bug first        |
| Refactor (no behavior change) | No          | Use `TDD: exempt (refactor-only)`            |
| Type-only changes           | No            | Use `TDD: exempt (type-only)`                |
| Config changes              | No            | Use `TDD: exempt (config-only)`              |
| Infrastructure changes      | No            | Use `TDD: exempt (infrastructure-only)`      |
| Delete-only tasks           | No            | Use `TDD: exempt (delete-only)`              |

If you are unsure whether a task is behavior-changing, treat it as behavior-changing.
The bias is toward `TDD: required`, not exemption.

---

## Good Examples (Language-Agnostic Pseudocode)

### Testing a Zod schema

```text
Test("PayloadSchema rejects missing event_type")
  result = PayloadSchema.parse_safe({ data: {} })
  expect(result.success).to_equal(false)

Test("PayloadSchema accepts valid payload")
  result = PayloadSchema.parse_safe({ event_type: "order.confirmed", data: { quantity: 3 } })
  expect(result.success).to_equal(true)
```

### Testing async behavior

```text
Test("retry gives up after 3 attempts")
  attempts = 0
  always_fails = () =>
    attempts += 1
    raise "boom"
  expect_async_error(() => retry(always_fails, max_attempts=3))
  expect(attempts).to_equal(3)
```

### Testing with setup/teardown

```text
Test("create_conversation persists to database")
  db = create_test_db()
  try
    Step("inserts a new row")
      conv = create_conversation(db, { user_id: "u1" })
      expect_exists(conv.id)

    Step("rejects duplicate user+channel combo")
      expect_async_error(() => create_conversation(db, { user_id: "u1" }), contains="duplicate")
  finally
    db.close()
```

---

## Bad Examples — Anti-Patterns

### Writing implementation first, test second

```text
# ❌ BAD: Wrote process_request() first, then wrote this test.
# The test passed immediately — how do you know it tests the right thing?
Test("process_request works")
  result = process_request(data={item: "A"})
  expect_exists(result)  # Too vague — proves nothing
```

### Testing implementation details instead of behavior

```text
# ❌ BAD: Testing that an internal method was called, not the outcome.
Test("process_order calls validate_inventory")
  spy = stub(inventory, "validate")
  process_order({ item: "widget" })
  expect_call_count(spy, 1)  # Couples test to implementation
  spy.restore()
```

### Mocking the thing you're testing

```text
# ❌ BAD: You're testing the mock, not the real code.
Test("calculate_total returns correct total")
  mock_calc = () => 42  # You're not testing calculate_total at all
  expect(mock_calc()).to_equal(42)
```

---

## Anti-Rationalization Table

| Excuse                                            | Response                                                          |
| ------------------------------------------------- | ----------------------------------------------------------------- |
| "It's a simple change, doesn't need a test"       | Simple changes break things too. The test takes 30 seconds.       |
| "I'll add the test after I see the code works"    | Then delete the code and write the test first. That's the rule.   |
| "This is just a refactor"                         | Then existing tests should pass. Run them before AND after.       |
| "The test is obvious, I know it will fail"        | Prove it. Run the test. You'll be surprised how often it passes.  |
| "I can't write a test for this"                   | Then you don't understand the behavior well enough to implement.  |
| "Writing tests first is slower"                   | Debugging untested code is slower. TDD catches bugs at write time.|
| "The acceptance criteria are the tests"            | Criteria describe what. Tests prove it. Both are required.        |
| "It's infrastructure / config / glue code"        | Then it's in the exemption list above. But verify by shell cmd.   |

---

## Hard Stops

Stop immediately and do not proceed if any of these happen:

- A `TDD: required` task has no explicit `TDD:` field.
- A `TDD: required` task has no test file in `Files:`.
- A `TDD: required` task starts with implementation instead of a failing test.
- The new test passes on its first run before implementation exists — stop and
  establish whether the behavior is already implemented before touching the test.
- The failing test fails for the wrong reason and you ignore it.
- You cannot express the required behavior as a test or reproduction.
- A `TDD: required` task has no focused GREEN test command in `Verify:`.

When a hard stop is triggered:

1. Stop implementation.
2. Fix the task definition or the test.
3. If the task itself is underspecified, return to `/isanna-4-plan` or `/isanna-2-design`.

---

## Red Flags

Stop and re-evaluate if you notice:

- ⚠️ You wrote more than 10 lines of implementation without a failing test
- ⚠️ A new test passes on the first run (is the behavior already shipped, or did
  you test the wrong thing? Read the target code to find out)
- ⚠️ You're mocking more than 1 dependency in a single test
- ⚠️ You're testing private functions instead of public behavior
- ⚠️ Your test description says "works" or "correctly" (too vague)
- ⚠️ The test can't fail — it would pass with an empty implementation
- ⚠️ You're refactoring inside the GREEN step (wait for REFACTOR)

---

## Verification Checklist

After each Red-Green-Refactor cycle, confirm:

- [ ] Test was written BEFORE the implementation code
- [ ] Test FAILED on first run for the expected reason
- [ ] Implementation is the MINIMUM code to pass the test
- [ ] No untested behavior was added
- [ ] All existing tests still pass
- [ ] Refactoring (if done) changed no behavior — same test results
- [ ] `phase-log.yaml` contains RED evidence, GREEN evidence, and final verify evidence

This checklist is enforced by `/isanna-5-implement`'s TDD verification gate.

---

## Appendix: Deno/TypeScript Examples

If your project uses Deno + TypeScript, translate the pseudocode examples above
to your local `Deno test` conventions and assertion helpers.
