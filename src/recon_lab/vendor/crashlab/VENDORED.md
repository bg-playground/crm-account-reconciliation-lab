# Vendored: Agent-Crash-Lab measurement modules

Copied from bg-playground/Agent-Crash-Lab @ 8a3f865 (projects/agent-crash-lab-m1/; MIT; LICENSE reproduced verbatim). These six files were authored by B Gee in that fork.

Source commit: `8a3f865` (merge of PR #14, 2026-10-01). Copied 2026-10-02 (America/New_York).

| File | Upstream git blob at 8a3f865 | Changed here? |
|---|---|---|
| `typed_answers.py` | `9b3c0b29fbf0ed76902983983047162116554d2e` | yes (scrub 6, 7) |
| `calibration.py` | `09d9b9d125af906c821d15d0f349975f02d3c6a4` | yes (scrub 7) |
| `reliability_stats.py` | `3809d54082b2c980fbc2388bebf08575d738eb01` | yes (scrub 1) |
| `model_version_log.py` | `03489adff3cd2bf30ef7ff5d7b7e62fa1126eac5` | yes (scrub 5, 7) |
| `run_mode.py` | `cc6ee5fe5964e63ed1f1552c705456a557fdc2cb` | yes (scrub 7) |
| `run_log.py` | `a70ba263634556b26a771f60924db3bb4179ed02` | no |
| `LICENSE` | `abc369cf30f5` (prefix) | no (verbatim) |

Tests copied from the same commit into `tests/vendor_crashlab/` (`test_typed_answers.py`, `test_calibration.py`, `test_reliability_stats.py`, `test_model_version_log.py`, `test_run_mode.py`), with scrubs 2-4 and 7. `test_run_log.py` is new here (upstream had no dedicated test).

## Scrubs applied

1. `reliability_stats.py`: module docstring replaced with "Dependency-free Wilson score interval for binomial proportions." (the old one named upstream runner modules).
2. `test_reliability_stats.py`: removed the import of the upstream runner module and its re-export test.
3. `test_reliability_stats.py`: test renamed `test_known_interval_2_of_20` with the comment "2 of 20 -> 2.8%-30.1%".
4. `test_model_version_log.py`: deleted the test that exercised an optional third-party browser-agent SDK.
5. `model_version_log.py`: hook docstring trimmed to "any SDK that accepts an httpx.AsyncClient (e.g. the OpenAI SDK)".
6. `typed_answers.py`: docstring example id `site-3` -> `acct-3`.
7. Flat imports (`from run_log import ...` etc.) changed to package-relative imports in the modules, and to `from recon_lab.vendor.crashlab.<module> import ...` in the tests.

Kept unchanged on purpose: `DEFAULT_MIN_COUNT = 10` in `calibration.py` (this repo passes `min_count=30` explicitly) and the toy thresholds in `test_run_mode.py`.
