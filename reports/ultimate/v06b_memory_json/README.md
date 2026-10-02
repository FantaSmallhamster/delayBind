# V06b tested snapshot

- Authoritative run: `results/ultimate/v06b_memory_json_full128_r1_retry3`
- Result: 95/128 exact match, accuracy 0.7421875, F1 0.7625744047619047
- Completion: 128 OK, 128 runtime ANSWERED, 128 replayed states
- Configuration: JSON UPDATE and JSON MEMORY, both using provider `json_schema`
- Audit: `audit.json`
- Paired rows: `paired_diff.csv`

The earlier `v06b_memory_json_full128_r1` run did not activate JSON MEMORY because
the experiment configuration omitted the new mapping. Retry 2 exposed fact-text
references in source checks. Retry 3 includes the prompt and local exact-alias fix
and is the version preserved by the `v06b-memory-json` tag.
