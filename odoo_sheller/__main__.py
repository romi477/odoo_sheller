"""Run the daemon: odoo-sheller [--reload], or python -m odoo_sheller."""

import argparse
import os
from pathlib import Path

import uvicorn

from odoo_sheller.registry import load_admin_key

IN_CONTAINER_ENV = "ODOO_SHELLER_IN_CONTAINER"


def bind_warning(host: str, in_container: bool) -> str | None:
    """What to say about this daemon's exposure, or nothing.

    Two modes, two different boundaries. On a host, the bind address is the
    boundary and anything but loopback widens it. In a container, binding
    loopback would make the published port unreachable, so 0.0.0.0 is
    mandatory and the boundary is how the port was published instead —
    which this process cannot see, and therefore states rather than checks.
    """
    if in_container:

        return (
            "note: containerized, binding 0.0.0.0 inside the container. What "
            "keeps this API on the machine is how the port was published: "
            "-p 127.0.0.1:8765:8765. Anything wider offers unauthenticated "
            "code execution as SUPERUSER_ID to whatever can reach it."
        )

    if host != "127.0.0.1":

        return (
            f"warning: binding {host} exposes an unauthenticated code-execution "
            "API beyond this machine"
        )

    return None


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

    notice = bind_warning(args.host, bool(os.environ.get(IN_CONTAINER_ENV)))
    if notice:
        print(notice)

    # Printed, never served: the UI sits behind the same unauthenticated API, so
    # an endpoint handing this out would give it to anything that can fetch a
    # page. Paste it into the UI once when it asks.
    print(f"admin key: {load_admin_key()}")
    print(f"ui:   http://{args.host}:{args.port}/web")
    print(f"docs: http://{args.host}:{args.port}/docs")

    # The import-string form is for ``--reload`` only. A freeze has no source
    # tree to watch, and uvicorn's string import is how hidden imports get
    # missed; passing the app object is what a packaged binary can actually
    # start.
    if args.reload:
        uvicorn.run(
            "odoo_sheller.api:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=True,
            reload_dirs=[str(Path(__file__).parent)],
            reload_excludes=["web/*"],
            timeout_graceful_shutdown=5,
        )

        return

    from odoo_sheller.api import create_app

    uvicorn.run(
        create_app(),
        host=args.host,
        port=args.port,
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
