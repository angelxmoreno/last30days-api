"""Find the latest upstream release; if newer than UPSTREAM_REF, update the file.

Prints the new tag and writes a PR body (changelog excerpt) to --body-file. Opening the PR is
done by the workflow. Never auto-merges: a human reviews the PR.

    uv run python -m scripts.bump_upstream --body-file pr_body.md
"""

import json
import sys
import urllib.request
from pathlib import Path

REPO = "mvanhorn/last30days-skill"
REF_FILE = Path(__file__).resolve().parent.parent / "UPSTREAM_REF"


def latest_release() -> dict[str, str]:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "last30days-api-bump"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data: dict[str, str] = json.load(resp)
    return data


def body_for(old: str, rel: dict[str, str]) -> str:
    notes = (rel.get("body") or "(no release notes)").strip()
    if len(notes) > 3000:
        notes = notes[:3000] + "\n\n… (truncated)"
    return (
        f"Bump upstream `{old}` -> `{rel['tag_name']}`.\n\n{rel.get('html_url', '')}\n\n"
        f"### Upstream release notes\n\n{notes}\n\n"
        "CI runs the pinned engine's doctor, records a tiny real query, and diffs the JSON shape "
        "against the previous fixture. Merge by hand only if that check is green. "
        "If shapes changed, update the normalizer and fixtures in this PR first."
    )


def main() -> int:
    old = REF_FILE.read_text().strip()
    rel = latest_release()
    new = rel["tag_name"]
    if new == old:
        print(f"up to date at {old}", file=sys.stderr)
        return 0
    REF_FILE.write_text(new + "\n")
    if "--body-file" in sys.argv:
        Path(sys.argv[sys.argv.index("--body-file") + 1]).write_text(body_for(old, rel))
    print(new)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
