# The containerized daemon

The daemon ships as an image for hosts where installing it is not wanted. It
runs no Docker Engine of its own: it mounts the host's socket and drives the
same containers a native daemon would. Docker-outside-of-Docker, not
Docker-in-Docker.

This document is the contract. It is written for a program that starts and
stops this container — a tool that needs a daemon and may have to bring one
up itself — and everything here is stable enough to be coded against.

Supported hosts: Linux, and macOS with Docker Desktop. Rootless Docker works;
its socket simply lives somewhere else. Windows is not supported, and neither
is a `DOCKER_HOST` that is not a unix socket.

## Start

```bash
docker run -d \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$HOME/.odoo-sheller:/data/.odoo-sheller" \
  -e ODOO_SHELLER_UID="$(id -u)" -e ODOO_SHELLER_GID="$(id -g)" \
  -p 127.0.0.1:8765:8765 \
  ghcr.io/romi477/odoo-sheller:<version>
```

Every one of those five is required, and each is required for a reason:

| Flag | Why it is not optional |
|---|---|
| `-v …/docker.sock:/var/run/docker.sock` | The only way in to the Engine. Without it the container exits 1 immediately rather than starting into something that can do nothing. |
| `-v $HOME/.odoo-sheller:/data/.odoo-sheller` | Journals must land where a natively installed daemon writes and reads them. A named volume would fork the history by how the daemon happened to be started. |
| `-e ODOO_SHELLER_UID` / `_GID` | Who owns those journals. On a first run there is no directory to infer it from, so without it the container exits 2. |
| `-p 127.0.0.1:8765:8765` | The entire security boundary. See below. |

The container name is yours to pick. Nothing in the contract depends on it
except the MCP command, which a human pastes into a client config.

### Rootless Docker

Only the source of the socket mount changes:

```bash
  -v "$XDG_RUNTIME_DIR/docker.sock:/var/run/docker.sock" \
```

The path inside the container is always `/var/run/docker.sock`. A rootless
Engine sees only its own containers, which is a property of that Engine rather
than of this image.

## Before you start one

A daemon may already be running — natively, or in a container somebody else
started. Starting a second one on the same port fails, so ask first:

1. `GET http://127.0.0.1:8765/health`. An answer means a daemon is serving.
   Use it, and **do not stop it**: it may not be yours.
2. No answer, but `docker ps -q --filter label=io.github.romi477.odoo-sheller`
   finds something — our container exists but is not serving. It is stopped or
   unhealthy; `docker start` it or recreate it.
3. Neither — start one.

Over HTTP it makes no difference which kind answered. Everything below the API
is identical, and a caller that found a daemon can stop caring how it got
there.

Skipping the check is not catastrophic but is untidy: `docker run` exits 125
with `bind: address already in use`, **and leaves the container behind in
`created` state**. With `--name` the next attempt then fails on the name
instead, which sends the reader after the wrong problem. Remove it before
retrying.

## Wait

The image carries a `HEALTHCHECK`. Wait on it instead of polling the port:

```bash
docker inspect --format '{{.State.Health.Status}}' <container>
```

It reports `starting`, then `healthy` once `/health` answers. The daemon opens
no session on its own, so `healthy` means ready.

## What /health says

```json
{
  "ok": true,
  "version": "1.8.0",
  "container": true,
  "mcp": {
    "command": "docker",
    "args": ["exec", "-i", "9f31c2ab77e1", "python", "-m", "odoo_sheller.mcp"]
  }
}
```

`mcp` is the one thing a caller cannot work out for itself. The HTTP API is the
same either way, but an MCP server is spawned rather than called, and a
containerized daemon's has to be spawned inside its own container — which only
that daemon knows the id of. Run the command as given. A native daemon answers
with its own interpreter and `-m odoo_sheller.mcp` instead, and `container` is
`false`.

`"mcp": null` means the daemon cannot say — a frozen build whose MCP tree is
missing. Nothing to retry; a command invented on its behalf would only fail
later.

The endpoint needs no key and reports no session data. On a native daemon the
command holds a local path, which is a filename on a loopback-only endpoint.

## Find

The image carries `io.github.romi477.odoo-sheller=daemon`, and a container inherits
its image's labels. That is how a caller finds what it started — or finds that
somebody else already started one — without remembering a name:

```bash
docker ps -q --filter label=io.github.romi477.odoo-sheller
```

## Stop

```bash
docker rm -f $(docker ps -aq --filter label=io.github.romi477.odoo-sheller)
```

Sessions cannot outlive the daemon: the daemon owns the pipes, so stopping the
container ends every session's container-side process with it. Uncommitted work
is discarded, which is the same rule everywhere else in this tool.

## Exit codes

The entrypoint fails before starting anything, rather than starting into a
state it cannot serve from:

| Code | Meaning |
|---|---|
| 1 | No socket at `/var/run/docker.sock`. It was not mounted. |
| 2 | The state directory does not exist and no `ODOO_SHELLER_UID` was given. |

Code 2 is deliberately a refusal on every host. The entrypoint runs inside a
Linux container and cannot tell which kind of host it is on. On Linux, starting
here would write journals as root that a natively installed daemon could never
append to; on macOS it would have been harmless. One rule for both beats
guessing.

It also prints one line describing what it resolved, before the daemon starts:

```
odoo-sheller: uid=501 gid=20 socket_gid=0 drop=yes
```

Two identities, from two places, and they are not interchangeable: `uid`/`gid`
come from the state mount and decide who owns the journals, `socket_gid` comes
from the socket and decides who can reach the Engine. `drop=no` means the
process stays root — what a mount reports when the host fakes ownership, as
macOS and rootless Docker both do.

## Environment

| Variable | Default | Effect |
|---|---|---|
| `ODOO_SHELLER_UID` / `ODOO_SHELLER_GID` | derived from the state mount | Who the daemon runs as, and who owns the journals. |
| `ODOO_SHELLER_PORT` | `8765` | The port inside the container. The `-p` mapping, the healthcheck and the MCP server all follow it. |
| `ODOO_SHELLER_SOCKET` | `/var/run/docker.sock` | Where the entrypoint looks for the Engine. |
| `ODOO_SHELLER_STATE` | `/data/.odoo-sheller` | Where the state mount is expected. |
| `ODOO_SHELLER_IN_CONTAINER` | `1`, set by the image | Changes what the daemon says about its own exposure. |

## Security

**Publish to `127.0.0.1` and nowhere else.** This API executes arbitrary code as
`SUPERUSER_ID` and has no authentication. Inside the container the daemon binds
`0.0.0.0` — it must, because the container's own loopback is not what `-p`
reaches — so the published port is the only thing keeping the API on this
machine. `-p 8765:8765` offers code execution to the whole network.

**Mounting the socket grants the host's whole Engine**: every container on the
machine, not only the Odoo ones, and the Engine socket is root-equivalent on
the host. This is equally true of a natively installed daemon, which runs as a
user who can already do it. It is worth stating here because a container looks
like isolation and provides none of it in this direction.

Journals are unmasked, as everywhere else, and arrive in the host's
`~/.odoo-sheller` through the bind mount. Review one before sharing it.

The full model is [security.md](security.md).

## Reaching it with an agent

The MCP server travels in the image and nothing starts it. Point an MCP client
at a container by name:

```bash
docker exec -i odoo-sheller python -m odoo_sheller.mcp
```

This is the way a human reaches a stuck module when the daemon was started this
way and nothing else odoo-sheller-related is installed on the host. The server
talks to the daemon over HTTP inside the container and writes nothing itself.

`docker exec` inherits the container's environment, so the server finds the
daemon on whatever `ODOO_SHELLER_PORT` the container serves, including one moved
with `-e`. Set `ODOO_SHELLER_URL` only to point it somewhere else entirely.

## Pulling the image

The GHCR package is private, so a pull needs a token carrying `read:packages`:

```bash
docker login ghcr.io
```

Access is granted per user or team in the package's own settings, which are
separate from the repository's.
