# V08 tested snapshot

- Parent: `v07-confirmed-links` (`a396c4e0013df03e8504b67335d81a6eaf14595d`)
- Authoritative run: `results/ultimate/v08_final_evidence_full128_r1`
- Result: 98/128 exact match, accuracy 0.765625, F1 0.7805059523809523
- Completion: 127 OK, 1 ERROR, 127 replayed states
- Paired against V07: +6 exact, +0.040244391025640924 F1; 12 gains and 6 losses
- Against V06b: +3 exact, +0.017931547619047583 F1
- Against V06a: equal exact (98), -0.0049479166666667185 F1
- Against V04: +2 exact, +0.003757440476190421 F1

Final-evidence audit: 1,134 persisted source assessments, 128 packs built,
128 complete ANSWER requests measured, no budget failures, and no evidence-pack
invariant violations. The largest ANSWER request was 8,120 tokens under the
8,192-token limit. Three samples required deterministic trimming of supplemental
context or hints while preserving their confirmed chains.

The single error is `dev_4298`: both ANSWER attempts reached the 4,096 output-token
limit without emitting the required boxed answer. Its 2,307-token input was under
budget. The observed error is retained; no row from another run was substituted.
