"""Type-check the Python code blocks in the given Markdown files, as Microsoft agent-framework's docs check does.

Usage: python scripts/check_markdown_code.py README.md docs/*.md

Each fenced Python block that imports ``agent_framework`` is written to a temporary file and checked with
pyright, in the environment of the interpreter running this script. Only two kinds of error fail the check,
as in Microsoft's: an ``agent_framework`` import that does not resolve, and an attribute that does not exist.
Snippets legitimately leave out context, such as top-level ``await`` or a helper defined in a sample, so
other errors are ignored. The README's agent example imports ``agent_framework.openai``, so run the script
with agent-framework-openai installed.
"""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

BLOCK = re.compile(r"^```python\n(.*?)^```", re.MULTILINE | re.DOTALL)
CONFIG = {
    "include": ["."],
    "typeCheckingMode": "off",
    "reportMissingImports": "error",
    "reportAttributeAccessIssue": "error",
}


def check(code: str) -> list[str]:
    """The relevant pyright errors for one snippet."""
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / "pyrightconfig.json").write_text(json.dumps(CONFIG), encoding="utf-8")
        (Path(directory) / "snippet.py").write_text(code, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "pyright", "--pythonpath", sys.executable, "-p", directory],
            capture_output=True,
            text=True,
            check=False,
        )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"pyright could not run:\n{result.stderr or result.stdout}")
    return [
        line.strip()
        for line in result.stdout.splitlines()
        if ("reportMissingImports" in line and "agent_framework" in line) or "reportAttributeAccessIssue" in line
    ]


def main(paths: list[str]) -> int:
    """Check every Python block in ``paths``; return the exit status."""
    checked = failing = 0
    for path in paths:
        text = Path(path).read_text(encoding="utf-8")
        for match in BLOCK.finditer(text):
            code = match.group(1)
            if "import agent_framework" not in code and "from agent_framework" not in code:
                continue
            checked += 1
            line = text[: match.start()].count("\n") + 1
            errors = check(code)
            print(f"{'FAIL' if errors else 'ok'} {path}:{line}")
            for error in errors:
                print(f"    {error}")
            failing += bool(errors)
    print(f"{checked} blocks checked, {failing} failing")
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
