from __future__ import annotations

import argparse
import io
import os
from pathlib import Path
import sys

from dotenv import load_dotenv
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare direct llama.cpp vision with the bot prompt, without Discord traffic."
    )
    parser.add_argument("--model", help="llama-server alias, e.g. gemma4:e4b")
    parser.add_argument("--base-url", help="llama-server URL; defaults to .env or localhost")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    if args.base_url:
        os.environ["LLAMA_CPP_BASE_URL"] = args.base_url
    if args.model:
        os.environ["LLAMA_CPP_ALLOWED_MODELS"] = args.model
        os.environ["LLAMA_CPP_DEFAULT_MODEL"] = args.model
        os.environ["MENTION_LLAMA_CPP_MODEL"] = args.model

    from llm.client import LlamaCppError, get_mention_model, llama_supports_vision, query_llm
    from llm.capacity import CapacityError
    from llm.responses import generate_mention_result

    try:
        model = get_mention_model()
        enabled = llama_supports_vision()
        print(f"Model: {model}; server advertises vision: {enabled}", flush=True)
        if not enabled:
            print("Vision is disabled. Check that the matching mmproj is loaded.")
            return 1
        picture = Image.new("RGB", (256, 256), "white")
        ImageDraw.Draw(picture).ellipse((48, 48, 208, 208), fill="red")
        output = io.BytesIO()
        picture.save(output, format="PNG")
        image = {"image_bytes": output.getvalue(), "image_mime": "image/png"}
        question = "What shape and color are visible in the attached image?"
        print("Testing direct vision (no history, memory, or JSON schema)...", flush=True)
        direct = query_llm(
            question, model=model, options={"max_tokens": 128, "temperature": 0},
            **image,
        )
        print(f"Direct answer: {direct}", flush=True)
        print("Testing the bot's structured mention prompt...", flush=True)
        result = generate_mention_result("Vision check", question, model=model, **image)
        print(f"Bot answer: {result.text if result else '[no valid visual answer]'}")
        print("Expected in BOTH answers: a red circle on a white background.")
        print(
            "If both fail, check the llama.cpp build and matching model/mmproj. "
            "If only the bot fails, investigate its prompt/schema. "
            "If both pass, inspect bot.log for the Discord request's image flag."
        )
        return 0 if result else 1
    except (LlamaCppError, CapacityError) as exc:
        print(f"Vision check failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
