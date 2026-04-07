#!/usr/bin/env python3
"""fry.farm Genesis NFT — trait generation + DALL-E 3 image pipeline."""

import argparse
import base64
import hashlib
import json
import os
import random
import sys
import time
from pathlib import Path

# ── Constants ───────────────────────────────────────────────────────────────
TOTAL_SUPPLY = 1000
COLLECTION_NAME = "fry.farm Genesis"
COLLECTION_DESCRIPTION = (
    "Limited edition Genesis Pass for fry.farm — 1,000 unique cyberpunk french fry "
    "characters. Holders receive 10% of all fry.farm platform fees distributed "
    "monthly in FRY."
)
EXTERNAL_URL = "https://fry.farm"
SEED = 42069
DALLE_MODEL = "dall-e-3"
DALLE_SIZE = "1024x1024"
DALLE_QUALITY = "hd"
DALLE_STYLE = "vivid"
REQUESTS_PER_MINUTE = 5
RETRY_DELAY = 30
MAX_RETRIES = 3

# ── Rarity tiers & weights ─────────────────────────────────────────────────
RARITY_TIERS = ["Common", "Uncommon", "Rare", "Epic", "Legendary"]
RARITY_WEIGHTS = [50, 25, 15, 7.5, 2.5]
RARITY_VALUES = {"Common": 1, "Uncommon": 2, "Rare": 3, "Epic": 4, "Legendary": 5}

# Overall rarity thresholds (calibrated for target distribution)
OVERALL_THRESHOLDS = [
    (2.83, "Legendary"),
    (2.50, "Epic"),
    (2.10, "Rare"),
    (1.83, "Uncommon"),
]

# ── Trait definitions ───────────────────────────────────────────────────────
TRAITS = {
    "Background": {
        "Common": [
            "solid dark charcoal gray",
            "solid dark navy blue",
            "solid dark slate",
            "solid matte black",
        ],
        "Uncommon": [
            "dark neon-lit city alleyway with rain",
            "dark underground tunnel with flickering neon signs",
            "dimly lit street food market with red lanterns",
            "rainy neon rooftop with puddle reflections",
        ],
        "Rare": [
            "cyberpunk rooftop overlooking a sprawling neon skyline",
            "dark high-tech laboratory with glowing monitors",
            "underground hacker den with holographic screens",
            "futuristic transit station with digital billboards",
        ],
        "Epic": [
            "massive server room with rows of blinking lights in darkness",
            "digital matrix rain cascading in a dark void",
            "dark control room of a space station with Earth visible",
            "neon-drenched blockchain visualization chamber",
        ],
        "Legendary": [
            "cosmic void with swirling blockchain data streams and galaxies",
            "ethereal dark grid plane extending to infinity with golden light",
            "dark quantum realm with fractured reality and glowing particles",
            "inside a living circuit board universe with pulsing energy veins",
        ],
    },
    "Eyes": {
        "Common": [
            "simple small glowing white dot eyes",
            "basic round black eyes with slight glow",
            "small friendly oval eyes",
            "simple half-closed relaxed eyes",
        ],
        "Uncommon": [
            "horizontal LED strip visor eyes glowing red",
            "narrow cyberpunk visor with blue light",
            "rectangular pixel eyes like an LED display",
            "eyes behind small round tech goggles",
        ],
        "Rare": [
            "one cyber monocle eye with HUD display and one normal eye",
            "glowing green targeting reticle eyes",
            "eyes with scrolling code reflected in them",
            "asymmetric eyes — one red one blue cybernetic",
        ],
        "Epic": [
            "holographic prismatic eyes projecting data",
            "scanner beam eyes emitting a sweeping red line",
            "eyes replaced by twin floating hologram orbs",
            "deep purple glowing eyes with circuit patterns in the iris",
        ],
        "Legendary": [
            "void black eyes with tiny galaxies swirling inside",
            "eyes made of pure burning golden data streams",
            "eyes that are portals showing blockchain transactions flowing",
            "radiant white-hot eyes with energy tendrils extending outward",
        ],
    },
    "Outfit": {
        "Common": [
            "plain dark gray hoodie",
            "simple black t-shirt",
            "basic dark red crew neck sweater",
            "plain dark zip-up jacket",
        ],
        "Uncommon": [
            "dark tactical tech vest with small LED strips",
            "military-style cargo jacket with circuit patches",
            "dark bomber jacket with subtle neon piping",
            "utility vest covered in small pouches and cables",
        ],
        "Rare": [
            "sleek dark mechanical suit with glowing joint seams",
            "high-tech lab coat with holographic badge and arm displays",
            "armored streetwear jacket with LED shoulder pads",
            "dark trenchcoat lined with fiber optic threads",
        ],
        "Epic": [
            "partial exoskeleton frame over dark bodysuit with blue accents",
            "full hacker rig suit covered in attached devices and cables",
            "nano-fiber stealth armor with adaptive camouflage shimmer",
            "dark power armor torso piece with glowing core reactor",
        ],
        "Legendary": [
            "full nano-armor suit with shifting liquid metal surface",
            "void cloak that seems to absorb light with star-like particles",
            "transcendent energy armor made of pure golden light constructs",
            "living dark crystalline armor that pulses with inner fire",
        ],
    },
    "Headgear": {
        "Common": [
            "no headgear",
            "simple dark beanie",
            "plain black snapback cap",
            "no headgear",
        ],
        "Uncommon": [
            "over-ear cyberpunk headphones with LED accents",
            "backwards tech cap with small antenna",
            "dark bandana with circuit board pattern",
            "welding goggles pushed up on forehead",
        ],
        "Rare": [
            "sleek open-face cyber helmet with HUD visor",
            "antenna array headband receiving signals",
            "half-helmet with built-in comms system",
            "tech-enhanced dark hood with illuminated trim",
        ],
        "Epic": [
            "neural interface crown with glowing nodes at temples",
            "floating holographic data ring orbiting the head",
            "dark helmet with full face display showing code",
            "brain-jack headset with cables trailing upward",
        ],
        "Legendary": [
            "crown of burning red and gold energy flames",
            "halo of pure data — orbiting golden symbols and glyphs",
            "reality-bending headpiece that fractures the space around it",
            "ancient tech crown fused with crystalline blockchain shards",
        ],
    },
    "Held Item": {
        "Common": [
            "nothing in hands",
            "holding a basic smartphone",
            "nothing in hands",
            "holding a can of soda",
        ],
        "Uncommon": [
            "holding an open glowing laptop",
            "holding a wrench with circuitry patterns",
            "holding a data tablet showing charts",
            "holding a soldering iron with glowing tip",
        ],
        "Rare": [
            "holding a glowing energy drink canister",
            "holding a small IoT sensor device emitting signals",
            "holding a translucent data crystal",
            "holding a modified drone controller",
        ],
        "Epic": [
            "holding a miniature mining rig with blinking lights",
            "holding a plasma multi-tool crackling with energy",
            "holding a floating holographic globe of network nodes",
            "holding a dark cube emitting red light from its seams",
        ],
        "Legendary": [
            "holding an infinity orb of swirling golden blockchain data",
            "holding a blade made of pure crystallized data",
            "holding a miniature star that illuminates the whole scene",
            "holding an ancient artifact fused with futuristic tech, radiating power",
        ],
    },
    "Aura": {
        "Common": [
            "no visible aura",
            "no visible aura",
            "no visible aura",
            "very faint warm ambient glow around the character",
        ],
        "Uncommon": [
            "faint red glow emanating from the character",
            "subtle warm crimson outline glow",
            "soft reddish ambient light around shoulders",
            "gentle pulsing dark red underglow",
        ],
        "Rare": [
            "blue circuit line patterns glowing on the skin surface",
            "electric blue energy crackling along the arms",
            "cyan data streams flowing around the body",
            "teal wireframe overlay partially visible on the character",
        ],
        "Epic": [
            "purple energy field surrounding the character",
            "violet electromagnetic aura with visible force lines",
            "dark purple and magenta plasma wisps orbiting the body",
            "deep indigo shield-like energy barrier partially visible",
        ],
        "Legendary": [
            "intense golden flame aura engulfing the character",
            "massive particle storm of gold and white light radiating outward",
            "reality-warping aura — the space around the character bends and glows",
            "supernova-level radiance — white-gold energy explosion frozen in time",
        ],
    },
}

PROMPT_TEMPLATE = """Digital art portrait, bust shot from shoulders up, of a cyberpunk humanoid french fry character. The character is an anthropomorphic french fry — golden-yellow crispy potato body shaped like a thick-cut fry, with a face, arms, and personality. The art style is dark, techy, and cyberpunk with a moody cinematic atmosphere.

Character traits:
- Background: {background}
- Eyes: {eyes}
- Outfit: {outfit}
- Headgear: {headgear}
- Held item: {held_item}
- Aura effect: {aura}

Style requirements: Dark moody lighting, high contrast, cinematic composition, detailed textures, cyberpunk aesthetic. The french fry character should look cool, confident, and slightly menacing. Rich detail on textures — crispy potato surface visible. No text, no watermarks, no logos. Square composition, centered bust portrait."""


# ── Trait generation ────────────────────────────────────────────────────────
def generate_all_traits(rng: random.Random) -> list[dict]:
    """Generate traits for all TOTAL_SUPPLY NFTs deterministically."""
    all_nfts = []
    for token_id in range(1, TOTAL_SUPPLY + 1):
        traits = {}
        trait_rarities = {}
        for category in TRAITS:
            tier = rng.choices(RARITY_TIERS, weights=RARITY_WEIGHTS, k=1)[0]
            option = rng.choice(TRAITS[category][tier])
            traits[category] = option
            trait_rarities[category] = tier

        # Calculate rarity score
        score = sum(RARITY_VALUES[r] for r in trait_rarities.values()) / len(trait_rarities)
        overall = "Common"
        for threshold, label in OVERALL_THRESHOLDS:
            if score >= threshold:
                overall = label
                break

        all_nfts.append({
            "token_id": token_id,
            "traits": traits,
            "trait_rarities": trait_rarities,
            "rarity_score": round(score, 4),
            "overall_rarity": overall,
        })
    return all_nfts


def build_prompt(nft: dict) -> str:
    t = nft["traits"]
    return PROMPT_TEMPLATE.format(
        background=t["Background"],
        eyes=t["Eyes"],
        outfit=t["Outfit"],
        headgear=t["Headgear"],
        held_item=t["Held Item"],
        aura=t["Aura"],
    )


def build_metadata(nft: dict, image_hash: str = "") -> dict:
    attributes = []
    for category in TRAITS:
        attributes.append({"trait_type": category, "value": nft["traits"][category]})
        attributes.append({"trait_type": f"{category} Rarity", "value": nft["trait_rarities"][category]})
    attributes.append({"trait_type": "Overall Rarity", "value": nft["overall_rarity"]})
    attributes.append({"trait_type": "Rarity Score", "value": nft["rarity_score"]})

    return {
        "name": f"fry.farm Genesis #{nft['token_id']}",
        "description": COLLECTION_DESCRIPTION,
        "image": "",
        "image_integrity": f"sha256-{image_hash}" if image_hash else "",
        "external_url": EXTERNAL_URL,
        "attributes": attributes,
        "properties": {
            "token_id": nft["token_id"],
            "collection": COLLECTION_NAME,
            "rarity_score": nft["rarity_score"],
            "overall_rarity": nft["overall_rarity"],
        },
    }


# ── Image generation ────────────────────────────────────────────────────────
def generate_image(nft: dict, output_dir: Path) -> bool:
    """Generate a single image via DALL-E 3. Returns True on success."""
    from openai import OpenAI

    token_id = nft["token_id"]
    img_path = output_dir / "images" / f"{token_id}.png"
    meta_path = output_dir / "metadata" / f"{token_id}.json"
    prompt_path = output_dir / "prompts" / f"{token_id}.txt"
    revised_path = output_dir / "prompts" / f"{token_id}_revised.txt"

    if img_path.exists():
        print(f"  #{token_id}: already exists, skipping")
        return True

    prompt = build_prompt(nft)
    client = OpenAI()

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"  #{token_id}: generating (attempt {attempt}/{MAX_RETRIES})...")
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

            # Save image
            img_path.write_bytes(image_bytes)

            # SHA-256
            sha256 = hashlib.sha256(image_bytes).hexdigest()

            # Save metadata
            meta = build_metadata(nft, sha256)
            meta_path.write_text(json.dumps(meta, indent=2))

            # Save prompts
            prompt_path.write_text(prompt)
            if revised_prompt:
                revised_path.write_text(revised_prompt)

            size_kb = len(image_bytes) / 1024
            print(f"  #{token_id}: saved ({size_kb:.0f} KB, sha256: {sha256[:12]}...)")
            return True

        except Exception as e:
            err = str(e)
            if "rate_limit" in err.lower() or "429" in err:
                print(f"  #{token_id}: rate limited, waiting {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
            else:
                print(f"  #{token_id}: error — {err}")
                if attempt == MAX_RETRIES:
                    return False
                time.sleep(5)
    return False


# ── Rarity report ───────────────────────────────────────────────────────────
def print_rarity_report(all_nfts: list[dict]) -> None:
    total = len(all_nfts)
    print(f"\n{'='*60}")
    print(f" fry.farm Genesis — Rarity Distribution Report")
    print(f" Total supply: {total}")
    print(f" Seed: {SEED}")
    print(f"{'='*60}")

    # Overall rarity
    print(f"\n── Overall Rarity {'─'*41}")
    counts = {}
    for tier in RARITY_TIERS:
        counts[tier] = sum(1 for n in all_nfts if n["overall_rarity"] == tier)

    for tier in RARITY_TIERS:
        c = counts[tier]
        pct = c / total * 100
        bar = "█" * int(pct / 2) + "░" * (50 - int(pct / 2))
        print(f"  {tier:<12} {c:>4} ({pct:5.1f}%) {bar}")

    # Per-trait breakdown
    print(f"\n── Per-Trait Rarity Breakdown {'─'*30}")
    for category in TRAITS:
        print(f"\n  {category}:")
        for tier in RARITY_TIERS:
            c = sum(1 for n in all_nfts if n["trait_rarities"][category] == tier)
            pct = c / total * 100
            bar = "█" * int(pct / 2)
            print(f"    {tier:<12} {c:>4} ({pct:5.1f}%) {bar}")

    # Top 10 rarest
    print(f"\n── Top 10 Rarest NFTs {'─'*38}")
    sorted_nfts = sorted(all_nfts, key=lambda n: n["rarity_score"], reverse=True)
    for i, nft in enumerate(sorted_nfts[:10], 1):
        print(f"\n  #{i} — Token #{nft['token_id']}  "
              f"Score: {nft['rarity_score']:.4f}  "
              f"Overall: {nft['overall_rarity']}")
        for cat in TRAITS:
            print(f"      {cat:<12} [{nft['trait_rarities'][cat]:<10}] {nft['traits'][cat]}")

    print(f"\n{'='*60}\n")


# ── CLI ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="fry.farm Genesis NFT generator")
    parser.add_argument("--traits-only", action="store_true", help="Generate traits + metadata only (no API calls)")
    parser.add_argument("--generate", action="store_true", help="Generate images via DALL-E 3")
    parser.add_argument("--regenerate", type=int, metavar="N", help="Regenerate a single token")
    parser.add_argument("--start", type=int, default=1, help="Start of generation range")
    parser.add_argument("--end", type=int, default=TOTAL_SUPPLY, help="End of generation range")
    parser.add_argument("--output", type=str, default="./output", help="Output directory")
    parser.add_argument("--report", action="store_true", help="Print rarity distribution report only")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "images").mkdir(exist_ok=True)
    (output_dir / "metadata").mkdir(exist_ok=True)
    (output_dir / "prompts").mkdir(exist_ok=True)

    traits_path = output_dir / "all_traits.json"

    # If --report only, load existing traits
    if args.report:
        if not traits_path.exists():
            print("ERROR: all_traits.json not found. Run --traits-only first.")
            sys.exit(1)
        all_nfts = json.loads(traits_path.read_text())
        print_rarity_report(all_nfts)
        return

    # Generate traits
    print(f"Generating traits for {TOTAL_SUPPLY} NFTs (seed={SEED})...")
    rng = random.Random(SEED)
    all_nfts = generate_all_traits(rng)

    # Save all_traits.json
    traits_path.write_text(json.dumps(all_nfts, indent=2))
    print(f"Saved {len(all_nfts)} trait sets → {traits_path}")

    # Save individual metadata files (without image hashes for now)
    meta_count = 0
    for nft in all_nfts:
        meta_path = output_dir / "metadata" / f"{nft['token_id']}.json"
        meta = build_metadata(nft)
        meta_path.write_text(json.dumps(meta, indent=2))
        meta_count += 1
    print(f"Saved {meta_count} metadata files → {output_dir / 'metadata'}/")

    # Print report
    print_rarity_report(all_nfts)

    if args.traits_only:
        print("Done (traits only — no images generated).")
        return

    # Image generation
    if args.generate or args.regenerate is not None:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            print("ERROR: OPENAI_API_KEY not set. Export it before generating images.")
            print("  export OPENAI_API_KEY=sk-...")
            sys.exit(1)

        if args.regenerate is not None:
            targets = [n for n in all_nfts if n["token_id"] == args.regenerate]
            if not targets:
                print(f"ERROR: Token #{args.regenerate} not found.")
                sys.exit(1)
            # Delete existing to force regeneration
            img = output_dir / "images" / f"{args.regenerate}.png"
            if img.exists():
                img.unlink()
        else:
            targets = [n for n in all_nfts if args.start <= n["token_id"] <= args.end]

        print(f"\nGenerating {len(targets)} images via DALL-E 3...")
        print(f"  Model: {DALLE_MODEL}, Size: {DALLE_SIZE}, Quality: {DALLE_QUALITY}, Style: {DALLE_STYLE}")
        print(f"  Rate limit: {REQUESTS_PER_MINUTE} req/min\n")

        success = 0
        failed_list = []
        delay = 60 / REQUESTS_PER_MINUTE

        for i, nft in enumerate(targets):
            ok = generate_image(nft, output_dir)
            if ok:
                success += 1
            else:
                failed_list.append(nft["token_id"])

            if i < len(targets) - 1:
                print(f"  (waiting {delay:.0f}s for rate limit...)")
                time.sleep(delay)

        print(f"\nDone: {success} succeeded, {len(failed_list)} failed")
        if failed_list:
            failed_path = output_dir / "failed.json"
            failed_path.write_text(json.dumps(failed_list, indent=2))
            print(f"Failed tokens saved → {failed_path}")


if __name__ == "__main__":
    main()
