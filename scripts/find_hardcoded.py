"""List hard-coded text content in the package: non-ASCII characters and \\u/\\N escapes.

The converter must not carry its own symbol tables: symbols, unit symbols and
typographic characters come from pandoc, pylatexenc, pint or the TeX installation.
Exit status 1 when anything is found, so this can run in CI.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ESCAPE = re.compile(r"\\u[0-9a-fA-F]{4}|\\U[0-9a-fA-F]{8}|\\N\{[^}]+\}|\\x[89a-fA-F][0-9a-fA-F]")


def scan(root: Path) -> list[str]:
    hits = []
    for path in sorted(root.rglob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0] if not line.lstrip().startswith(("\"", "'")) else line
            if any(ord(c) > 127 for c in code) or ESCAPE.search(code):
                hits.append(f"{path.as_posix()}:{n}: {line.strip()[:140]}")
    return hits


if __name__ == "__main__":
    found = scan(Path(sys.argv[1] if len(sys.argv) > 1 else "src"))
    print("\n".join(found) or "no hard-coded non-ASCII content")
    sys.exit(1 if found else 0)
