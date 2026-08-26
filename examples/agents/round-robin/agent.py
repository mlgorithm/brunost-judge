"""Reference agent that chooses a deterministic value per round."""

import json
import sys

for line in sys.stdin:
    message = json.loads(line)
    if message["type"] == "init":
        print(json.dumps({"type": "ready"}), flush=True)
    elif message["type"] == "turn":
        round_number = message["state"]["round"]
        print(json.dumps({"type": "action", "action": (round_number * 3) % 10}), flush=True)
    elif message["type"] == "shutdown":
        break
