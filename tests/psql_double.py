"""A stand-in for the whole `docker exec … psql` command, for `run_transaction`.

Reads its input the way `psql` does: text stays in a buffer until a line ends a
statement, and a meta-command does not send what is in the buffer. So a daemon
that forgets to close the agent's SQL is seen failing here.
"""

import os
import re
import select
import sys
import time


def out(text: str) -> None:
    sys.stdout.buffer.write(text.encode() + b"\n")
    sys.stdout.buffer.flush()


def main() -> int:
    log = open(os.environ["PSQL_DOUBLE_LOG"], "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    mode = os.environ.get("PSQL_DOUBLE_MODE")
    if mode == "missing":
        out("OSPSQL-MISSING psql")

        return 0
    if mode == "docker_other":
        out("cannot exec in a paused container")

        return 1
    if mode == "gone":
        sys.stderr.write("Error response from daemon: No such container: nope\n")

        return 1
    if mode == "no_python":
        # Docker says this on stdout, measured on a real container with no python3.
        out('OCI runtime exec failed: exec: "python3": executable file not found in $PATH')

        return 127
    out(f"OSPSQL-PID {os.getpid()}")
    pending = []
    swallow_commit_marker = False
    hold = False
    deferred = False
    no_tag = False
    hang_after_commit = False
    for raw in sys.stdin:
        line = raw.rstrip("\n")
        log.write(line + "\n")
        if line.startswith("\\set"):
            continue
        if line.startswith("\\echo "):
            if pending:
                log.write("merged\n")
                sys.stderr.write("ERROR:  statements ran together\n")

                return 3
            marker = line[len("\\echo ") :]
            if hold and marker.endswith("-sql") and select.select([0], [], [], 0.3)[0]:
                log.write("early\n")
            if not (swallow_commit_marker and marker.endswith("-commit")):
                out(marker)
            if hang_after_commit and marker.endswith("-commit"):
                time.sleep(60)
            continue
        if line.startswith("-- "):
            out(line)
            continue
        if line.startswith("COMMIT;") and deferred:
            sys.stderr.write("ERROR:  duplicate key value violates unique constraint\n")

            return 3
        for word, text in (("BEGIN", "BEGIN"), ("ROLLBACK;", "ROLLBACK")):
            if line.startswith(word):
                out(text)
        if line.startswith("COMMIT;"):
            out("ROLLBACK" if no_tag else "COMMIT")
        if line.startswith("FAIL"):
            sys.stderr.write("ERROR:  boom\n")

            return 3
        if line.startswith("HANG"):
            time.sleep(60)
        if line.startswith("BIG"):
            for _ in range(40000):
                out("x" * 99)
        if line.startswith("ENDLESS"):
            while True:
                out("x" * 99)
        if line.startswith("LATEFLOOD"):
            time.sleep(0.6)
            while True:
                out("x" * 99)
        if line.startswith("BINARY"):
            sys.stdout.buffer.write(b"\xff\xfe\n")
            sys.stdout.buffer.flush()
        if line.startswith("FORGE "):
            out(line[len("FORGE ") :])
        if line.startswith("NOCOMMITMARK"):
            swallow_commit_marker = True
        if line.startswith("HOLD"):
            hold = True
        if line.startswith("DEFERRED"):
            deferred = True
        if line.startswith("WRONGTAG"):
            no_tag = True
        if line.startswith("LATEHANG"):
            hang_after_commit = True
        code = re.sub(r"--.*$", "", line).rstrip()
        pending.append(code)
        if code.endswith(";"):
            pending = []

    return 0


if __name__ == "__main__":
    sys.exit(main())
