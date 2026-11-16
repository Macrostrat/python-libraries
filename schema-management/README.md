# Macrostrat schema management

Declarative schemas, schema diffs and condition-checked migrations for
PostgreSQL. This is the approach Macrostrat's database is managed with, without
Macrostrat's own configuration, so that other applications (Rockd, Mapboard,
Sparrow) can adopt it.

## The approach

A database's structure is written down as it should be, not as a history of
changes:

- **Chunks** (`SchemaDefinition`) are named, dependency-ordered pieces of schema.
  Each provides directories or files of idempotent SQL, or a function. A chunk
  may be limited to classes of environment (`local`, `development`, `staging`,
  `production`) and names the role that owns what it creates.
- **A plan** is the difference between the live database and the chunks built
  into a scratch database. Statements that could lose data are counted apart and
  skipped unless asked for.
- **Sync** re-applies what a diff cannot manage: views, functions, seed data,
  roles and grants.
- **Migrations** cover what neither can express, chiefly changes to data and
  renames. A migration states preconditions and postconditions, so whether it
  has applied is read from the database rather than from a ledger.

## Describing an application

An application builds a `SchemaApp` and hands it to `create_schema_cli`:

```python
from pathlib import Path

from macrostrat.database import Database
from macrostrat.schema_management import SchemaApp, SchemaDefinition
from macrostrat.schema_management.cli import create_schema_cli

here = Path(__file__).parent

app = SchemaApp(
    name="myapp",
    database=lambda: Database("postgresql://localhost/myapp"),
    chunks=[SchemaDefinition(name="core", provides=[here / "definitions"])],
    schema_dir=here,
    migrations_dir=here / "migrations",
    env_class="local",
)

cli = create_schema_cli(app)  # a Typer app: plan, review, apply, migrate, …
```

`create_schema_cli` also takes a function returning a `SchemaApp`, called when a
command runs, for applications whose configuration is chosen per invocation.

| Command | Does |
| --- | --- |
| `graph` | Lists chunks, owners and application order |
| `plan` | Writes `<environment>.plan.sql`, the diff against the declarative schema |
| `review` | Prints or edits the plan |
| `apply` | Applies the plan; `--unsafe` includes statements that drop data |
| `sync` | Re-applies views, procedures, seed data and permissions |
| `provision` | Applies the declarative schema's statements directly |
| `migrate` | Lists migrations; `--apply` applies those that can |

## What the application supplies

Nothing is read from global configuration. `SchemaApp` fields carry everything
specific to an application:

| Field | Purpose |
| --- | --- |
| `database` | Connects to the managed database |
| `chunks` | The chunks, or a function of the environment class |
| `schema_dir` | Where plans and the applied-changes log are written |
| `migrations_dir`, `migrations` | Migration modules to load, and classes given directly |
| `environment`, `env_class` | The environment's name, and where it sits from laptop to production |
| `owner` | Role that owns what is created; statements it is refused are re-run as the connecting role |
| `planning_database` | Yields an empty database to build the schema into (default: a PostGIS container) |
| `excluded_schemas` | Schemas a diff never compares |
| `prepare_diff` | Drops objects from a diff that cannot be compared meaningfully |
| `authorize` | Called before any write with a description and whether `--yes` was given; raises to refuse it |
| `after_change` | Called after the schema changes, e.g. `reload_postgrest_schema` |

## Migrations

```python
from macrostrat.schema_management import Migration, exists, has_columns


class CheckinRatings(Migration):
    name = "checkin-ratings"
    subsystem = "checkins"
    readiness_state = "ga"
    preconditions = [exists("public", "checkins")]
    postconditions = [has_columns("public", "checkins", "rating")]
```

By default a migration runs the `.sql` files beside the module that defines it.
`readiness_state` (`alpha`, `beta`, `ga`) decides where it may apply: `alpha`
in local and development environments, `beta` from staging, `ga` everywhere.

`MigrationRunner` can be used directly, without a `SchemaApp`, given a database
and a list of migration classes.

## Layout

The package follows the four parts of the approach, over a shared base:

| Subpackage | Contents | To extend |
| --- | --- | --- |
| `chunks` | `SchemaDefinition`, discovery from SQL frontmatter, ordered builds | A new kind of provider goes in `definition.py` |
| `plan` | Schema inspection, plan generation, unsafe-statement accounting | A new diff rule goes beside `statements.py` |
| `sync` | Re-applying views, procedures, seed data and grants; `--object` selection | A new kind of re-runnable object is a module beside `views.py`, on the driver in `sweep.py` |
| `migrations` | `Migration`, condition helpers, the runner, loading from a directory | A new condition helper goes in `conditions.py` |
| `database` | Applying DDL as an owning role, environment classes, throwaway clusters | |
| `testing` | A harness that builds a test database chunk by chunk | |

`app` (`SchemaApp`) and `cli` (the command group) sit above these. `database`
depends on nothing here; `chunks` on `database`; `plan`, `sync` and `migrations`
on `chunks`. The one link across is a migration's `sync_chunks`, which calls
`sync`. Everything an application normally needs is importable from the package
root.
