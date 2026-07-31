# Metric Interpretation

## Contents

- WER and aggregation
- Convergence and stability
- Error-type diagnosis
- Long-audio and Context diagnostics
- Decision checklist

## WER and aggregation

`wer_group_summary.txt` reports group-level WER. An unweighted mean across groups balances categories but does not equal token-pooled WER. State this whenever using a macro.

`wer_summary.txt` reports task-level WER and SER. Use task-level values to find long-tail failures hidden by group means.

When exact pooled impact matters, sum `edits` and `tokens` from each task's `wer_pengcheng.log`; do not average task WERs and call it pooled WER.

## Convergence and stability

A good deployment checkpoint should combine:

- low macro WER;
- small regression from its own best;
- acceptable last-window variance;
- no catastrophic task-level failures.

The latest checkpoint is not automatically best. A low isolated checkpoint is not automatically trustworthy. Compare adjacent checkpoints and inspect the tasks responsible for the low point.

Use concrete language:

- “still improving”: latest points establish new lows across several groups;
- “platform”: values fluctuate in a narrow band without sustained gain;
- “regressing”: latest values stay materially above an earlier best;
- “anomaly-dominated”: the macro movement is mostly caused by one or two tasks.

## Error-type diagnosis

Read `%WER ... [ edits / tokens, ins, del, sub ]` from `wer_pengcheng.log`.

- Substitution growth across many tasks suggests recognition quality regression.
- Deletion growth suggests under-generation, truncation, or missed speech.
- Insertion growth with stable substitutions/deletions suggests repetition or hallucination.
- SER can remain similar while WER explodes if a few already-wrong utterances become extremely long; therefore inspect insertion counts and output lengths.

Compare `pred.txt` and `lab.txt` line counts and IDs. Different line counts can be valid when long-audio references are split between `lab.txt` and `lab_bg30s.txt`; verify `wer_pengcheng.txt` before declaring a file mismatch.

## Long-audio and Context diagnostics

Context and long-audio sets may combine:

- `pred_30s.txt`: short/first segments;
- `pred_bg30s.txt`: background or over-30s segments;
- `lab_bg30s.txt`: matching long-segment references;
- `pred.txt` / `lab.txt`: merged evaluation inputs.

Check:

1. WER, WER_30s, and WER_ITN_bg30s separately.
2. Maximum and P99 prediction length.
3. Repeated n-grams or repeated characters.
4. Whether the same task recovers at nearby checkpoints.
5. Whether the Context and Context-dialogue variants fail on the same utterance.

If insertion counts change by thousands while substitutions remain stable, describe the issue as generation/termination instability, not Context comprehension failure.

## Decision checklist

Before recommending a checkpoint, answer:

- What scope is used: all groups, non-Context, or business-weighted?
- Is the comparison equal-step or each-run-best?
- Is the winner stable over adjacent checkpoints?
- Which groups create the advantage?
- Which tasks create the disadvantage?
- Does removing a confirmed repetition outlier change the ranking?
- Is the recommendation for general deployment, dialect OOD, Context, or training efficiency?
