#!/usr/bin/env python3
"""
Scribe → DeepL → Confluence pipeline.

Steps:
  1. Fetch all SOPs from the Scribe API and save as markdown to ./input/
  2. Translate each file from English to German via DeepL
  3. Push each translated file to Confluence (create or update page)

Usage:
  python pipeline.py

  # Or run individual steps:
  python pipeline.py --step fetch
  python pipeline.py --step translate
  python pipeline.py --step push
"""

import argparse
import os
import re
import sys
from pathlib import Path

import deepl
import markdown as md_lib
import requests
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────

SCRIBE_API_KEY            = os.getenv("SCRIBE_API_KEY")
DEEPL_API_KEY             = os.getenv("DEEPL_API_KEY")
CONFLUENCE_BASE_URL       = os.getenv("CONFLUENCE_BASE_URL")   # e.g. https://your-org.atlassian.net
CONFLUENCE_USERNAME       = os.getenv("CONFLUENCE_USERNAME")
CONFLUENCE_API_TOKEN      = os.getenv("CONFLUENCE_API_TOKEN")
CONFLUENCE_SPACE_KEY      = os.getenv("CONFLUENCE_SPACE_KEY")
CONFLUENCE_PARENT_PAGE_ID = os.getenv("CONFLUENCE_PARENT_PAGE_ID")  # optional
TARGET_LANGUAGE           = os.getenv("TARGET_LANGUAGE", "de").upper()
INPUT_DIR                 = Path(os.getenv("INPUT_DIR", "input"))
OUTPUT_DIR                = Path(os.getenv("OUTPUT_DIR", "output"))

SCRIBE_API_BASE  = "https://api.scribehow.com"
CONFLUENCE_API   = f"{CONFLUENCE_BASE_URL}/wiki/rest/api" if CONFLUENCE_BASE_URL else ""


# ── Shared helpers ─────────────────────────────────────────────────────────────

def slugify(title: str) -> str:
    """Convert a title to a safe filename (max 80 chars)."""
    slug = re.sub(r"[^\w\-]", "_", title).strip("_")
    return slug[:80]


def validate_env(required: dict[str, str | None]) -> None:
    missing = [k for k, v in required.items() if not v]
    if missing:
        sys.exit(f"[ERROR] Missing environment variables: {', '.join(missing)}")


# ── Step 1: Fetch SOPs from Scribe ────────────────────────────────────────────

def scribe_headers() -> dict:
    return {"Authorization": f"Bearer {SCRIBE_API_KEY}"}


def list_scribe_sops() -> list[dict]:
    """
    Return [{id, title}, ...] for every SOP in the Scribe team.

    NOTE: Verify the exact endpoint path against your Scribe API docs
    at https://api.scribehow.com/docs — the list endpoint and response
    shape may differ depending on your account type.
    """
    url = f"{SCRIBE_API_BASE}/api/v1/team/scribes"
    resp = requests.get(url, headers=scribe_headers(), timeout=30)
    resp.raise_for_status()
    data = resp.json()

    # Scribe may return {"scribes": [...]} or a bare list — handle both.
    items = data.get("scribes", data) if isinstance(data, dict) else data
    return [{"id": item["id"], "title": item["title"]} for item in items]


def fetch_scribe_markdown(scribe_id: str) -> str:
    """
    Download a single Scribe SOP as markdown.

    NOTE: Confirm the export endpoint path in the Scribe API docs.
    A common pattern is GET /api/v1/scribes/{id}/export?format=markdown
    or GET /api/v1/scribes/{id}/markdown.
    """
    url = f"{SCRIBE_API_BASE}/api/v1/scribes/{scribe_id}/export"
    resp = requests.get(
        url,
        headers=scribe_headers(),
        params={"format": "markdown"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.text


def fetch_all_sops() -> None:
    """Fetch all SOPs from Scribe and save to INPUT_DIR."""
    validate_env({"SCRIBE_API_KEY": SCRIBE_API_KEY})
    INPUT_DIR.mkdir(parents=True, exist_ok=True)

    sops = list_scribe_sops()
    print(f"[Scribe] Found {len(sops)} SOP(s).")

    for sop in sops:
        filename = f"{slugify(sop['title'])}.md"
        filepath = INPUT_DIR / filename
        print(f"  Downloading: {sop['title']!r} → {filepath}")
        content = fetch_scribe_markdown(sop["id"])
        filepath.write_text(content, encoding="utf-8")

    print(f"[Scribe] Done. Files saved to {INPUT_DIR}/\n")


# ── Step 2: Translate with DeepL ──────────────────────────────────────────────

def translate_files() -> None:
    """Translate all .md files in INPUT_DIR and write results to OUTPUT_DIR."""
    validate_env({"DEEPL_API_KEY": DEEPL_API_KEY})
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    files = sorted(INPUT_DIR.glob("*.md"))
    if not files:
        print("[DeepL] No .md files found in input/ — nothing to translate.")
        return

    translator = deepl.Translator(DEEPL_API_KEY)
    print(f"[DeepL] Translating {len(files)} file(s) → {TARGET_LANGUAGE}...")

    for filepath in files:
        text = filepath.read_text(encoding="utf-8")
        result = translator.translate_text(
            text,
            target_lang=TARGET_LANGUAGE,
            tag_handling="markdown",   # preserves markdown syntax during translation
        )
        out_path = OUTPUT_DIR / filepath.name
        out_path.write_text(result.text, encoding="utf-8")
        print(f"  Translated: {filepath.name}")

    print(f"[DeepL] Done. Translated files in {OUTPUT_DIR}/\n")


# ── Step 3: Push to Confluence ────────────────────────────────────────────────

def confluence_auth() -> tuple[str, str]:
    return (CONFLUENCE_USERNAME, CONFLUENCE_API_TOKEN)


def confluence_headers() -> dict:
    return {"Content-Type": "application/json", "Accept": "application/json"}


def markdown_to_storage(text: str) -> str:
    """
    Convert markdown to Confluence storage format (XHTML).
    Uses the Python `markdown` library — no Confluence plugin required.
    """
    html = md_lib.markdown(
        text,
        extensions=["tables", "fenced_code", "nl2br", "sane_lists"],
    )
    return html


def find_page(title: str) -> dict | None:
    """Return the existing page dict if found in the space, else None."""
    resp = requests.get(
        f"{CONFLUENCE_API}/content",
        params={"spaceKey": CONFLUENCE_SPACE_KEY, "title": title, "expand": "version"},
        auth=confluence_auth(),
        headers=confluence_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def create_page(title: str, body: str) -> None:
    payload: dict = {
        "type": "page",
        "title": title,
        "space": {"key": CONFLUENCE_SPACE_KEY},
        "body": {"storage": {"value": body, "representation": "storage"}},
    }
    if CONFLUENCE_PARENT_PAGE_ID:
        payload["ancestors"] = [{"id": CONFLUENCE_PARENT_PAGE_ID}]

    resp = requests.post(
        f"{CONFLUENCE_API}/content",
        json=payload,
        auth=confluence_auth(),
        headers=confluence_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    page_id = resp.json()["id"]
    print(f"  Created: {title!r} (id={page_id})")


def update_page(page_id: str, version: int, title: str, body: str) -> None:
    payload = {
        "type": "page",
        "title": title,
        "version": {"number": version + 1},
        "body": {"storage": {"value": body, "representation": "storage"}},
    }
    resp = requests.put(
        f"{CONFLUENCE_API}/content/{page_id}",
        json=payload,
        auth=confluence_auth(),
        headers=confluence_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    print(f"  Updated: {title!r} (id={page_id}, v{version + 1})")


def push_to_confluence() -> None:
    """Push all translated .md files from OUTPUT_DIR to Confluence."""
    validate_env({
        "CONFLUENCE_BASE_URL":  CONFLUENCE_BASE_URL,
        "CONFLUENCE_USERNAME":  CONFLUENCE_USERNAME,
        "CONFLUENCE_API_TOKEN": CONFLUENCE_API_TOKEN,
        "CONFLUENCE_SPACE_KEY": CONFLUENCE_SPACE_KEY,
    })

    files = sorted(OUTPUT_DIR.glob("*.md"))
    if not files:
        print("[Confluence] No files found in output/ — nothing to push.")
        return

    print(f"[Confluence] Pushing {len(files)} page(s) to space {CONFLUENCE_SPACE_KEY!r}...")

    for filepath in files:
        # Derive title from filename: underscores → spaces
        title = filepath.stem.replace("_", " ").strip()
        content = filepath.read_text(encoding="utf-8")
        body = markdown_to_storage(content)

        existing = find_page(title)
        if existing:
            update_page(
                page_id=existing["id"],
                version=existing["version"]["number"],
                title=title,
                body=body,
            )
        else:
            create_page(title, body)

    print("[Confluence] Done.\n")


# ── CLI entry point ────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Scribe → DeepL → Confluence pipeline")
    parser.add_argument(
        "--step",
        choices=["fetch", "translate", "push"],
        help="Run only a single step instead of the full pipeline.",
    )
    args = parser.parse_args()

    if args.step == "fetch":
        fetch_all_sops()
    elif args.step == "translate":
        translate_files()
    elif args.step == "push":
        push_to_confluence()
    else:
        # Full pipeline
        fetch_all_sops()
        translate_files()
        push_to_confluence()


if __name__ == "__main__":
    main()
