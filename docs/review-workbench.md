# Synthetic review workbench

[Back to README](../README.md) · [Visitor walkthrough](visitor-walkthrough.md)

The local workbench lets you compare committed synthetic Accounts, explore failures, and capture review decisions. Startup verifies the frozen evidence before serving it. It makes no provider calls and does not require an API key.

## Start

Use the installation steps in the [visitor walkthrough](visitor-walkthrough.md), then run one of:

```powershell
.\.venv\Scripts\python.exe -m recon_lab.cli workbench
```

```bash
.venv/bin/python -m recon_lab.cli workbench
```

Open the local address printed in the terminal. The server binds only to `127.0.0.1`; the default port is 8765. Preparing the evidence can take several seconds. Leave the terminal running; stop with Ctrl+C. If the port is occupied, use `workbench --port 8766`.

## Review a pair

1. Start with the **Human review queue** and select a pair.
2. Compare the fields and inspect the recorded Splink score, AI repeats, and published routing.
3. Choose **Same entity**, **Different entities**, or **Defer**; add a note and save the assessment.
4. Use **Your review → Not assessed** to continue through the remaining pairs.
5. Select **Export review session** to download a JSON copy.

Unsaved edits must be saved or discarded before changing pairs, filters, or exporting. Assessments are saved in this browser under an evidence-specific key. On the same browser and local address, refresh or restart restores saved assessments. If browser storage is unavailable, the workbench reports that the session is in memory only; export it before closing. Each save appends an event, preserving earlier assessments in the export.

## Explore failures

| View | Meaning in the published synthetic run |
|---|---|
| Human review queue | 21 pairs left for review |
| Incorrect merge recommendations | Four recommended merges that contradict ground truth |
| Missed automatic matches | 12 true pairs not automatically matched, including ten review cases, one AI no-match, and one blocking miss |
| Repeat disagreement | Main-arm repeats fall in different frozen routing bands; it is not merely a difference in numeric probabilities |
| All evidence pairs | 1,648 blocked candidates plus the true pair missed by blocking |

The AI calls shown on Splink merge pairs were calibration audits and do not override Splink routing. A pair missed by blocking has no score or judge call. No model rationale was recorded.

Synthetic ground truth is collapsed by default. You can reveal it for learning; failure filters already use it retrospectively. This session therefore cannot establish blinded human accuracy or real-world time savings.

## What the export means

The download contains the run ID, evidence fingerprint, export time, published verdict, and ordered assessment events (pair ID, decision, note, timestamp). It is a local review-session record, not a reconciliation plan or authority to merge. Reviewer identity is not authenticated and timestamps come from the browser. Keep the downloaded copy if you need a durable record; clearing browser data removes local assessments. Importing sessions is not supported in this increment.

The server provides only the workbench assets and evidence response and accepts no writes. The frozen inputs, logs, metrics, acceptance criteria, and v1 **KILL** verdict remain unchanged. This increment supports the committed synthetic dataset only; CSV import, CRM connections, field-survivorship decisions, group reconciliation, and live merges are outside this slice.
