"""Reference agent that always chooses the middle value."""

import json
import sys

for line in sys.stdin:
    message = json.loads(line)
    if message["type"] == "init":
        print(json.dumps({"type": "ready"}), flush=True)
    elif message["type"] == "turn":
        print(json.dumps({"type": "action", "action": 5}), flush=True)
    elif message["type"] == "shutdown":
        break
