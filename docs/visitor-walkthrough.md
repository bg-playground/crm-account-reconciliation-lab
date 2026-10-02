# Five-minute visitor walkthrough

[Back to README](../README.md) · [Recorded examples](decision-examples.md) · [Published report](../results/index.md)

## 1. Understand the experiment

Two synthetic Salesforce orgs contain overlapping Account records. The lab asks whether a Splink baseline plus a typed AI judge can identify duplicates while keeping incorrect merges and human review within frozen limits.

Read the [headline results](../README.md#results-vs-the-frozen-bar). Recall improved from 68.8% to 96.6%; precision decreased from 99.6% to 98.8%. The experiment passed eight of nine criteria and failed calibration. **KILL means the frozen acceptance gate failed.** It does not mean the repository's verification failed.

## 2. Inspect three decisions

Open the [recorded examples](decision-examples.md) for a merge recommendation, a no-match decision, and a human-review case. These examples use committed synthetic records and recorded probabilities. They illustrate routing, not representative accuracy; aggregate metrics remain in the published report.

## 3. Verify the published evidence locally

Download or clone the repository using GitHub's Code menu. Open a terminal in the repository root. You need Python 3.11 or later and internet access to install dependencies. Verification itself is offline and needs no API key.

### Windows PowerShell

These commands use the virtual environment's Python directly, so script activation is unnecessary.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m recon_lab.cli verify
```

### macOS or Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m recon_lab.cli verify
```

Expected output (JSON key order may differ):

```json
{
  "calls_verified": 3308,
  "failed_criteria": ["6_calibration"],
  "provider_calls": 0,
  "published_verdict": "KILL",
  "run_id": "published-202-2fd9d168",
  "verification": "PASS"
}
```

Exit code 0 means the evidence reproduced consistently, including the KILL verdict. A nonzero exit code means evidence was missing, malformed, altered, or inconsistent. Replay files exist only in a temporary directory.

This checks evidence consistency; it does not authenticate the provider logs or establish performance on real CRM data. See the [verification limitations](../README.md#how-to-reproduce).

## 4. Try the interactive review workbench

Run `python -m recon_lab.cli workbench` with your virtual environment Python and open the address printed in the terminal. Review side-by-side records, explore recorded failures, and export your assessments. See the [workbench guide](review-workbench.md) for platform commands and session behavior.

## 5. Follow the evidence

| Artifact | What to inspect |
|---|---|
| [Published report](../results/index.md) | All nine criteria and baseline comparisons |
| [Calibration table](../results/calibration.md) | Two measured bins failed the frozen interval test; eight bins lacked enough observations |
| [Review queue](../results/review_queue.csv) | 21 pairs routed to human review, with both records and repeat probabilities |
| [Metrics JSON](../results/metrics.json) | Machine-readable measurements and verdict |
| [Frozen protocol](../config/frozen/protocol.json) | Precommitted cutoffs, acceptance rules, and input hashes |
| [Published run directory](../runs/published-202-2fd9d168/) | Raw calls, returned model versions, and decision ledgers |

## If setup fails

- If Python is missing or older than 3.11, install a supported version and recreate the virtual environment. On Windows, `py -3 --version` shows the selected version.
- If the module cannot be found, run the install command from the repository root and use the same virtual environment Python for verification.
- If verification reports an evidence mismatch, retain the error text and check for local changes to committed evidence. Do not run `generate` or `train-baseline` to repair the published result.

`generate` and `train-baseline` write inputs. `probe`, `dev`, and `publish` make provider calls and require an API key; `freeze` writes protocol state. None is needed for this walkthrough. The published run is one-shot.
