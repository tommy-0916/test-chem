# Test failure fingerprint: `ca07cdc` → `f37a431` checkout (2026-09-27)

Full per-test records, including test ID, exception type, failure reason and repository stack origin, are in [the machine-readable fingerprint](test_failure_fingerprint_20260927.json).

| Suite | `ca07cdc` | `f37a431` | Failure identity and cause |
| --- | --- | --- | --- |
| Research | 584 ran; 8 failures, 21 errors | 597 ran; 8 failures, 21 errors | Same 29 test IDs, exception types, normalized reasons and repository functions. |
| Device | 957 collected; 50 failed, 905 passed, 2 skipped | 985 collected; 50 failed, 935 passed | Same 50 test IDs, exception types, messages and repository functions. |

**Conclusion:** No new failure or changed causal failure signature was found in either full suite. This is a baseline comparison, not a claim that either full suite passes.

## Exact comparison method

Both runs used the same `.venv` Python interpreter and commands from their own checkout directories. The baseline was extracted with `git archive ca07cdc` into a separate temporary directory. The current checkout was at `f37a431`; it also contained the new, untracked offline replay test file from this turn, so its collected count is **not** a pure `f37a431` commit count. The additional tests all passed and did not change the failure set.

```text
python -m unittest discover -s reaserch_agent -p test*.py -q
python -m pytest device_agent -q --tb=short --junitxml=<out>.xml
```

The comparison keys are test ID, outcome, exception type, reason after replacing randomized temporary directory names, and repository stack path plus function. Source line numbers remain in the JSON for inspection but do not define a causal change when added code merely moves the same function.

## Differences investigated

- Research: eight failures show only `workflow.py` line shifts. One old assertion, `test_macro_step_prompt_projection_keeps_contract_without_audit_noise`, reads `18027 < 17637.2` in the archive versus `18027 < 17633.7` in the current checkout. A focused rerun reproduced this. The full device context differs only in its absolute `skill_source_path`: the archive path serializes to 140 characters versus 135 in the current checkout. The compact context is 18027 in both. The test fails for the same reason in both revisions.
- Device: three `test_plan_repair` failures point to the same `single_agent.py` `<lambda>` function; its line moved from 19298 to 19371. Their exception types and messages match exactly.
- The two baseline skips in `FrozenB01LidChainTest` become passes in the current checkout because its local B01 diagnostic package exists there but is not in the clean Git archive. These are fixture availability differences. Additional Device tests were collected from `f37a431`; Research also collected the untracked offline replay tests from this turn.

Research's 29 failures include 13 Windows `PermissionError [WinError 32]` SQLite cleanup errors, eight assertion failures, four `RuntimeError`s and four `ValueError`s. Device's 50 failures have their individual messages and origins in the JSON.

The Windows PowerShell redirection replaced some Chinese characters in native test output. The comparison retained matching English reason codes, exception types, test IDs and stack functions; the temporary directory also contains the full console logs and JUnit XML from both runs.
