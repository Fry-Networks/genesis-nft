#!/usr/bin/env python3
"""fry.farm Genesis NFT — test batch: generate 6 sample images across all rarity tiers."""

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DALLE_MODEL = "dall-e-3"
DALLE_SIZE = "1024x1024"
DALLE_QUALITY = "hd"
DALLE_STYLE = "vivid"
RATE_LIMIT_DELAY = 13  # seconds between requests

OUTPUT_DIR = Path("./output")
TEST_DIR = OUTPUT_DIR / "test_batch"


def main():
    # Check API key
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("ERROR: OPENAI_API_KEY not set.")
        print("  export OPENAI_API_KEY=sk-...")
        sys.exit(1)

    # Ensure traits exist
    traits_path = OUTPUT_DIR / "all_traits.json"
    if not traits_path.exists():
        print("Traits not found — running generate.py --traits-only...")
        result = subprocess.run([sys.executable, "generate.py", "--traits-only"], check=True)
        if not traits_path.exists():
            print("ERROR: generate.py failed to create all_traits.json")
            sys.exit(1)

    all_nfts = json.loads(traits_path.read_text())
    print(f"Loaded {len(all_nfts)} NFTs from all_traits.json")

    # Pick 6 test NFTs: one per rarity tier + the single rarest
    picks = {}
    for tier in ["Common", "Uncommon", "Rare", "Epic", "Legendary"]:
        candidates = [n for n in all_nfts if n["overall_rarity"] == tier]
        if candidates:
            # Pick the middle one for a representative sample
            picks[tier] = candidates[len(candidates) // 2]

    # The single rarest by score
    rarest = max(all_nfts, key=lambda n: n["rarity_score"])
    if rarest["token_id"] not in [p["token_id"] for p in picks.values()]:
        picks["Rarest"] = rarest
    else:
        # Already included, pick second rarest
        sorted_by_score = sorted(all_nfts, key=lambda n: n["rarity_score"], reverse=True)
        for n in sorted_by_score:
            if n["token_id"] not in [p["token_id"] for p in picks.values()]:
                picks["Rarest"] = n
                break

    TEST_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f" Test Batch — {len(picks)} images to generate")
    print(f"{'='*60}")
    for label, nft in picks.items():
        print(f"  {label:<12} → Token #{nft['token_id']}  Score: {nft['rarity_score']:.4f}  Overall: {nft['overall_rarity']}")
    print()

    # Import build_prompt from generate module
    sys.path.insert(0, str(Path(__file__).parent))
    from generate import build_prompt

    from openai import OpenAI
    client = OpenAI()

    success = 0
    failed = 0
    items = list(picks.items())

    for i, (label, nft) in enumerate(items):
        token_id = nft["token_id"]
        rarity = nft["overall_rarity"]
        filename = f"{token_id}_{rarity}.png"
        img_path = TEST_DIR / filename
        prompt_path = TEST_DIR / f"{token_id}_{rarity}_prompt.txt"
        revised_path = TEST_DIR / f"{token_id}_{rarity}_revised.txt"

        prompt = build_prompt(nft)
        print(f"[{i+1}/{len(items)}] Generating #{token_id} ({label}, {rarity})...")

        try:
            response = client.images.generate(
                model=DALLE_MODEL,
                prompt=prompt,
                size=DALLE_SIZE,
                quality=DALLE_QUALITY,
                style=DALLE_STYLE,
                response_format="b64_json",
                n=1,
            )
            b64_data = response.data[0].b64_json
            revised_prompt = response.data[0].revised_prompt or ""
            image_bytes = base64.b64decode(b64_data)

            img_path.write_bytes(image_bytes)
            prompt_path.write_text(prompt)
            if revised_prompt:
                revised_path.write_text(revised_prompt)

            sha256 = hashlib.sha256(image_bytes).hexdigest()
            size_kb = len(image_bytes) / 1024
            print(f"  ✓ Saved {filename} ({size_kb:.0f} KB, sha256: {sha256[:12]}...)")
            success += 1

        except Exception as e:
            print(f"  ✗ FAILED: {e}")
            failed += 1

        if i < len(items) - 1:
            print(f"  (waiting {RATE_LIMIT_DELAY}s...)")
            time.sleep(RATE_LIMIT_DELAY)

    # Summary
    cost = success * 0.08
    print(f"\n{'='*60}")
    print(f" Test Batch Complete")
    print(f"{'='*60}")
    print(f"  Generated: {success}/{len(items)}")
    print(f"  Failed:    {failed}/{len(items)}")
    print(f"  Cost:      ${cost:.2f} ({success} × $0.08)")
    print()

    # Print trait details for each generated image
    print(f"── Generated Image Details {'─'*33}")
    for label, nft in picks.items():
        rarity = nft["overall_rarity"]
        filename = f"{nft['token_id']}_{rarity}.png"
        img_path = TEST_DIR / filename
        if img_path.exists():
            size_kb = img_path.stat().st_size / 1024
            print(f"\n  Token #{nft['token_id']} — {label} ({rarity}, score: {nft['rarity_score']:.4f}) [{size_kb:.0f} KB]")
            for cat, val in nft["traits"].items():
                print(f"    {cat:<12} [{nft['trait_rarities'][cat]:<10}] {val}")
    print()


if __name__ == "__main__":
    main()
