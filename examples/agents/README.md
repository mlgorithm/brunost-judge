# Example agents

Both bundles implement the v1 JSONL protocol and can be checked locally:

```bash
brunost agent validate examples/agents/steady --smoke
brunost agent validate examples/agents/round-robin --smoke
```

Use them as registered participant artifacts for
`examples/games/closest-number`.
