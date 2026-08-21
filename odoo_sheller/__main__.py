"""Run the daemon: python -m odoo_sheller [--reload]."""

import argparse
from pathlib import Path

import uvicorn

from odoo_sheller.registry import load_admin_key


def main() -> None:
    parser = argparse.ArgumentParser(prog="odoo-sheller", description=__doc__)
    # 127.0.0.1 only by default: this API executes arbitrary code as SUPERUSER_ID.
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--reload",
        action="store_true",
        help=(
            "restart on Python changes (development only). Every restart kills "
            "the live sessions: the daemon owns the pipes, so the container-side "
            "processes die with it. Files under web/ are ignored — reload the "
            "browser page instead."
        ),
    )
    args = parser.parse_args()

    if args.host != "127.0.0.1":
        print(
            f"warning: binding {args.host} exposes an unauthenticated code-execution "
            "API beyond this machine",
        )

    # Printed, never served: the UI sits behind the same unauthenticated API, so
    # an endpoint handing this out would give it to anything that can fetch a
    # page. Paste it into the UI once when it asks.
    print(f"admin key: {load_admin_key()}")
    print(f"ui:   http://{args.host}:{args.port}/web")
    print(f"docs: http://{args.host}:{args.port}/docs")

    uvicorn.run(
        "odoo_sheller.api:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=[str(Path(__file__).parent)] if args.reload else None,
        reload_excludes=["web/*"] if args.reload else None,
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
