import os
import sys
import warnings
from contextlib import contextmanager, redirect_stdout
from typing import Callable

import docker
from macrostrat.database import Database
from macrostrat.database.utils import connection_args, run_sql, temp_database
from macrostrat.utils import cmd, get_logger
from results.dbdiff import Migration
from results.dbdiff.statements import check_for_drop
from results.schemainspect import get_inspector
from rich import print

from .cluster import database_cluster
from .upgrade_cluster.utils import wait_for_cluster, wait_for_ready

log = get_logger(__name__)

DatabaseInitializer = Callable[[Database], None]


def _superseded(name: str):
    warnings.warn(
        f"macrostrat.dinosaur.{name} is deprecated; use macrostrat.schema_management"
        " (SchemaApp, MigrationRunner) instead",
        DeprecationWarning,
        stacklevel=3,
    )


class AutoMigration(Migration):
    def changes_omitting_views(self):
        nsel_drops = self.changes.non_table_selectable_drops()
        nsel_creations = self.changes.non_table_selectable_creations()
        # Warning: this also may omit changes to functions etc. We need to test this.
        for stmt in self.statements:
            if stmt in nsel_drops or stmt in nsel_creations:
                continue
            yield stmt

    def _exec(self, sql, quiet=False):
        """Execute SQL unsafely on an sqlalchemy Engine"""
        run_sql(self.s_from, sql)

    def apply(self, quiet=False, safe_only=False):
        statements = list(self.statements)
        if safe_only:
            statements = list(self.safe_changes())
        n = len(statements)
        log.debug(f"Applying migration with {n} operations")
        for stmt in statements:
            self._exec(stmt, quiet=quiet)
        self.changes.i_from = get_inspector(
            self.s_from, schema=self.schema, exclude_schema=self.exclude_schema
        )

        safety_on = self.statements.safe
        self.clear()
        self.set_safety(safety_on)

    @property
    def is_safe(self):
        """We have a looser definition of safety than core Migra; ours involves not
        destroying data.
        Dropping 'non-table' items (such as views) is OK to do without checking with
        the user. Usually, these views are just dropped and recreated anyway when dependent
        tables change."""
        # We could try to apply 'non-table-selectable drops' first and then check again...
        unsafe = any(check_for_drop(s) for s in self.changes_omitting_views())
        return not unsafe

    def unsafe_changes(self):
        for stmt in self.changes_omitting_views():
            if check_for_drop(stmt):
                yield stmt

    def safe_changes(self):
        nsel_drops = self.changes.non_table_selectable_drops()
        nsel_creations = self.changes.non_table_selectable_creations()
        for stmt in self.statements:
            if stmt in nsel_drops or stmt in nsel_creations:
                # View drops are OK.
                yield stmt
            if check_for_drop(stmt):
                continue
            yield stmt

    def print_changes(self):
        statements = "\n".join(self.statements)
        print(statements, file=sys.stderr)


def _create_migration(db_engine, target, safe=True, **kwargs):
    # For some reason we need to patch this...
    log.info("Creating an automatic migration")

    migration = AutoMigration(db_engine.connect(), target.connect(), **kwargs)

    migration.set_safety(safe)
    # Not sure what this does
    migration.add_all_changes()
    return migration


@contextmanager
def _target_db(
    url: str, initializer: DatabaseInitializer, quiet: bool = False, redirect=sys.stderr
):
    if quiet:
        redirect = open(os.devnull, "w")

    log.debug("Creating migration target")
    with temp_database(url) as engine:
        database = Database(engine)
        with redirect_stdout(redirect):
            initializer(database)
        yield engine


def create_migration(
    database: Database,
    initializer: DatabaseInitializer,
    target_url: str = "postgresql://postgres@db:5432/sparrow_temp_migration",
    safe: bool = True,
    redirect=sys.stderr,
    **kwargs,
):
    _superseded("create_migration")
    with (
        _target_db(target_url, initializer, redirect=redirect) as target,
        redirect_stdout(redirect),
    ):
        return _create_migration(database.engine, target, safe=safe, **kwargs)


def needs_migration(database: Database, initializer: DatabaseInitializer):
    migration = create_migration(database, initializer)
    return len(migration.statements) == 0


def db_migration(
    database: Database,
    initializer: DatabaseInitializer,
    safe=True,
    apply=False,
    hide_view_changes=False,
):
    """Create a database migration against the idealized schema"""
    m = create_migration(database, initializer, safe=safe, redirect=sys.stderr)
    stmts = m.statements
    if hide_view_changes:
        stmts = m.changes_omitting_views()
    print("===MIGRATION BELOW THIS LINE===", file=sys.stderr)
    for stmt in stmts:
        if apply:
            run_sql(database.session, stmt)
        else:
            print(stmt, file=sys.stdout)


def dump_schema(engine, image_name=None) -> str:
    flags, dbname = connection_args(engine)
    flags_array = [f for f in flags.split(" ") if len(f) > 0]
    args = ("pg_dump", "--schema-only", *flags_array, dbname)
    env_vars = dict(PGPASSWORD=engine.url.password)
    if image_name is None:
        # Run pg_dump locally
        res = cmd(*args, capture_output=True, env=env_vars)
        return res.stdout.decode("utf-8")
    else:
        client = docker.from_env()
        res = client.containers.run(
            image_name, args, network_mode="host", remove=True, environment=env_vars
        )
        return res.decode("utf-8")


def dump_schema_containerized(container, dbname) -> str:
    res = container.exec_run(
        "pg_dump --schema-only -U postgres -h localhost", dbname, stdout=True
    )
    return res.output.decode("utf-8")


@contextmanager
def create_schema_clone(
    engine, db_url="postgresql://postgres@db:5432/sparrow_schema_clone", image_name=None
):
    schema = dump_schema(engine, image_name=image_name)
    with temp_database(db_url) as clone_engine:
        # Not sure why we have to mess with this, but we do
        wait_for_ready(clone_engine)
        log.info(schema)

        list(run_sql(clone_engine, schema, interpret_as_file=False, raise_errors=True))
        # Sometimes, we still have some differences, annoyingly
        # m = _create_migration(clone_engine, engine, safe=False)
        # m.apply(quiet=True, safe_only=True)
        yield clone_engine


def has_table(engine, table):
    insp = get_inspector(engine)
    return table in insp.tables


def has_column(engine, table, column):
    insp = get_inspector(engine)
    if table not in insp.tables:
        return False
    tbl = insp.tables[table]
    for col in tbl.columns:
        if col == column:
            return True
    return False


class SchemaMigrationError(Exception):
    pass


class SchemaMigration:
    name = None

    def should_apply(self, source, target, migrator):
        return False

    def apply(self, engine):
        pass


class MigrationManager:
    target_url = "postgresql://postgres@db:5432/sparrow_temp_migration"
    dry_run_url = "postgresql://postgres@db:5432/sparrow_schema_clone"
    postgres_image_name = "postgres:15"
    schema = None

    def __init__(self, database, _init_function, migrations=None, schema=None):
        _superseded("MigrationManager")
        self.db = database

        self._init_function = _init_function
        self._migrations = migrations or []
        self.schema = schema

    def add_migration(self, migration):
        assert issubclass(migration, SchemaMigration)
        self._migrations.append(migration())

    def add_module(self, module):
        for _, obj in module.__dict__.items():
            try:
                assert issubclass(obj, SchemaMigration)
            except (TypeError, AssertionError):
                continue
            if obj is SchemaMigration:
                continue
            self.add_migration(obj)

    def apply_migrations(self, engine, target):
        """This is the magic function where an ordered changeset gets
        generated and applied"""
        migrations = [
            m for m in self._migrations if m.should_apply(engine, target, self)
        ]
        log.info("Applying manual migrations")
        if len(migrations) == 0:
            log.info("Found no migrations to apply")
        while len(migrations) > 0:
            n = len(migrations)
            log.info(f"Found {n} migrations to apply")
            for m in migrations:
                log.info(f"Applying manual migration {m.name}")
                m.apply(engine)
                # We have applied this migration and should not do it again.
                migrations.remove(m)
            migrations = [m for m in migrations if m.should_apply(engine, target, self)]

    def _pre_auto_migration(self, engine, target):
        """This is a hook for subclasses to do things before the automatic migration"""
        pass

    def _run_migration(self, engine, target, check=False):
        try:
            # First, try an automatic migration
            m = _create_migration(engine, target, schema=self.schema)
            if len(m.statements) == 0:
                log.info("No automatic migration necessary")
                return

            if m.is_safe:
                log.info("Applying automatic migration")
                m.apply(quiet=True)
                return
        except Exception as exc:
            log.warning(f"Automatic migration failed: {exc}")

        self.apply_migrations(engine, target)

        log.info("Running scripts before automatic migrations")
        self._pre_auto_migration(engine, target)

        # Migrating to the new version should now be possible using a "safe" automatic migration
        m = _create_migration(engine, target)

        try:
            assert m.is_safe
        except AssertionError as err:
            print("[bold red]Manual migration needed!")
            print("Run [bold cyan]sparrow db migration[/bold cyan] to see the changes")
            print("[bold red]Unsafe changes:[/bold red]")
            for s in m.unsafe_changes():
                print(s, file=sys.stderr)
            raise err

        m.apply(quiet=True)
        # Re-add changes (this is time-consuming)
        # m.add_all_changes()
        # assert len(m.statements) == 0

    def dry_run_migration(self, target):
        log.info("Running dry-run migration")
        with create_schema_clone(
            self.db.engine, db_url=self.dry_run_url, image_name=self.postgres_image_name
        ) as src:
            self._run_migration(src, target)
        log.info("Migration dry run successful")

    def run_migration(self, dry_run=True, apply=True):
        log.info("Setting up target database")
        with _target_db(self.target_url, self._init_function) as target:
            if dry_run:
                self.dry_run_migration(target)
            if not apply:
                return
            log.info("Running migration")
            self._run_migration(self.db.engine, target)
            log.info("Finished running migration")


def update_schema(db: Database, initializer, migrations=[], **kwargs):
    # Might be worth creating an interactive upgrader
    migrator = MigrationManager(db, initializer, migrations=migrations)
    migrator.run_migration(**kwargs)
