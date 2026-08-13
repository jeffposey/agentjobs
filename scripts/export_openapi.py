"""Export the exact FastAPI OpenAPI document without starting a server."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentjobs.api.main import app


def render_openapi() -> str:
    """Return a deterministic representation of the document served at /openapi.json."""
    return json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Path to the checked-in OpenAPI JSON file")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail instead of writing when the checked-in document is stale",
    )
    args = parser.parse_args()
    expected = render_openapi()

    if args.check:
        actual = args.output.read_text(encoding="utf-8") if args.output.is_file() else None
        if actual != expected:
            print(f"{args.output} is stale; run `npm run generate:api` from frontend/.")
            return 1
        print(f"{args.output} matches the FastAPI OpenAPI document.")
        return 0

    args.output.write_text(expected, encoding="utf-8", newline="\n")
    print(f"Wrote {args.output} from the FastAPI OpenAPI document.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
