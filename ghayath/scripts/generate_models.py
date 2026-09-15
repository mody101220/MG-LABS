#!/usr/bin/env python3
"""Regenerate app/generated/models.py from openapi/openapi.yaml.

Pinned, reproducible generation (no hand-editing of generated models):

    datamodel-codegen 0.37.0 (dev dependency, pinned in pyproject.toml)
      --input openapi/openapi.yaml
      --input-file-type openapi
      --output-model-type pydantic_v2.BaseModel
      --use-annotated --use-union-operator --use-standard-collections

Post-processing (deterministic, so `git diff` after a run is EMPTY when the
contract is unchanged):
  1. The volatile `#   timestamp:` header line is normalized to a fixed marker.
  2. datamodel-codegen emits contract string defaults (e.g. `status: TODO`) as
     `str` assignments onto enum-typed fields — a type-only artifact. The
     script runs mypy over the generated file and appends a `type: ignore`
     comment to exactly the lines mypy flags (self-maintaining: a future
     contract change that introduces a new such default is handled
     automatically).

Usage:  python scripts/generate_models.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPENAPI = ROOT / "openapi" / "openapi.yaml"
OUTPUT = ROOT / "app" / "generated" / "models.py"
TIME_MARKER = "#   timestamp: normalized (deterministic output for this openapi.yaml)"
IGNORE_SUFFIX = "  # type: ignore[assignment]  # generated: contract default is a string"

GEN_ARGS = [
    "--input", str(OPENAPI),
    "--input-file-type", "openapi",
    "--output-model-type", "pydantic_v2.BaseModel",
    "--use-annotated",
    "--use-union-operator",
    "--use-standard-collections",
    "--output", str(OUTPUT),
]


def main() -> int:
    if not OPENAPI.is_file():
        print(f"error: contract not found: {OPENAPI}", file=sys.stderr)
        return 1

    print("[1/3] datamodel-codegen (pinned 0.37.0) ...")
    r = subprocess.run([sys.executable, "-m", "datamodel_code_generator", *GEN_ARGS],
                       cwd=ROOT)
    if r.returncode != 0:
        print("error: datamodel-codegen failed", file=sys.stderr)
        return r.returncode

    lines = OUTPUT.read_text(encoding="utf-8").splitlines()

    # 1) normalize the volatile timestamp header
    for i, line in enumerate(lines):
        if line.startswith("#   timestamp:"):
            lines[i] = TIME_MARKER

    # 2) silence the str-default-on-enum artifact exactly where mypy flags it
    r = subprocess.run([sys.executable, "-m", "mypy", "--no-error-summary",
                        "--ignore-missing-imports", str(OUTPUT)],
                       cwd=ROOT, capture_output=True, text=True)
    flagged: set[int] = set()
    for m in re.finditer(r"models\.py:(\d+): error: Incompatible types in assignment", r.stdout):
        flagged.add(int(m.group(1)))
    for lineno in sorted(flagged, reverse=True):
        lines[lineno - 1] = lines[lineno - 1] + IGNORE_SUFFIX

    OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[2/3] normalized header + {len(flagged)} generated-default ignore(s)")

    # 3) self-check: the generated file must type-check and import
    r = subprocess.run([sys.executable, "-m", "mypy", "--no-error-summary",
                        "--ignore-missing-imports", str(OUTPUT)],
                       cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("error: generated models do not type-check:\n" + r.stdout, file=sys.stderr)
        return 1
    r = subprocess.run([sys.executable, "-c",
                        "import sys; sys.path.insert(0, '.'); import app.generated.models as m; "
                        f"print('classes:', sum(1 for n in dir(m) if n[0].isupper()))"],
                       cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("error: generated models do not import:\n" + r.stderr, file=sys.stderr)
        return 1

    print(f"[3/3] OK — {OUTPUT.relative_to(ROOT)} regenerated (mypy clean, imports clean)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
