from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import docker
from docker import DockerClient
from testcontainers.postgres import PostgresContainer

from macrostrat.database import Database
from macrostrat.utils import get_logger

log = get_logger(__name__)


@contextmanager
def database_cluster(
    image_tag: str,
    *,
    build: bool = False,
    context: Path = None,
    optimize_for_testing: bool = False,
    data_volume: str = None,
    user: str = None,
    database: str = None,
    driver: str = None,
    environment: Optional[dict[str, str]] = None,
    config: dict | Path = None,
    in_memory: bool = True,
    docker_client: Optional[DockerClient] = None,
    **kwargs,
):
    """
    Start a database cluster in a Docker volume
    Context manager to create a temporary database cluster
    """

    print("Starting database cluster using image %s" % image_tag)

    should_build = build and (context is not None)

    # Check if image exists locally to avoid build
    client = docker_client or docker.from_env()
    if not should_build:
        try:
            client.images.get(image_tag)
            log.info(f"Using existing image {image_tag}")
        except docker.errors.ImageNotFound:
            if context is not None:
                should_build = True
            # else: testcontainers will pull the image automatically

    if should_build:
        client.images.build(path=str(context), tag=image_tag)

    # Begin run configurations
    # Default to the postgres superuser/database so testcontainers' readiness
    # check (psql --username ... --dbname ...) can always connect, even when
    # attaching to an existing volume with no separate user/db configured.
    kwargs.setdefault("username", user or "postgres")
    kwargs.setdefault("dbname", database or "postgres")

    container = PostgresContainer(image_tag, **kwargs)

    _config = {}

    _environment = {}
    if environment is not None:
        _environment.update(environment)
    _environment.setdefault("POSTGRES_HOST_AUTH_METHOD", "trust")
    container.with_envs(**_environment)

    if isinstance(config, Path):
        assert config.is_file()
        key = str(config.resolve())
        internal_config_file_path = "/etc/postgresql-config.conf"
        container.with_volume_mapping(key, internal_config_file_path, mode="ro")
        config["config_file"] = internal_config_file_path
    elif config is not None:
        _config.update(config)

    if data_volume is not None:
        # We can't optimize for testing if we're using a data volume
        in_memory = False
        optimize_for_testing = False
        container.with_volume_mapping(
            data_volume, "/var/lib/postgresql/data", mode="rw"
        )

    if optimize_for_testing:
        _config = {
            **_config,
            "synchronous_commit": "off",
            "fsync": "off",
            "wal_level": "minimal",
            "max_wal_senders": "0",
            "full_page_writes": "off",
            "checkpoint_completion_target": "0.9",
        }
        in_memory = True

    if in_memory:
        # testcontainers >= 4.x owns `tmpfs` itself and passes it to Docker
        # explicitly, so smuggling it through `with_kwargs` collides with that
        # argument ("got multiple values for keyword argument 'tmpfs'"). Use the
        # dedicated mount API where it exists, and fall back for older versions.
        data_dir = "/var/lib/postgresql/data"
        options = "uid=999,gid=999,mode=0700"
        if hasattr(container, "with_tmpfs_mount"):
            container.with_tmpfs_mount(data_dir, options)
        else:
            container.with_kwargs(tmpfs={data_dir: options})

    cmd = build_postgres_command(_config)
    if cmd is not None:
        container.with_command(cmd)
    container.start()
    log.info(f"Container started!")
    try:
        _url = container.get_connection_url(driver=driver)
        print("Database cluster started at %s" % _url)
        db = Database(_url)
        yield db
        db.cleanup()
    finally:
        log.info(f"Stopping container {image_tag}...")
        container.stop()


def build_postgres_command(pg_config):
    if not pg_config:
        return None
    cmd = ["postgres"]
    for k, v in pg_config.items():
        cmd += ["-c", f"{k}={v}"]
    return cmd
