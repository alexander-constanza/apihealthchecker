#!/usr/bin/env python3
"""Re-copy the check engine from infra-health-check into this repo.

The engine is vendored rather than pip-installed (see the header the script
writes for why). Vendoring by hand is how a copy drifts: someone fixes a check
here, upstream never learns, and the two diverge until nobody knows which is
authoritative. So the copy is a script, it is the only supported way to update
these files, and it records the upstream version it took them from.

Usage, with the two repos checked out side by side:

    python3 scripts/vendor_engine.py                 # ../infra-health-check
    python3 scripts/vendor_engine.py --source PATH
    python3 scripts/vendor_engine.py --check         # CI: fail if stale
"""
import argparse
import re
import sys
from pathlib import Path

MODULES = ("checks.py", "config.py", "tailscale.py")

HEADER = '''# VENDORED CODE, DO NOT EDIT LIGHTLY.
#
# Source: github.com/alexander-constanza/infra-health-check
# Path in source repo: infra_health/{module}
# Vendored at: version {version}
#
# Why vendored rather than installed as a dependency:
# infra-health-check is a portfolio CLI, not a package published to PyPI, so
# `pip install infra-health-check` does not resolve from a fresh clone or
# inside a Docker build. Rather than depend on a private index or a git URL
# that would break this repo's build the moment the other repo moved, the
# check engine is copied in verbatim. This repo stays a single deployable
# unit with a plain requirements.txt.
#
# What changed from the original: only the import paths, rewritten from
# `infra_health.*` to `apihealthchecker.engine.*`. The check logic, the
# three-state Status (ok / fail / unknown), the retry semantics and the
# thread-pool runner are unmodified upstream code. Upstream is the place to
# fix check behaviour; changes should be made there and re-vendored.
#
# Regenerate with: python3 scripts/vendor_engine.py

'''

_VERSION = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)


def upstream_version(source: Path) -> str:
    text = (source / "infra_health" / "__init__.py").read_text()
    match = _VERSION.search(text)
    if match is None:
        raise SystemExit(f"No __version__ found in {source}/infra_health/__init__.py")
    return match.group(1)


def render(source: Path, module: str, version: str) -> str:
    body = (source / "infra_health" / module).read_text()
    body = body.replace("from infra_health.", "from apihealthchecker.engine.")
    body = body.replace("import infra_health.", "import apihealthchecker.engine.")
    return HEADER.format(module=module, version=version) + body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="../infra-health-check")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write. Exit 1 if any vendored file differs from upstream.",
    )
    args = parser.parse_args(argv)

    source = Path(args.source).resolve()
    if not (source / "infra_health").is_dir():
        raise SystemExit(f"No infra_health package under {source}")

    destination = Path(__file__).resolve().parent.parent / "apihealthchecker" / "engine"
    version = upstream_version(source)

    stale = []
    for module in MODULES:
        rendered = render(source, module, version)
        target = destination / module
        if args.check:
            current = target.read_text() if target.exists() else ""
            if current != rendered:
                stale.append(module)
            continue
        target.write_text(rendered)
        print(f"vendored {module} from infra-health-check {version}")

    if args.check:
        if stale:
            print("stale vendored modules: " + ", ".join(stale), file=sys.stderr)
            print("run: python3 scripts/vendor_engine.py", file=sys.stderr)
            return 1
        print(f"vendored engine matches infra-health-check {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
