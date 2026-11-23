"""`execute` against real Redis and Postgres: output, outcome, and a cancel that
lands mid-statement.

Each run executes in a subprocess, as under the worker: `execute` installs a
signal handler, so it needs a main thread.
"""

import subprocess
import sys
import time

import pytest
from redis import Redis

from macrostrat.task_runner import request_cancel
from macrostrat.task_runner.output import output_key

# Sleeps in Postgres in steps, swallowing errors per step the way a library
# would, so only a BaseException can end it early. `slow` uses a connection of
# its own, which a cancel must reach through PGAPPNAME.
RUNNER = """
import json, sys
from sqlalchemy import create_engine, text
from macrostrat.task_runner import execute

run_id, db_url, redis_url, steps, seconds, own_connection = sys.argv[1:]

def task(db, ctx):
    from rich import print as rich_print
    engine = create_engine(db_url) if own_connection == "1" else db.engine
    rich_print("[bold green]e2e[/] start")
    for i in range(int(steps)):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT pg_sleep(:s)"), dict(s=float(seconds)))
        except Exception as err:
            print(f"  caught {type(err).__name__}")
        print(f"  step {i} done")
    return {"steps": int(steps)}

outcome = execute(run_id, task, db_url=db_url, redis_url=redis_url)
print(json.dumps(outcome.__dict__))
"""


@pytest.fixture(scope="module")
def redis_url(docker_client):
    container = docker_client.containers.run(
        "redis:7-alpine", detach=True, remove=True, ports={"6379/tcp": None}
    )
    try:
        container.reload()
        port = container.ports["6379/tcp"][0]["HostPort"]
        url = f"redis://localhost:{port}/0"
        client = Redis.from_url(url)
        for _ in range(50):
            try:
                client.ping()
                break
            except Exception:
                time.sleep(0.1)
        yield url
    finally:
        container.stop()


def _start(run_id, database_url, redis_url, steps, seconds, own_connection=False):
    db_url = database_url
    if not isinstance(db_url, str):
        db_url = db_url.render_as_string(hide_password=False)
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            RUNNER,
            run_id,
            db_url,
            redis_url,
            str(steps),
            str(seconds),
            "1" if own_connection else "0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _outcome(proc, timeout):
    out, err = proc.communicate(timeout=timeout)
    assert proc.returncode == 0, err
    import json

    return json.loads(out.strip().splitlines()[-1])


def test_run_succeeds_and_returns_its_output(database_url, redis_url):
    proc = _start("e2e-ok", database_url, redis_url, steps=2, seconds=0.1)
    outcome = _outcome(proc, timeout=60)
    assert outcome["state"] == "succeeded"
    assert outcome["result"] == {"steps": 2}
    assert "e2e" in outcome["log"] and "step 1 done" in outcome["log"]
    assert "\x1b[" in outcome["log"]
    assert Redis.from_url(redis_url).ttl(output_key("e2e-ok")) > 0


@pytest.mark.parametrize("own_connection", [False, True])
def test_cancel_interrupts_a_running_statement(database_url, redis_url, own_connection):
    run_id = f"e2e-cancel-{int(own_connection)}"
    redis = Redis.from_url(redis_url)
    proc = _start(run_id, database_url, redis_url, 5, 20, own_connection)
    deadline = time.time() + 30
    while not redis.exists(output_key(run_id)):
        assert time.time() < deadline, proc.stderr.read()
        time.sleep(0.1)
    # Let it get into the first pg_sleep.
    time.sleep(1)
    t0 = time.time()
    assert request_cancel(redis, run_id) == 1
    outcome = _outcome(proc, timeout=30)
    assert outcome["state"] == "cancelled"
    # Immediate: not the 20 s the statement would have taken, nor a later step.
    assert time.time() - t0 < 10
    assert "cancelled" in outcome["log"] and "step 0 done" not in outcome["log"]
