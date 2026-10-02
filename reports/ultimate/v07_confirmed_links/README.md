# V07 tested snapshot

- Parent: `v06b-memory-json` (`7db67304ea32febb4a6040445b983e287858eb5c`)
- Authoritative run: `results/ultimate/v07_confirmed_links_full128_r1`
- Result: 92/128 exact match, accuracy 0.71875, F1 0.7402615613553114
- Completion: 127 OK, 1 ERROR, 127 replayed states
- Relationship audit: 270 confirmed binding links, 0 candidate links, 0 violations
- Paired against V06b: 6 gains, 9 losses (the losses include the error)

The single error is `dev_2261`. Both ANSWER attempts exhausted the output budget
without emitting the required boxed answer. The run and error are retained as
observed; no rows from another run were substituted.
