"""Check relative Markdown links and images without contacting external websites."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit


def check_links(root: Path) -> list[str]:
    """Return missing local link targets in the repository's Markdown documentation."""
    errors = []
    documents = [root / "README.md", *(root / "docs").rglob("*.md")]
    for path in documents:
        text = re.sub(r"```.*?```", "", path.read_text(), flags=re.DOTALL)
        for match in re.finditer(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", text):
            target = urlsplit(match.group(1).strip("<>"))
            if target.scheme or target.netloc or not target.path:
                continue
            destination = (path.parent / unquote(target.path)).resolve()
            if not destination.exists():
                line = text[: match.start()].count("\n") + 1
                errors.append(f"{path.relative_to(root)}:{line}: missing {target.path}")
    return errors


def main() -> None:
    """Check all documentation targets using the Git repository root."""
    root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
    errors = check_links(root)
    if errors:
        raise SystemExit("\n".join(errors))
    print("All local documentation link targets exist.")


if __name__ == "__main__":
    main()
