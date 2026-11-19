# Classifier repeat check (diagnostic, not acceptance)

Model `gpt-6-luna`; classifier output budget 512; 5 attempts per instruction per arm; 180 calls (cap 200).
Arms: `default` = effort not sent; `none` = effort `none`. Correct means: equals the route written down beforehand.

## By route (correct / attempts; failures)

| Expected route | default | none |
|---|---|---|
| compare | 10/10; 0 failed | 10/10; 0 failed |
| document_review | 15/15; 0 failed | 15/15; 0 failed |
| qa | 44/50; 0 failed | 50/50; 0 failed |
| summarize | 15/15; 0 failed | 15/15; 0 failed |

## By instruction

| Instruction | Source | Expected | default (routes returned) | none (routes returned) |
|---|---|---|---|---|
| qa-berlin | gold | qa | 5/5 {'qa': 5} | 5/5 {'qa': 5} |
| qa-md-owner | gold | qa | 5/5 {'qa': 5} | 5/5 {'qa': 5} |
| figure-handbook | gold | qa | 5/5 {'qa': 5} | 5/5 {'qa': 5} |
| summarize-release | gold | summarize | 5/5 {'summarize': 5} | 5/5 {'summarize': 5} |
| compare-depots-release | gold | compare | 5/5 {'compare': 5} | 5/5 {'compare': 5} |
| review-launch-plan | gold | document_review | 5/5 {'document_review': 5} | 5/5 {'document_review': 5} |
| diagram-release | gold | qa | 1/5 {'summarize': 4, 'qa': 1} | 5/5 {'qa': 5} |
| diagram-architecture | gold | qa | 5/5 {'qa': 5} | 5/5 {'qa': 5} |
| var-qa-owner | variant | qa | 5/5 {'qa': 5} | 5/5 {'qa': 5} |
| var-qa-count | variant | qa | 5/5 {'qa': 5} | 5/5 {'qa': 5} |
| var-sum-overview | variant | summarize | 5/5 {'summarize': 5} | 5/5 {'summarize': 5} |
| var-sum-bullets | variant | summarize | 5/5 {'summarize': 5} | 5/5 {'summarize': 5} |
| var-cmp-differences | variant | compare | 5/5 {'compare': 5} | 5/5 {'compare': 5} |
| var-rev-proofread | variant | document_review | 5/5 {'document_review': 5} | 5/5 {'document_review': 5} |
| var-rev-critique | variant | document_review | 5/5 {'document_review': 5} | 5/5 {'document_review': 5} |
| var-dia-deploy | variant | qa | 4/5 {'qa': 4, 'summarize': 1} | 5/5 {'qa': 5} |
| var-dia-release | variant | qa | 5/5 {'qa': 5} | 5/5 {'qa': 5} |
| var-dia-system | variant | qa | 4/5 {'qa': 4, 'summarize': 1} | 5/5 {'qa': 5} |

## Tokens, latency, cost

| Arm | Attempts | Input tokens | Output tokens (incl. hidden reasoning) | Mean output | Median s | Max s | Cost |
|---|---|---|---|---|---|---|---|
| default | 90 | 12370 | 2174 | 24.2 | 0.95 | 2.07 | unknown (no prices configured) |
| none | 90 | 12370 | 1215 | 13.5 | 1.0 | 2.01 | unknown (no prices configured) |

## Decision rule 2-4 (rule 1 is the full evaluation)

* 2. diagram-release correct on every attempt under `none`: **True**
* 3. no route worse under `none`, and no failures: **True**
* 4. gold items right every time by default but wrong at least once under `none`: **none**
* Rules 2-4 hold: **True**
