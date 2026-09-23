import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from re import search
from sys import stderr
from typing import Callable, Any, IO, Union
from warnings import warn

import psycopg2.sql as psql2
import sqlparse
from click import secho
from psycopg.errors import QueryCanceled
from psycopg.sql import SQL, Composable, Composed
from rich.console import Console
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import (
    IntegrityError,
    InternalError,
    InvalidRequestError,
    OperationalError,
    ProgrammingError,
)
from sqlalchemy.sql.elements import TextClause

from macrostrat.database.compat import (
    update_legacy_identifier,
)
from macrostrat.database.progress import ActivityIndicator, get_console
from macrostrat.utils import get_logger

log = get_logger(__name__)


def infer_is_sql_text(_string: str) -> bool:
    """
    Return True if the string is a valid SQL query,
    false if it should be interpreted as a file path.
    """
    # If it's a byte string, decode it
    if isinstance(_string, bytes):
        _string = _string.decode("utf-8")

    lines = _string.split("\n")
    if len(lines) > 1:
        return True
    return _starts_with_keyword(_string)


def canonicalize_query(file_or_text: Union[str, Path, IO]) -> Union[str, Path]:
    if isinstance(file_or_text, Path):
        return file_or_text
    # If it's a file-like object, read it
    if hasattr(file_or_text, "read"):
        return file_or_text.read()
    # Otherwise, assume it's a string
    if infer_is_sql_text(file_or_text):
        return file_or_text
    pth = Path(file_or_text)
    if pth.exists() and pth.is_file():
        return pth
    return file_or_text


def _starts_with_keyword(text: str) -> bool:
    t1 = text.strip().lower()
    for i in _sql_keywords:
        if t1.startswith(i.lower() + " "):
            return True
    return False


def pretty_print(sql, **kwargs):
    """Print and optionally summarize an SQL query"""
    summarize = kwargs.pop("summarize", True)
    if summarize:
        sql = summarize_statement(sql)
    secho(sql, **kwargs)


_sql_keywords = [
    "SELECT",
    "INSERT",
    "UPDATE",
    "CREATE",
    "DROP",
    "DELETE",
    "ALTER",
    "SET",
    "GRANT",
    "WITH",
    "NOTIFY",
    "COPY",
]


def summarize_statement(sql):
    for line in sql.split("\n"):
        if _starts_with_keyword(line):
            return line.split("(")[0].strip().rstrip(";").replace(" AS", "")
    return sql.strip().split("\n")[0].strip().rstrip(";")


def get_sql_text(sql, interpret_as_file=None, echo_file_name=True):
    if interpret_as_file:
        sql = Path(sql).read_text()
    elif interpret_as_file is None:
        sql = canonicalize_query(sql)

    if isinstance(sql, Path):
        if echo_file_name:
            secho(sql.name, fg="cyan", bold=True)
        sql = sql.read_text()

    return sql


def _get_queries(sql, *, interpret_as_file=None, format=False):
    if isinstance(sql, (list, tuple)):
        queries = []
        for i in sql:
            queries.extend(_get_queries(i, interpret_as_file=interpret_as_file))
        return queries
    if isinstance(sql, (SQL, psql2.SQL, TextClause)):
        return [sql]

    if sql in [None, ""]:
        return
    if interpret_as_file:
        sql = Path(sql).read_text()
    elif interpret_as_file is None:
        sql = canonicalize_query(sql)

    if isinstance(sql, Path):
        sql = sql.read_text()

    # We're now dealing with a single string

    # Quickly check for comments to see if we must strip them
    if _has_comments(sql) or format:
        sql = sqlparse.format(sql, strip_comments=True)

    sql = sql.strip()
    # If the last character is a semicolon, remove it
    if sql.endswith(";"):
        sql = sql[:-1]

    if ";" not in sql:
        # Fast path, only semicolons can be used to separate queries
        return [sql]

    # Do the expensive SQL split operation
    return sqlparse.split(sql)


def _is_prebind_param(param):
    return isinstance(param, (Composable, psql2.Composable))


def _has_comments(sql):
    """Fast pre-check to see if a query has comments.
    This saves us from formatting the query and doing a regex search"""
    for line in sql.split("\n"):
        line = line.strip()
        if line.startswith("--") or line.startswith("/*"):
            return True
    return False


def _split_params(params):
    if params is None:
        return None, None
    new_params = []
    new_bind_params = []
    if isinstance(params, (list, tuple)):
        for i in params:
            if _is_prebind_param(i):
                new_bind_params.append(update_legacy_identifier(i))
            else:
                new_params.append(i)
    elif isinstance(params, dict):
        new_params = {}
        new_bind_params = {}
        for k, v in params.items():
            if _is_prebind_param(v):
                new_bind_params[k] = update_legacy_identifier(v)
            else:
                new_params[k] = v
    if len(new_bind_params) == 0:
        new_bind_params = None
    return new_params, new_bind_params


def _get_cursor(connectable):
    if isinstance(connectable, Engine):
        conn = connectable.connect()

    # Find a connection or cursor object for the connectable
    conn = connectable
    if hasattr(conn, "raw_connection"):
        conn = conn.raw_connection()
    while hasattr(conn, "driver_connection") or hasattr(conn, "connection"):
        if hasattr(conn, "driver_connection"):
            conn = conn.driver_connection
        elif conn.connection == conn:
            break
        else:
            conn = conn.connection
        if callable(conn):
            conn = conn()
    if hasattr(conn, "cursor"):
        conn = conn.cursor()

    return conn


def _get_connection(connectable) -> Connection:
    if isinstance(connectable, Engine):
        return connectable.connect()
    if isinstance(connectable, Connection):
        return connectable
    if not hasattr(connectable, "connection"):
        return connectable
    conn = connectable.connection
    if callable(conn):
        return conn()
    return conn


def _render_query(query: Union[SQL, Composed], connectable: Union[Engine, Connection]):
    """Render a query to a SQL string."""
    if not isinstance(query, (Composed, SQL)):
        return query
    # Find a connection or cursor object for the connectable
    conn = _get_cursor(connectable)
    return query.as_string(conn)


def infer_has_server_binds(sql) -> bool:
    if "%s" in sql:
        return True
    res = search(r"%\(\w+\)s", sql)
    return res is not None


_default_statement_filter = lambda sql_text, params: True


class OutputMode(Enum):
    NONE = "none"
    ERRORS = "errors"
    SUMMARY = "summary"
    ALL = "all"


def _normalize_output_args(kwargs):
    output_mode = kwargs.pop("output_mode", OutputMode.SUMMARY)
    output_file = kwargs.pop("output_file", stderr)

    if not isinstance(output_mode, OutputMode):
        output_mode = OutputMode(output_mode)

    if output_mode == OutputMode.NONE:
        output_file = open(os.devnull, "w")
    return output_mode, output_file


@dataclass
class StatementDirective:
    """What a hook asks the loop to *do*: run ``query`` (or ``skip`` it).

    Returned by ``transform_statement``/``on_error`` *before* the statement runs,
    so it is an instruction, not an outcome.
    """

    query: Any
    params: Any = None
    skip: bool = False
    label: str | None = None

    @classmethod
    def skipped(cls, query=None, params=None) -> "StatementDirective":
        return cls(query=query, params=params, skip=True)


# Deprecated alias — renamed from the misleading "Result" (it is returned before
# the statement runs). Kept for backwards compatibility.
StatementResult = StatementDirective


@dataclass
class StatementContext:
    """What a hook is told: the statement currently being processed."""

    index: int
    query: Any
    params: Any
    sql_text: str


TransformFn = Callable[[StatementContext], list[StatementDirective] | None]
Connectable = Union[Engine, Connection]

# Called when a statement raises. It may return a list of StatementDirectives to
# execute as recovery (e.g. drop-and-recreate, then re-run) — in which case the
# original error is suppressed — or None to let normal error handling proceed.
RecoveryFn = Callable[
    [StatementContext, Exception, Connectable], "list[StatementDirective] | None"
]


def _statement_filter_to_transform(statement_filter) -> TransformFn:
    def transform(ctx: StatementContext) -> list[StatementDirective] | None:
        if not statement_filter(ctx.sql_text, ctx.params):
            return [StatementDirective.skipped(query=ctx.query, params=ctx.params)]
        return None

    return transform


def _run_sql(
    connectable,
    sql,
    params=None,
    *,
    print_skipped=True,
    use_transaction=True,
    **kwargs,
):
    """
    Internal function for running a query on a SQLAlchemy connectable,
    which always returns an iterator. The wrapper function adds the option
    to return a list of results.
    """
    if isinstance(connectable, Engine):
        with connectable.connect() as conn:
            yield from _run_sql(conn, sql, params, **kwargs)
            return

    stop_on_error = kwargs.pop("stop_on_error", False)
    raise_errors = kwargs.pop("raise_errors", False)
    ensure_single_query = kwargs.pop("ensure_single_query", False)
    output_mode, output_file = _normalize_output_args(kwargs)
    console = kwargs.pop("console", None)
    if console is None:
        console = get_console(output_file)
    has_server_binds = kwargs.pop("has_server_binds", None)

    statement_filter = kwargs.pop("statement_filter", None)
    transform_statement: TransformFn | None = kwargs.pop("transform_statement", None)
    on_error: RecoveryFn | None = kwargs.pop("on_error", None)

    if stop_on_error:
        raise_errors = True
        warn(DeprecationWarning("stop_on_error is deprecated, use raise_errors"))

    if statement_filter is not None:
        warn(
            DeprecationWarning(
                "statement_filter is deprecated, use transform_statement"
            )
        )
        if transform_statement is not None:
            raise ValueError(
                "Cannot specify both statement_filter and transform_statement"
            )
        transform_statement = _statement_filter_to_transform(statement_filter)

    interpret_as_file = kwargs.pop("interpret_as_file", None)
    queries = _get_queries(sql, interpret_as_file=interpret_as_file)

    if queries is None:
        return

    if ensure_single_query and len(queries) > 1:
        raise ValueError("Multiple queries passed when only one was expected")

    if not isinstance(params, list) or not len(params) == len(queries):
        params = [params] * len(queries)

    for index, (query, _params) in enumerate(zip(queries, params)):
        _query, sql_text, rest_params = _render_query_text(connectable, query, _params)
        if sql_text == "":
            continue

        ctx = StatementContext(
            index=index, query=query, params=_params, sql_text=sql_text
        )

        results = transform_statement(ctx) if transform_statement is not None else None

        if results is None:
            results = [StatementDirective(query=query, params=_params)]

        for result in results:
            yield from _execute_one(
                connectable,
                result,
                output_file,
                raise_errors=raise_errors,
                output_mode=output_mode,
                print_skipped=print_skipped,
                has_server_binds=has_server_binds,
                use_transaction=use_transaction,
                on_error=on_error,
                context=ctx,
                console=console,
            )


def _render_query_text(connectable, query, params):
    params, pre_bind_params = _split_params(params)
    if isinstance(query, (psql2.SQL, psql2.Composed)):
        query = update_legacy_identifier(query)

    if isinstance(query, str):
        # Escape postgresql cast parameters after SQLAlchemy binds
        # (e.g., :param::text)
        query = escape_postgresql_cast_parameters(query)

    if pre_bind_params is not None:
        if not isinstance(query, SQL):
            query = SQL(query)
        # Pre-bind the parameters using psycopg
        query = query.format(**pre_bind_params)

    if isinstance(query, (SQL, Composed)):
        query = _render_query(query, connectable)

    sql_text = str(query)

    return query, sql_text, params


def escape_postgresql_cast_parameters(sql_text):
    regex = r":([\w]+)::([a-zA-Z]+)"
    for res in re.findall(regex, sql_text):
        param, cast_type = res
        sql_text = sql_text.replace(
            f":{param}::{cast_type}", ":" + param + r"\:\:" + cast_type
        )
    return sql_text


def _execute_one(
    connectable,
    result: StatementDirective,
    output_file: IO,
    *,
    raise_errors: bool = True,
    output_mode: OutputMode = OutputMode.SUMMARY,
    has_server_binds: bool | None = None,
    print_skipped: bool = True,
    use_transaction: bool = True,
    on_error: "RecoveryFn | None" = None,
    context: "StatementContext | None" = None,
    console: "Console | None" = None,
    _recovering: bool = False,
):
    params = result.params

    query, sql_text, _params = _render_query_text(connectable, result.query, params)
    if has_server_binds is None:
        has_server_binds = infer_has_server_binds(sql_text)

    if result.label is not None:
        display_text = result.label
    elif output_mode == OutputMode.NONE:
        display_text = None
    elif output_mode != OutputMode.ALL:
        display_text = summarize_statement(str(query))
    else:
        display_text = str(query)

    if result.skip:
        if print_skipped and display_text is not None:
            secho(display_text, dim=True, strikethrough=True, file=output_file)
        return

    trans = None
    if use_transaction:
        try:
            trans = connectable.begin()
        except InvalidRequestError:
            pass

    # Print the statement before it runs, with an activity indicator if it blocks
    indicator = None
    if display_text is not None:
        if console is None:
            console = get_console(output_file)
        indicator = ActivityIndicator(display_text, console=console).start()

    try:
        log.debug("Executing SQL: \n %s", query)
        try:
            if has_server_binds:
                conn = _get_connection(connectable)
                res = conn.exec_driver_sql(query, _params)
            else:
                if not isinstance(query, TextClause):
                    query = text(query)
                res = connectable.execute(query, _params)
        except Exception:
            # Handled (and possibly recovered from) below
            if indicator is not None:
                indicator.clear()
            raise
        except BaseException:
            # E.g., a KeyboardInterrupt: show which statement was interrupted
            if indicator is not None:
                indicator.done(style="red")
            raise

        # The statement has finished executing; stop showing it as in progress
        # before handing the result back to the caller.
        if indicator is not None:
            indicator.done()

        yield res

        if trans is not None:
            trans.commit()
        elif hasattr(connectable, "commit"):
            connectable.commit()

    except Exception as err:
        if trans is not None:
            trans.rollback()
        elif hasattr(connectable, "rollback"):
            connectable.rollback()

        # Give an error handler a chance to recover (e.g. drop-and-recreate).
        # Recovery statements run with on_error disabled to prevent loops.
        if on_error is not None and not _recovering and context is not None:
            recovery = on_error(context, err, connectable)
            if recovery is not None:
                for _stmt in recovery:
                    yield from _execute_one(
                        connectable,
                        _stmt,
                        output_file,
                        raise_errors=raise_errors,
                        output_mode=output_mode,
                        print_skipped=print_skipped,
                        use_transaction=use_transaction,
                        console=console,
                        _recovering=True,
                    )
                return

        # Whether the statement is already shown on the console
        statement_visible = indicator is not None and indicator.label_visible

        if raise_errors or _should_raise_query_error(err):
            if display_text is not None and not statement_visible:
                secho(display_text, fg="red", dim=True, file=output_file)
            raise err
        if display_text is not None:
            _print_error(
                display_text,
                err,
                file=output_file,
                show_statement=not statement_visible,
            )


def _should_raise_query_error(err):
    """Determine if an error should be raised for a query or not."""
    if not isinstance(
        err, (ProgrammingError, IntegrityError, InternalError, OperationalError)
    ):
        return True

    orig_err = getattr(err, "orig", None)
    if orig_err is None:
        return True

    # If we cancel statements midstream, we should raise the error.
    # We might want to change this behavior in the future, or support more graceful handling of errors from other
    # database backends.
    # Ideally we could handle operational errors more gracefully
    if (
        isinstance(orig_err, QueryCanceled)
        or getattr(orig_err, "pgcode", None) == "57014"
    ):
        return True

    return False


def _print_error(sql_text, err, **kwargs):
    if orig := getattr(err, "orig", None):
        _err = str(orig)
    else:
        _err = str(err)
    _err = _err.strip()
    # Decide whether error should be dimmed
    dim = kwargs.pop("dim", "already exists" in _err)
    if kwargs.pop("show_statement", True):
        secho(sql_text, fg=None if dim else "red", dim=True, **kwargs)
    if dim:
        _err = "  " + _err
    secho(_err, fg="red", dim=dim, **kwargs)
    log.error(err)


def run_sql_file(connectable, filename, params=None, **kwargs):
    return run_sql(connectable, filename, params, interpret_as_file=True, **kwargs)


def run_query(connectable, query, params=None, **kwargs):
    # Single queries are not printed unless an output mode is requested
    kwargs.setdefault("output_mode", OutputMode.NONE)
    return next(
        iter(
            _run_sql(
                connectable,
                query,
                params,
                ensure_single_query=True,
                yield_results=kwargs.pop("yield_results", False),
                raise_errors=kwargs.pop("raise_errors", True),
                **kwargs,
            )
        )
    )


def get_sql_files(
    fixtures: Union[Path, list[Path]], recursive=False, order_by_name=True
):
    files = []
    if isinstance(fixtures, Path):
        fixtures = [fixtures]
    for fixture in fixtures:
        files.extend(_get_sql_files(fixture, recursive))
    if order_by_name:
        files = sorted(files)
    return files


def _get_sql_files(fixture: Path, recursive=False):
    if not fixture.exists():
        raise FileNotFoundError(f"Fixture {fixture} does not exist.")
    if fixture.is_file() and fixture.suffix == ".sql":
        return [fixture]
    _fn = "rglob" if recursive else "glob"
    files = getattr(fixture, _fn)("*.sql")
    return [r for r in files if r.is_file()]


def run_fixtures(connectable, fixtures: Union[Path, list[Path]], params=None, **kwargs):
    """
    Run a set of SQL fixture files on a database. Fixtures can be passed as a list of file paths or a directory.
    Fixtures are ordered by name by default, but this can be disabled.
    """
    recursive = kwargs.pop("recursive", False)
    order_by_name = kwargs.pop("order_by_name", True)
    output_mode, output_file = _normalize_output_args(kwargs)

    console = kwargs.pop("console", Console(stderr=True, file=output_file))
    files = get_sql_files(fixtures, recursive=recursive, order_by_name=order_by_name)

    prefix = os.path.commonpath(files)

    console.print(f"Running fixtures in [cyan bold]{prefix}[/]")
    for fixture in files:
        fn = fixture.relative_to(prefix)
        console.print(f"[cyan bold]{fn}[/]")
        run_sql_file(
            connectable,
            fixture,
            params,
            output_mode=output_mode,
            output_file=output_file,
            console=console,
            **kwargs,
        )
        console.print()


def run_sql(*args, **kwargs):
    """
    Run a query on a SQLAlchemy connectable.

    Parameters
    ----------
    connectable : Union[Engine, Connection]
        A SQLAlchemy engine or connection object.
    sql : Union[str, Path, IO, SQL, Composed]
        A SQL query, or a file containing a SQL query.
    params : Union[dict, list, tuple]
        Parameters to bind to the query. If a list or tuple, the parameters
        will be bound to the query in order. If a dict, the parameters will
        be bound to the query by name.
    stop_on_error : bool
        If True, stop running queries if an error is encountered.
    raise_errors : bool
        If True, raise errors encountered while running queries.
    has_server_binds : bool
        Interpret the query to have server-side bind parameters (requiring execution
        with the backend driver). By default, this is inferred from the query string,
        but inference is not always reliable.
    interpret_as_file : bool
        If True, force interpreting the query as a file path.
    yield_results : bool
        If True, yield the results of the query as they are executed, rather than
        returning a list after completion.
    ensure_single_query : bool
        If True, raise an error if multiple queries are passed when only one is expected.
    statement_filter : Callable
        A function that takes a SQL statement and parameters and returns True if the statement
        should be run, and False if it should be skipped.
    transform_statement: TransformFn | None
        A function that takes a StatementContext and returns a list of StatementDirective
        objects, which can modify the query, parameters, and whether the statement
        should be skipped or not. This allows for more complex logic than a simple
        statement filter.
    use_transaction: bool
        Whether to run the query in a transaction block
    """
    res = _run_sql(*args, **kwargs)
    if kwargs.pop("yield_results", False):
        return res
    return list(res)


def execute(connectable, sql, params=None, stop_on_error=False, **kwargs):
    output_file = kwargs.pop("output_file", None)
    output_mode = kwargs.pop("output_mode", None)
    sql = format(sql, strip_comments=True).strip()
    if sql == "":
        return
    try:
        connectable.begin()
        res = connectable.execute(text(sql), params=params)
        if hasattr(connectable, "commit"):
            connectable.commit()
        pretty_print(sql, dim=True, file=output_file, mode=output_mode)
        return res
    except (ProgrammingError, IntegrityError) as err:
        if hasattr(connectable, "rollback"):
            connectable.rollback()
        _print_error(sql, dim=True, file=output_file, mode=output_mode)
        if stop_on_error:
            return
    finally:
        if hasattr(connectable, "close"):
            connectable.close()
