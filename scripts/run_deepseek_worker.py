"""Historical opt-in Ollama coding-worker launcher using low reasoning effort and no model fallback.

This does not authorize its use instead of headless Codex.
"""

import argparse
import json
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MODEL = "deepseek-v4.1-flash:1017-cloud"
RESPONSE_MODELS = {MODEL, "deepseek-v4.1-flash:1017"}
ROOT = Path(__file__).resolve().parents[1]
ENDPOINT = "http://127.0.0.1:11434/api/chat"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brief", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    run = args.run_dir.resolve()
    if run == ROOT or ROOT in run.parents:
        parser.error("Worker receipts must live outside the product checkout")
    run.mkdir(parents=True, exist_ok=False)
    policy = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    messages = [
        {
            "role": "system",
            "content": policy
            + "\nOnly perform the supplied unit. Use PowerShell for commands. Report exact changes and verification; do not claim unrun tests passed.",
        },
        {"role": "user", "content": args.brief.read_text(encoding="utf-8")},
    ]
    tool = {
        "type": "function",
        "function": {
            "name": "powershell",
            "description": "Run a PowerShell command in the TextFlowKit checkout.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
    meta = {
        "requested_model": MODEL,
        "think": "low",
        "endpoint": ENDPOINT,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "returned_models": [],
    }
    for turn in range(20):
        payload = {"model": MODEL, "think": "low", "stream": False, "messages": messages}
        if not args.probe:
            payload["tools"] = [tool]
        request = urllib.request.Request(
            ENDPOINT, json.dumps(payload).encode(), {"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.load(response)
        (run / f"response-{turn:02d}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        returned = result.get("model")
        meta["returned_models"].append(returned)
        (run / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        if returned not in RESPONSE_MODELS:
            raise RuntimeError(f"Unexpected returned model: {returned!r}; no fallback permitted")
        message = result["message"]
        messages.append(message)
        calls = message.get("tool_calls", [])
        if not calls:
            (run / "report.md").write_text(message.get("content", ""), encoding="utf-8")
            print(json.dumps(meta), flush=True)
            return
        for call_index, call in enumerate(calls):
            if call["function"]["name"] != "powershell":
                raise RuntimeError("Unexpected tool")
            command = call["function"]["arguments"]["command"]
            command_path = run / f"command-{turn:02d}-{call_index:02d}.ps1"
            command_path.write_text(command, encoding="utf-8")
            try:
                completed = subprocess.run(
                    ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(command_path)],
                    check=False,
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=120,
                )
                output = f"Exit {completed.returncode}\n{completed.stdout}\n{completed.stderr}"
            except subprocess.TimeoutExpired:
                output = "Command timed out after 120 seconds; do not claim completion."
            with (run / "tools.jsonl").open("a", encoding="utf-8") as log:
                log.write(
                    json.dumps({"command": command, "script": str(command_path), "output": output}, ensure_ascii=False) + "\n"
                )

            messages.append({"role": "tool", "tool_name": "powershell", "content": output[-24000:]})
    raise RuntimeError("Worker exceeded the 20-turn unit limit")


if __name__ == "__main__":
    main()
