# Macrostrat Python Libraries - Agent Guide

## Repository Overview

This is a monorepo containing **6 interconnected Python packages** within the `macrostrat` namespace. All are published to PyPI and can be consumed independently. The primary orchestration point is the shared test infrastructure and a centralized `conftest.py` that manages Docker-based testing.

### Package Directory

| Package | Location | Purpose | Version |
|---------|----------|---------|---------|
| **macrostrat.database** | `/database` | SQLAlchemy wrapper for PostgreSQL/PostGIS; core DB operations | 4.3.0 |
| **macrostrat.dinosaur** | `/dinosaur` | Database schema migration, upgrade, and conformance testing | - |
| **macrostrat.app_frame** | `/app-frame` | Docker Compose management CLI framework for Dockerized apps | - |
| **macrostrat.auth_system** | `/auth-system` | Authentication (legacy JWT + emerging ORCID-based) | - |
| **macrostrat.utils** | `/utils` | Shared logging, shell commands, timers, exceptions | 1.3.3+ |
| **macrostrat.package_tools** | `/package-tools` | Package versioning and PyPI publishing for the monorepo | - |

---

## 1. Top-Level Architecture

### 1.1 Dependency Graph

```
macrostrat.database
  ├── depends on: macrostrat.utils, SQLAlchemy, GeoAlchemy2, psycopg
  └── used by: dinosaur, auth-system (indirectly), app-frame

macrostrat.dinosaur
  ├── depends on: macrostrat.database, macrostrat.utils, results (migra/dbdiff)
  └── used by: tests that verify schema evolution

macrostrat.app_frame
  ├── depends on: macrostrat.utils, Typer, Docker, Rich
  └── no DB dependency; standalone CLI scaffolding

macrostrat.auth_system
  ├── depends on: macrostrat.database, SQLAlchemy ORM
  └── two subsystems: legacy (JWT-based) and core (ORCID-based)

macrostrat.utils
  ├── no external macrostrat dependencies
  └── provides: logging, shell execution, timers, error handling
```

### 1.2 Monorepo Layout

```
python-libraries/
├── README.md                  # Main repo overview
├── conftest.py                # Root pytest config (session-level fixtures)
├── database/                  # SQLAlchemy DB toolkit
│   ├── pyproject.toml
│   ├── macrostrat/database/
│   │   ├── __init__.py        # Main exports: Database, DatabaseMapper, utilities
│   │   ├── core.py            # Database class definition
│   │   ├── mapper/            # Automap, reflection, model building
│   │   ├── query.py           # run_sql, run_query, run_fixtures
│   │   ├── postgresql.py      # PostGSQL-specific (on_conflict, prefix_inserts)
│   │   ├── transfer/          # pg_dump/pg_restore utilities
│   │   └── utils.py           # Engine creation, database utilities
│   └── tests/
│       ├── test_database.py
│       ├── test_table_access.py
│       ├── test_temp_database.py
│       ├── test_database_fixtures.py
│       └── fixtures/
│           └── test-schema.sql, schema-dir/*.sql
├── dinosaur/                  # Schema migration and upgrade tools
│   ├── macrostrat/dinosaur/
│   │   ├── __init__.py        # AutoMigration, MigrationManager, create_migration()
│   │   ├── cluster.py         # database_cluster() context manager
│   │   └── upgrade_cluster/   # Cluster version upgrade utilities
│   └── tests/
│       ├── test_upgrade_cluster.py
│       └── fixtures/
├── app-frame/                 # Docker Compose CLI management
│   ├── macrostrat/app_frame/
│   │   ├── __init__.py        # Application, ControlCommand, Subsystem
│   │   ├── core.py            # Application class
│   │   ├── compose/           # DockerComposeManager
│   │   ├── control_command.py # CLI integration
│   │   ├── subsystems/        # Subsystem, SubsystemManager
│   │   └── utils/
│   └── test_app_frame.py      # Embedded in module
├── auth-system/               # Authentication system
│   ├── macrostrat/auth_system/
│   │   ├── core/              # ORCID-based auth (in development)
│   │   └── legacy/            # JWT-based auth (active, legacy)
│   │       └── test_auth.py
│   └── docs/
├── utils/                     # Shared utilities
│   ├── macrostrat/utils/
│   │   ├── __init__.py        # relative_path, working_directory, override_environment
│   │   ├── logs.py            # get_logger, setup_stderr_logs
│   │   ├── shell.py           # cmd(), split_args()
│   │   ├── timer.py           # CodeTimer
│   │   └── exc.py             # BaseError, ApplicationError
├── .changes/                  # Pending version changes (see section 8)
├── package-tools/            # Monorepo publication utilities
│   ├── macrostrat/package_tools/
│   │   ├── packages.py        # Package discovery and PyPI metadata
│   │   ├── changes.py         # Change fragments, version and changelog updates
│   │   ├── commands.py        # `mono changeset`, `mono version`
│   │   ├── status.py          # `mono status` release plan and checks
│   │   ├── publish.py         # Building and PyPI publishing
│   │   ├── install.py         # Dependency management
│   │   └── dependencies.py    # Backwards-compatible re-exports
├── docs/                      # MkDocs documentation
│   ├── index.md
│   └── macrostrat/
│       ├── database.md
│       └── app-frame.md
└── .github/                   # GitHub Actions workflows
```

---

## 2. Key Modules and Responsibilities

### 2.1 macrostrat.database

**Purpose**: Unified SQLAlchemy interface optimized for PostgreSQL, with automatic schema reflection and model generation.

#### Core Modules

##### `database/core.py` - Database Class
- **Class: `Database`** - Central wrapper around SQLAlchemy engine and session
  - **Constructor params**:
    - `db_conn`: Connection string, URL, or Engine
    - `echo_sql`: Enable SQL logging (default False)
    - `instance_params`: Parameters passed to queries
  - **Key methods**:
    - `.automap(schemas=["public"], **kwargs)` - Reflect database and auto-generate ORM models
    - `.session_scope(commit=True)` - Context manager for transactional scope
    - `.transaction(rollback="on-error")` - Fine-grained transaction control
    - `.savepoint(name=None, rollback="on-error")` - PostgreSQL savepoint (nested transaction support)
    - `.run_sql(fn, params=None, **kwargs)` - Execute SQL file or query string
    - `.run_query(sql, params=None, **kwargs)` - Single query with result iteration
    - `.run_fixtures(fixtures, params=None, **kwargs)` - Bulk fixture loading
    - `.get_table(name, schema=None)` - Reflect single table (with caching)
    - `.get_model(name, schema=None, automap=True)` - Get ORM model class (lazy automap)
    - `.get_or_create(model, **kwargs)` - ORM convenience method
    - `.refresh_schema(automap=None)` - Clear cached state and optionally re-automap
    - `.cleanup()` - Dispose engine and close session
  - **Properties**:
    - `.table` - Dict-like access to reflected SQLAlchemy Table objects
    - `.model` - Dict-like access to ORM model classes
    - `.inspector` - SQLAlchemy Inspector (lazily cached)
    - `.mapped_classes` - Alias for `.model`

**Important Pattern**: The Database class combines session management (via `scoped_session`) with lazy reflection. Models are not required at construction; they are generated on-demand when accessed via `.model` or `.automap()`.

##### `database/mapper/` - Automap and Model Generation
- **`mapper/__init__.py`**: `DatabaseMapper` class
  - Wraps SQLAlchemy's automap system with caching and relationship naming customization
  - **Methods**:
    - `.reflect_database(schemas=["public"], use_cache=True)` - Reflect multiple schemas
    - `.reflect_schema(schema, use_cache=True)` - Reflect a single schema
    - `.register_models(*models)` - Manually register ORM models
    - `.automap_view(table_name, *column_args)` - Reflect a view (must manually specify PK)
  - **Attributes**:
    - `._models` - ModelCollection (attribute access to ORM classes)
    - `._tables` - TableCollection (attribute access to Table objects)
    - `._reflected_schemas` - Set of schemas already reflected
    - `.automap_base` - SQLAlchemy automap base class

- **`mapper/base.py`**: `ModelHelperMixins`
  - Mixed into all automapped ORM models
  - **Methods**: `.to_dict()`, `.__repr__()` (shows primary key)

- **`mapper/utils.py`**: Model utilities
  - `primary_key(instance)` - Extract PK columns from model instance
  - `classname_for_table(table)` - Generate class name (handles schemas: "schema_table")
  - `name_for_scalar_relationship()` / `name_for_collection_relationship()` - Customize FK relationship names
  - `ModelCollection` - Dict-like wrapper for ORM classes with attr access
  - `TableCollection` - Dict-like wrapper for Table objects

- **`mapper/cache.py`**: `DatabaseModelCache`
  - Optional caching of reflected metadata to pickle files for faster startup
  - **Used by**: Tests and high-performance scenarios

##### `database/query.py` - Query Execution
- **Functions**:
  - `run_sql(session, filename_or_sql, params=None, **kwargs)` - Execute file/string; returns iterator
  - `run_query(session, sql, params=None, **kwargs)` - Single query; returns results
  - `run_fixtures(session, fixtures_dir_or_list, params=None, **kwargs)` - Load fixture files
  - `execute(session, sql, params=None)` - Low-level execution
  - `infer_is_sql_text(string)` - Distinguish SQL code from file paths
  - `canonicalize_query(file_or_text)` - Convert input to Path or str SQL
  - `get_sql_text(filename_or_query)` - Extract raw SQL text
  - `pretty_print(sql, **kwargs)` - Display query with optional summarization

**Important**: Parameters are interpolated using psycopg/SQLAlchemy parameter placeholders (`{name}` or `:name`).

##### `database/utils.py` - Database Utilities
- **Functions**:
  - `create_engine(db_conn, **kwargs)` - Wrapper around SQLAlchemy's `create_engine`
  - `create_database(url)` - Create a new database (requires `sqlalchemy_utils`)
  - `drop_database(url)` - Drop a database
  - `database_exists(url)` - Check if database exists
  - `get_dataframe(connectable, query, **kwargs)` - Return pandas DataFrame from query
  - `get_or_create(session, model, defaults=None, **kwargs)` - ORM get-or-create pattern
  - `temporary_database(url, *, drop=True, ensure_empty=False, template=None, force_drop=False)` - Context manager for temp DB
  - `template_database(url, template)` - Use an existing DB as template for new one
  - `get_database_url(url)` - Normalize connection string
  - `reflect_table(engine, name, schema=None, *column_args)` - Reflect a single table

##### `database/postgresql.py` - PostgreSQL-Specific
- **Functions/Classes**:
  - `on_conflict(target_cols, action, **kwargs)` - SQLAlchemy INSERT ... ON CONFLICT helper
  - `prefix_inserts(insert_clause, ...)` - Compiler for PostgreSQL INSERT syntax
  - `table_exists(engine, table, schema)` - Check if table exists
  - `upsert(session, table, values, constraint, **kwargs)` - Upsert operation
  - `OnConflictAction` - Enum (UPDATE, DO_NOTHING, etc.)

##### `database/transfer/` - Database Movement Tools
- **`dump_database.py`**: `pg_dump()`, `pg_dump_to_file()` - Dump schema/data
- **`restore_database.py`**: `pg_restore()`, `pg_restore_from_file()` - Restore from dump
- **`move_tables.py`**: `move_tables()` - Copy tables between databases
- **`stream_utils.py`**: Streaming helpers for large exports

#### Test Structure
- **Root fixture** (`/conftest.py`): 
  - `docker_client` - Session-scoped Docker client
  - `database_url` - PostgreSQL URL (from env var or Docker container, with optional teardown)
- **Package fixtures** (`database/tests/`):
  - `engine` - Creates temporary test database
  - `empty_db` - Database without schema
  - `db` - Database with test schema (sample, formation, unit, etc.)
  - `conn` - Connection with auto-rollback
- **Test fixtures** in `database/tests/fixtures/`:
  - `test-schema.sql` - Main test schema definition
  - `schema-dir/*.sql` - Modular fixture files (loaded in order)

---

### 2.2 macrostrat.dinosaur

**Purpose**: Database schema migration, validation, and PostgreSQL cluster upgrade utilities. Built on `results` library (dbdiff/migra) and testcontainers.

#### Core Modules

##### `dinosaur/__init__.py` - Migration System
- **Key Classes**:
  - `AutoMigration(results.Migration)` - Extends migra's Migration class
    - **Methods**:
      - `.apply(quiet=False, safe_only=False)` - Apply migration statements
      - `.is_safe` - Boolean; True if no data-destroying drops
      - `.unsafe_changes()` / `.safe_changes()` - Iterate through statements by safety
      - `.changes_omitting_views()` - Filter out view drops (safe operation)
      - `.print_changes()` - Display migration to stderr
  - `SchemaMigration` - Base class for custom per-schema migrations
    - Subclasses define: `.should_apply(source, target, migrator)` and `.apply(engine)`
  - `MigrationManager` - Orchestrates auto + custom migrations
    - **Constructor params**:
      - `database` - Database object
      - `_init_function` - Callable that sets up "ideal" schema
      - `migrations` - List of SchemaMigration subclasses
      - `schema` - Schema name to migrate (default: all)
    - **Methods**:
      - `.add_migration(MigrationClass)` - Register custom migration
      - `.add_module(module)` - Discover migrations in module
      - `.run_migration(dry_run=True, apply=True)` - Execute migration pipeline
      - `.dry_run_migration(target)` - Test migration on clone
      - `.apply_migrations(engine, target)` - Apply custom migrations
    - **Attributes**:
      - `target_url` - PostgreSQL URL for test database (configurable)
      - `dry_run_url` - URL for schema clone
      - `postgres_image_name` - Docker image for clone

- **Key Functions**:
  - `create_migration(database, initializer, target_url, safe, **kwargs)` - Generate migration
  - `needs_migration(database, initializer)` - Check if schema is current
  - `db_migration(database, initializer, safe, apply, hide_view_changes)` - User-facing migration call
  - `dump_schema(engine, image_name=None)` - Dump schema via `pg_dump`
  - `dump_schema_containerized(container, dbname)` - Dump from running container
  - `create_schema_clone(engine, db_url, image_name)` - Clone schema for testing
  - `has_table(engine, table)` / `has_column(engine, table, column)` - Schema introspection
  - `update_schema(db, initializer, migrations=[], **kwargs)` - Top-level schema update

##### `dinosaur/cluster.py` - Database Cluster Management
- **Function**: `database_cluster(image_tag, *, build=False, context=Path, optimize_for_testing=False, data_volume=str, user=str, database=str, driver=str, environment=dict, config=dict|Path, in_memory=True, docker_client=DockerClient, **kwargs)`
  - **Returns**: Context manager yielding `Database` object
  - **Behavior**:
    - Starts PostgreSQL container via testcontainers.PostgresContainer
    - Supports custom config files (mounted), environment variables, data volumes
    - If `in_memory=True`: uses tmpfs for faster testing
    - If `optimize_for_testing=True`: disables fsync, WAL, checkpoints for speed
    - Automatically pulls or builds image as needed
  - **Cleanup**: Stops container on context exit; Database object is yielded for immediate use

##### `dinosaur/upgrade_cluster/` - Cluster Upgrade Tools
- **`describe.py`**: `check_database_cluster_version()` - Determine current PostgreSQL/PostGIS versions
- **`utils.py`**: 
  - `wait_for_cluster()` / `wait_for_ready()` - Polls for container readiness
  - `ensure_empty_docker_volume()` - Verify volume is empty before use

---

### 2.3 macrostrat.app_frame

**Purpose**: CLI scaffolding and Docker Compose management for containerized applications.

#### Core Modules

##### `app_frame/core.py` - Application Class
- **Class: `Application`** - Main entry point
  - **Constructor params**:
    - `name` - Application display name
    - `command_name` - CLI command name (defaults to name.lower())
    - `project_prefix` - Docker Compose project name prefix
    - `log_modules` - Modules to enable logging for
    - `env` - Environment variables (dict or callable)
    - `load_dotenv` - Load .env file (bool, Path, or list of Paths)
    - `env_prefix` - Environment variable prefix (e.g., "APP_")
  - **Key methods**:
    - `.load_dotenv(dependency=None)` - Load environment from file(s)
    - `.setup_environment(env)` - Register environment variables
    - `.info(text, style=None)` - Print styled info message
    - `.control_command()` - Return Typer CLI app
  - **Attributes**:
    - `.console` - Rich Console for output
    - `.envvar_prefix` - Prefix for env vars
    - `.name`, `.command_name`, `.project_prefix` - Names

##### `app_frame/compose/` - Docker Compose Integration
- **`base.py`**: `DockerComposeManager`
  - Wraps `docker-compose` CLI operations
  - **Methods**: Start, stop, restart services; follow logs
- **`follow_logs.py`**: Log streaming from containers

##### `app_frame/subsystems/` - Extensibility
- **`defs.py`**: `Subsystem`, `SubsystemManager`
  - Allows modular service definitions
  - Each subsystem has health checks, start/stop hooks

##### `app_frame/control_command.py` - CLI Integration
- **Class: `ControlCommand`** - Typer-based CLI wrapper
- **Class: `CommandBase`** - Base for custom commands

---

### 2.4 macrostrat.auth_system

**Purpose**: User authentication and authorization for Macrostrat services.

#### Core Modules

##### `auth_system/legacy/` - JWT-Based Auth (Active)
- **`backend.py`**: JWT token generation/validation
- **`api.py`**: FastAPI/Starlette integration
- **`identity.py`**: User representation
- **`context.py`**: Request context with user info
- **`test_auth.py`**: Test suite

##### `auth_system/core/` - ORCID-Based Auth (In Development)
- **`main.py`**: Core auth logic
- **`model.py`**: ORM models for users, sessions
- **`database.py`**: Database layer
- **`schema.py`**: Pydantic schemas

---

### 2.5 macrostrat.utils

**Purpose**: Cross-cutting utilities for logging, CLI integration, and error handling.

#### Key Modules

##### `utils/__init__.py`
- `relative_path(base, *parts)` - Path utility
- `working_directory(path)` - Context manager for cwd
- `override_environment(**kwargs)` - Temp env override

##### `utils/logs.py`
- `get_logger(name)` - Get Python logger with Macrostrat defaults
- `setup_stderr_logs(modules=None)` - Configure stderr logging for specified modules

##### `utils/shell.py`
- `cmd(*args, capture_output=False, env=None, **kwargs)` - Execute shell command
- `split_args(string)` - Parse shell argument string

##### `utils/timer.py`
- `CodeTimer` - Context manager for timing code blocks

##### `utils/exc.py`
- `BaseError` - Base exception class
- `ApplicationError` - High-level application errors

---

## 3. Test Infrastructure

### 3.1 Conftest Hierarchy

**Location**: `/conftest.py` (root)

**Session-Level Fixtures** (used by all packages):
```python
@fixture(scope="session")
def docker_client():
    """Docker client from environment."""
    return DockerClient.from_env()

@fixture(scope="session")
def database_url(docker_client):
    """PostgreSQL connection URL.
    
    Priority:
    1. Use TESTING_DATABASE env var if accessible
    2. Spin up PostGIS container via testcontainers
    3. Raise error if no option available
    """
    # Returns str URL like "postgresql://..."
```

**Custom Options**:
```python
parser.addoption(
    "--teardown",
    action="store_true",
    default=False,
    help="Tear down database after tests run"
)
```

### 3.2 Database Test Fixtures

**Location**: defined in `database/tests/test_database.py` (not a conftest; available
only within that file and files that import them explicitly).

```python
@fixture(scope="session")
def engine(database_url, pytestconfig):
    """SQLAlchemy engine bound to a temporary test database."""
    with temporary_database(database_url, drop=pytestconfig.option.teardown,
                            force_drop=True) as engine:
        yield engine

@fixture(scope="session")
def empty_db(engine):
    """Database instance with no schema loaded; cleaned up at session end."""
    db = Database(engine.url)
    yield db
    db.cleanup()  # catches OperationalError gracefully if server already gone

@fixture(scope="session")
def db(empty_db):
    """Database with the full test schema applied (sample, geology.formation, etc.)."""
    for sqlfile in Path("fixtures").glob("*.sql"):
        run_sql(empty_db.engine, sqlfile)
    yield empty_db

@fixture(scope="function")
def conn(db):
    """Connection with automatic rollback after each test."""
    with db.session.connection() as conn:
        yield conn
        db.session.rollback()
```

**Note**: Other test files (e.g. `test_table_access.py`) define their own module-scoped
`db` fixtures that connect to the same `database_url` server, applying the schema
idempotently with `CREATE … IF NOT EXISTS`.

### 3.3 Running Tests

```bash
# Run all tests (also aliased as `make test`)
uv run pytest

# Run only database tests
uv run pytest database/tests

# Run with verbose output
uv run pytest -vv database/tests

# Run a specific test
uv run pytest database/tests/test_database.py::test_database

# Clean up the test database on teardown
uv run pytest --teardown
```

**Important**: Tests require Docker to be running. Some tests depend on external containers (PostGIS, PostgreSQL upgrade scenarios).

### 3.4 Test Database Patterns

**Pattern 1: Session-Scoped DB with Rollback**
```python
@fixture(scope="function")
def fresh_sample(db):
    """Insert sample, rollback after test."""
    db.session.execute(insert(db.table.sample).values(...))
    db.session.commit()
    yield db.table.sample
    db.session.rollback()
```

**Pattern 2: Transaction Rollback**
```python
def test_something(db):
    with db.transaction(rollback=True):
        # All operations are rolled back after context
        yield db
```

**Pattern 3: Savepoint (Nested)**
```python
def test_nested(db):
    with db.savepoint() as sp:
        db.session.execute(...)
        # Rolls back to savepoint on error
```

---

## 4. Notable Patterns and Key Concepts

### 4.1 Database Class Usage Pattern

```python
from macrostrat.database import Database

# 1. Create database connection
db = Database("postgresql://user:pass@localhost/mydb")

# 2. Auto-reflect schema (generates ORM models)
db.automap(schemas=["public", "geology"])

# 3. Access models via attribute or dict-like access
Sample = db.model.sample
Formation = db.model.geology_formation

# 4. Query via ORM
from sqlalchemy import select
query = select(Sample).where(Sample.c.name == "test")
result = db.session.execute(query).fetchall()

# 5. Or use convenience methods
with db.session_scope(commit=True) as session:
    sample = Sample(name="test")
    session.add(sample)
    # Auto-commits on context exit

# 6. Run raw SQL with parameters
results = db.run_query("SELECT * FROM sample WHERE id = {id}", {"id": 1})

# 7. Load fixtures
db.run_fixtures(Path("fixtures/") / "seed-data.sql")

# 8. Cleanup
db.cleanup()
```

### 4.2 How SQLAlchemy Automap Works in This Codebase

**Flow**:
1. User calls `db.automap(schemas=["public"])`
2. `Database` creates a `DatabaseMapper` instance
3. `DatabaseMapper.reflect_database()` calls `BaseModel.prepare(autoload_with=engine, schema=None)`
4. SQLAlchemy's automap:
   - Reflects all tables in the schema from database metadata
   - Inspects primary keys, foreign keys, constraints
   - Generates ORM model classes inheriting from `ModelHelperMixins` + `automap_base`
   - Names scalar relationships with `name_for_scalar_relationship()` (e.g., `_formation`)
   - Names collection relationships with `name_for_collection_relationship()` (e.g., `formation_collection`)
5. Models stored in `ModelCollection` and `TableCollection` for attribute/dict access
6. Models are cached in `mapper._models` and `mapper._tables`

**Key Customizations**:
- Models inherit `ModelHelperMixins` for `.to_dict()` and smart `.__repr__()`
- Relationship naming prefixed with underscore (e.g., `unit._formation`) to avoid conflicts
- Schema-qualified names: `"geology_formation"` for `geology.formation` table
- Views cannot be automapped (no PK); must use `mapper.automap_view()` with manual PK specification

### 4.3 Testcontainers Integration Pattern

**Used in**: Root `conftest.py`, `dinosaur/cluster.py`, `dinosaur/tests/`

```python
from testcontainers.postgres import PostgresContainer

# Pattern 1: Direct testcontainers usage
container = PostgresContainer("postgis/postgis:14-3.3")
container.with_envs(POSTGRES_HOST_AUTH_METHOD="trust")
container.with_kwargs(tmpfs={"/var/lib/postgresql/data": "uid=999,gid=999"})
container.start()
url = container.get_connection_url()
db = Database(url)
# ... use db ...
container.stop()

# Pattern 2: Via database_cluster context manager
from macrostrat.dinosaur import database_cluster
with database_cluster("postgis/postgis:14-3.3", in_memory=True) as db:
    # db is Database object; container cleaned up on exit
    db.automap()
    # ...
```

**Advantages**:
- Isolated test environment per test or session
- In-memory tmpfs for speed
- Custom PostgreSQL configs via environment or mounted files
- Automatic image pulling/building

### 4.4 Migration Pattern (dinosaur)

**Scenario**: Schema changes between app versions

```python
from macrostrat.dinosaur import MigrationManager, SchemaMigration

# Define "ideal" schema (what the app expects)
def init_schema(db: Database):
    db.run_sql("CREATE TABLE IF NOT EXISTS users ...")

# Define custom per-version migrations (if automatic detection fails)
class Migration_v1_to_v2(SchemaMigration):
    name = "v1_to_v2_rename_columns"
    
    def should_apply(self, source, target, migrator):
        # Check if source schema needs this migration
        insp = get_inspector(source)
        return "old_column_name" in [c.name for c in insp.get_columns("users")]
    
    def apply(self, engine):
        run_sql(engine, "ALTER TABLE users RENAME old_column_name TO new_column_name")

# Orchestrate
manager = MigrationManager(
    db,
    initializer=init_schema,
    migrations=[Migration_v1_to_v2],
    schema="public"
)

# Dry-run first (creates clone, tests migration)
manager.run_migration(dry_run=True, apply=False)

# Then apply to real DB
manager.run_migration(dry_run=False, apply=True)
```

**How it works**:
1. Create temp "target" database with ideal schema (via initializer)
2. Compare current DB to target DB using `results.dbdiff.Migration`
3. Identify unsafe changes (data drops); raise error if any
4. Execute safe statements (adds, alters, view drops)
5. Apply custom `SchemaMigration` subclasses as hooks

### 4.5 Query Parameter Interpolation

**Supported Styles**:
```python
# Named parameters (SQLAlchemy style)
db.run_query(
    "SELECT * FROM sample WHERE id = {id}",
    {"id": 42}
)

# Positional placeholders (psycopg style)
db.run_query(
    "SELECT * FROM sample WHERE id = %s",
    [42]
)

# SQL Identifier (table/column names via psycopg.sql)
from psycopg.sql import Identifier
db.run_query(
    "SELECT * FROM {table}",
    {"table": Identifier("sample")}
)
```

---

## 5. Documentation Files

### Existing Documentation

| File | Purpose |
|------|---------|
| `/README.md` | Main repo overview; development setup, testing, releasing |
| `/docs/index.md` | MkDocs landing page |
| `/docs/macrostrat/database.md` | Database module auto-generated docs (references `Database` class) |
| `/docs/macrostrat/app-frame.md` | App-frame overview and basic usage |
| `/app-frame/README.md` | App-frame quick reference |
| `/auth-system/README.md` | Auth system overview; legacy vs. core |
| `/auth-system/docs/Version 1.md` | Auth system v1 design notes |
| `/database/CHANGELOG.md` | Version history and breaking changes |
| `/dinosaur/CHANGELOG.md` | Dinosaur version history |
| `/auth-system/CHANGELOG.md` | Auth system version history |
| `/package-tools/README.md` | Package tools for monorepo publishing |

### Key Points for Agents

- **AGENTS.md** (this file) is the primary reference for agent onboarding
- **MkDocs setup** in `/docs/` can be extended with agent-specific workflows
- **README.md** provides development setup; agents should ensure compliance
- **CHANGELOG.md** files track breaking changes across versions

---

## 6. Architecture Decision Records (ADRs)

### 6.1 Namespace Package Structure

The `macrostrat` namespace is split across directories as a **namespace package** (no `__init__.py` in `macrostrat/` root; each subpackage has its own `__init__.py`).

**Benefit**: Allows independent installation of `macrostrat.database`, `macrostrat.utils`, etc., while maintaining unified namespace.

**Build System**: Uses `uv_build` (uv's build backend) with `namespace = true` in `pyproject.toml`.

### 6.2 Scoped Sessions vs. Explicit Context Managers

`Database` uses SQLAlchemy's `scoped_session` for thread-local session management, **but** also provides `.session_scope()` context manager for explicit control.

**Pattern**: 
- Use `.session_scope()` for transactional safety in tests
- Use `with db.transaction(rollback=True)` for nested rollback
- Use `with db.savepoint()` for PostgreSQL-specific nested transactions (supports nesting)

### 6.3 Lazy Automap

Models are generated on-demand (when `.model` is accessed), not at `Database` construction time.

**Benefit**: Faster startup; only reflect needed schemas.

**Tradeoff**: First access to `.model` incurs reflection cost; subsequent accesses are cached.

### 6.4 Migration Safety

The dinosaur module defines "safe" as "no table data is destroyed." View drops, index drops, and constraint additions are safe. Table drops, column deletions, and type changes are unsafe.

**Rationale**: Views are reconstructible; data is not.

---

## 7. Common Agent Tasks and Patterns

### Task: Add a New Feature to Database Module
1. Add function to `/database/macrostrat/database/{module}.py`
2. Export in `/database/macrostrat/database/__init__.py`
3. Add test in `/database/tests/test_{feature}.py` using `db` fixture
4. Run `uv run pytest database/tests` to validate
5. Record the version change: `uv run mono changeset` (see section 8). Do not
   edit versions or CHANGELOG.md by hand.

### Task: Run Migration on Test Database
1. Define `init_schema()` function (sets up ideal schema)
2. Create `MigrationManager(db, init_schema)`
3. Call `.run_migration(dry_run=True, apply=False)` to test
4. Call `.run_migration(dry_run=False, apply=True)` to apply

### Task: Access Database Models
```python
db = Database("postgresql://...")
db.automap(schemas=["public"])
Sample = db.model.sample  # ORM class
sample_table = db.table.sample  # SQLAlchemy Table
```

### Task: Test with Docker Container
```python
from macrostrat.dinosaur import database_cluster

with database_cluster("postgis/postgis:14-3.3", in_memory=True) as db:
    db.automap()
    # ... test code ...
```

### Task: Write Fixture
```python
@fixture(scope="function")
def my_fixture(db):
    # Setup
    db.session.execute(insert(...))
    yield db
    # Teardown (implicit via db.session.rollback() in conftest)
```

---

## 8. Quick Reference: Key APIs

### Database Class
- **Constructor**: `Database(url, echo_sql=False, instance_params={}, **engine_kwargs)`
- **Reflection**: `.automap(schemas=["public"])`, `.refresh_schema(automap=True)`
- **Tables/Models**: `.table`, `.model`, `.get_table()`, `.get_model()`
- **Queries**: `.run_sql()`, `.run_query()`, `.run_fixtures()`, `.session.execute()`
- **Transactions**: `.session_scope()`, `.transaction()`, `.savepoint()`
- **Cleanup**: `.cleanup()`

### Query Module
- **Functions**: `run_sql()`, `run_query()`, `run_fixtures()`, `execute()`
- **Utilities**: `infer_is_sql_text()`, `canonicalize_query()`, `get_sql_text()`

### Dinosaur Module
- **Migration**: `MigrationManager`, `SchemaMigration`, `create_migration()`
- **Utilities**: `database_cluster()`, `dump_schema()`, `has_table()`, `has_column()`

### Utils Module
- **Logging**: `get_logger()`, `setup_stderr_logs()`
- **Shell**: `cmd()`, `split_args()`
- **Context**: `working_directory()`, `override_environment()`
- **Paths**: `relative_path()`
- **Timing**: `CodeTimer`

---

## 9. Troubleshooting

### Issue: "No ORM model found for schema.table"
- **Cause**: Schema not reflected, or table doesn't exist
- **Fix**: Call `db.automap(schemas=["schema_name"])` first; verify table exists in DB

### Issue: Fixture not found in database tests
- **Cause**: Fixture file path incorrect or file doesn't exist
- **Fix**: Check `/database/tests/fixtures/` directory; verify paths in test code

### Issue: Docker container fails to start
- **Cause**: Image not found, port conflict, or Docker daemon not running
- **Fix**: Check `docker ps`, verify image name, ensure Docker is running

### Issue: Test flakes or hangs
- **Cause**: Database connection timeout, async operation not awaited, or savepoint conflict
- **Fix**: Increase timeouts, check for async/await mismatches, use `.savepoint()` instead of nested `.transaction()`

### Issue: Migration detects unsafe changes
- **Cause**: Schema change would destroy data (e.g., column drop, type narrowing)
- **Fix**: Write custom `SchemaMigration` class to handle the change safely, or manually edit data before running automatic migration

---

## 10. Related Systems and Dependencies

### External Packages (Key)
- **SQLAlchemy** (>=2.0) - Core ORM and query builder
- **GeoAlchemy2** (>=0.15) - PostGIS types for SQLAlchemy
- **psycopg** (>=3.2) - PostgreSQL driver (pure Python)
- **testcontainers** - Docker container management for tests
- **Docker Python SDK** - Direct Docker API access
- **results** (dbdiff, schemainspect) - Database schema diffing
- **Typer** - CLI framework (for app-frame)
- **Rich** - Terminal formatting and logging

### Related Repositories
- [Sparrow](https://github.com/EarthCubeGeochron/Sparrow) - Primary consumer of these libraries
- [Macrostrat Web Components](https://github.com/UW-Macrostrat/web-components) - Sibling monorepo (JavaScript)

---

**Last Updated**: June 2026  
**Status**: Comprehensive; ready for agent onboarding


---

## 8. Releases

Releases are driven by **change fragments**, the same way `web-components` uses
changesets. The version decision is made in the pull request that makes the
change, and merging to `main` publishes.

### In the pull request

Record what changed and how far each package should move:

```bash
uv run mono changeset
```

This writes a small markdown file in `.changes/` naming the affected packages
and their change levels (`patch`, `minor`, `major`), with the changelog entry.
**Commit it alongside the change.** Do not hand-edit `version` fields in
`pyproject.toml` or write `CHANGELOG.md` entries directly.

`mono status` runs as a required check on every pull request. It prints the
release plan and fails if a version moved without a changelog entry, or with a
stale `uv.lock`.

### Making the release

When the pending fragments should become a release, apply them:

```bash
uv run mono version
```

This raises each affected package's version, cascades to packages that depend
on it within the monorepo (raising their dependency floors and giving them a
patch — configured by `tool.mono.update-internal-dependencies` in the root
`pyproject.toml`), writes each `CHANGELOG.md` entry, refreshes lock files, and
deletes the fragments it applied. Review the diff, then commit and merge.

### Publishing

`.github/workflows/release.yml` runs on push to `main` and publishes every
package whose version is not yet on PyPI, then pushes the version tags. It uses
**PyPI trusted publishing** through the `pypi` GitHub environment — there is no
API token anywhere, and approving that environment is the release gate.

Publishing locally is a fallback, not the normal path. `mono publish` builds,
uploads and tags; it does not commit or regenerate lock files. It needs
`UV_PUBLISH_TOKEN` set. Use `--dry-run` to see what it would publish.

### Conventions

- A package with no classifiers, or with the `Private :: Do Not Upload`
  classifier, is never published.
- CHANGELOG entries are terse one-line bullets; detail belongs in the
  feature documentation.
- A version on PyPI can never be reused. Recovery from a bad release is a new
  patch version, never a retry of the same one.
