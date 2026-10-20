"""Day-1 smoke test for the interview VLM endpoint.

Checks, in order:
  1. text-only chat completion works
  2. image + text works (sends one local CT slice as base64 data URL)
  3. native OpenAI `tools=` function calling works (decides the transport layer)

Usage:
  python scripts/smoke_vlm.py [path/to/image.jpg]
Reads OPENAI_BASE_URL / OPENAI_API_KEY / CAIR_MODEL from env or .env.
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

import os  # noqa: E402

MODEL = os.environ.get("CAIR_MODEL", "Qwen3.6-35B-A3B")


def default_image() -> Path:
    """First jpg/png under CAIR_DATA_DIR, so the script works on any unpacked dataset."""
    data_dir = Path(os.environ.get("CAIR_DATA_DIR", "./data_subset10"))
    for pattern in ("**/*.jpg", "**/*.jpeg", "**/*.png"):
        for p in sorted(data_dir.glob(pattern)):
            return p
    sys.exit(f"no image found under {data_dir}; pass an image path: python scripts/smoke_vlm.py path/to/image.jpg")


def data_url(path: Path) -> str:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def report(name: str, t0: float, resp) -> None:
    usage = getattr(resp, "usage", None)
    tok = f"prompt={usage.prompt_tokens} completion={usage.completion_tokens}" if usage else "usage=n/a"
    print(f"[{name}] {time.time() - t0:.1f}s  {tok}")


def main() -> int:
    if not os.environ.get("OPENAI_BASE_URL") or not os.environ.get("OPENAI_API_KEY"):
        sys.exit("OPENAI_BASE_URL and OPENAI_API_KEY are not set. Copy .env.example to .env and fill them in.")
    client = OpenAI(timeout=120)
    img_path = Path(sys.argv[1]) if len(sys.argv) > 1 else default_image()

    # 1. text only
    t0 = time.time()
    r = client.chat.completions.create(
        model=MODEL, temperature=0,
        messages=[{"role": "user", "content": "Reply with exactly: OK"}],
    )
    report("text", t0, r)
    print("   ->", r.choices[0].message.content.strip()[:80])

    # 2. image + text
    t0 = time.time()
    r = client.chat.completions.create(
        model=MODEL, temperature=0,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "What imaging modality and anatomical region is this? Answer in one sentence."},
                {"type": "image_url", "image_url": {"url": data_url(img_path)}},
            ],
        }],
    )
    report("image", t0, r)
    print("   ->", r.choices[0].message.content.strip()[:200])

    # 3. native tools
    tools = [{
        "type": "function",
        "function": {
            "name": "list_studies",
            "description": "List imaging studies for a case.",
            "parameters": {
                "type": "object",
                "properties": {"case_id": {"type": "string"}},
                "required": ["case_id"],
            },
        },
    }]
    t0 = time.time()
    try:
        r = client.chat.completions.create(
            model=MODEL, temperature=0, tools=tools, tool_choice="auto",
            messages=[
                {"role": "system", "content": "You are a diagnostic agent. Use tools to gather evidence before answering."},
                {"role": "user", "content": "Start working on case-01. First, list its studies."},
            ],
        )
        report("tools", t0, r)
        msg = r.choices[0].message
        if msg.tool_calls:
            tc = msg.tool_calls[0]
            print(f"   -> NATIVE TOOL CALL OK: {tc.function.name}({tc.function.arguments})")
            print("\nTRANSPORT DECISION: native")
        else:
            print("   -> no tool_calls returned; content:", (msg.content or "")[:200])
            print("\nTRANSPORT DECISION: json-protocol (model did not emit tool_calls)")
    except Exception as e:  # noqa: BLE001
        print(f"[tools] FAILED: {type(e).__name__}: {str(e)[:300]}")
        print("\nTRANSPORT DECISION: json-protocol (endpoint rejected tools=)")

    # 4. multi-turn tool round trip (tool result message accepted?)
    try:
        t0 = time.time()
        r = client.chat.completions.create(
            model=MODEL, temperature=0, tools=tools,
            messages=[
                {"role": "user", "content": "List studies of case-01 then summarize."},
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "list_studies", "arguments": json.dumps({"case_id": "case-01"})},
                }]},
                {"role": "tool", "tool_call_id": "call_1", "content": json.dumps({"studies": [{"study_id": "study-1", "modality": "ct"}]})},
            ],
        )
        report("tool-roundtrip", t0, r)
        print("   ->", (r.choices[0].message.content or "")[:200])
    except Exception as e:  # noqa: BLE001
        print(f"[tool-roundtrip] FAILED: {type(e).__name__}: {str(e)[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
