# Three recorded decision examples

[Back to README](../README.md) · [Visitor walkthrough](visitor-walkthrough.md)

These are selected examples from `published-202-2fd9d168`, not an accuracy sample. Fields come from the committed seed 202 exports; probabilities come from main-arm calls, ordered by repeat. Routing is confirmed by the score-mode decision ledger. Ground truth is shown only for retrospective explanation, not as judge input.

The AI combines two repeat probabilities by their arithmetic mean: at least 0.8 recommends merge; at most 0.3 means no match; values between those cutoffs go to human review. These are recorded simulation decisions; no live Account was merged. Overall v1 remains **KILL** on calibration.

## Merge recommendation

Pair ID: `org_a:0015A00B0Ac1tvyQUA|org_b:0018g0cFAIunaYoA2I`

| Field | Left Account | Right Account |
|---|---|---|
| Id | 0015A00B0Ac1tvyQUA | 0018g0cFAIunaYoA2I |
| Name | Draegrim Capital Inc | Draegrim Capital Inc |
| Website | https://www.draegrim.example/about | WWW.WYHOUM61.EXAMPLE |
| Phone | (415) 555-0134 | 1-415-555-0140 |
| BillingPostalCode | 02790 | 02790 |

Recorded repeat probabilities: **0.88**, **0.84**. Mean: **0.860**. Routing: `merge_ai`.

The mean meets the frozen merge cutoff of 0.8. Ground truth: **same entity**.

## No match

Pair ID: `org_a:0015A001yOiy4m9QQA|org_a:0015A0OPjH5LhedQWC`

| Field | Left Account | Right Account |
|---|---|---|
| Id | 0015A001yOiy4m9QQA | 0015A0OPjH5LhedQWC |
| Name | Zagouve Robotics Ltd. | Haedraim Networks LLC |
| Website | http://zagouve.example | https://www.haedraim.example/about |
| Phone | 1-305-555-0150 | 305.555.0150 |
| BillingPostalCode | 96943 | 42261 |

Recorded repeat probabilities: **0.01**, **0.01**. Mean: **0.010**. Routing: `nomatch_ai`.

The mean is below the frozen no-match cutoff of 0.3. Ground truth: **different entities**.

## Human review

Pair ID: `org_a:0015A0G0JzVIs2FQKT|org_b:0018g0OzrKJipDWASZ`

| Field | Left Account | Right Account |
|---|---|---|
| Id | 0015A0G0JzVIs2FQKT | 0018g0OzrKJipDWASZ |
| Name | Miostaith Industries Corp. | Taihiox Industries Corp. |
| Website | https://www.miostaith.example/about | — |
| Phone | (813) 555-0145 | (813) 555-0145 |
| BillingPostalCode | 82590 | 46633 |

Recorded repeat probabilities: **0.08**, **0.68**. Mean: **0.380**. Routing: `review_ai_uncertain`.

The repeat answers disagree; their mean falls between the frozen cutoffs, so the pair remains for human review. Ground truth: **different entities**.

## Inspect the sources

- [Org A export](../data/synthetic/202/org_a/Account.csv) and [Org B export](../data/synthetic/202/org_b/Account.csv): find the Account IDs above.
- [Raw calls](../runs/published-202-2fd9d168/calls.jsonl): find the pair ID and `arm: main`; inspect repeats 1 and 2.
- [Decision ledger](../runs/published-202-2fd9d168/decisions.jsonl): find the pair ID in `step` and `mode: score` to inspect cutoff checks.
- [Ground truth](../data/ground_truth/202/entity_map.csv): compare `entity_id` for the two records.
- [Review queue](../results/review_queue.csv): the human-review example is included here with its recorded Splink scores.

Field similarities are visible for the reader; no model rationale was recorded, so these examples do not claim why the judge produced its probabilities.
