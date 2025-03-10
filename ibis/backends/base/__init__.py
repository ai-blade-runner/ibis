from __future__ import annotations

import abc
import collections.abc
import functools
import importlib.metadata
import keyword
import re
import sys
import urllib.parse
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ClassVar,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
)

import ibis
import ibis.common.exceptions as exc
import ibis.config
import ibis.expr.operations as ops
import ibis.expr.types as ir
from ibis import util
from ibis.common.caching import RefCountedCache

if TYPE_CHECKING:
    import pandas as pd
    import pyarrow as pa

__all__ = ('BaseBackend', 'Database', 'connect')


class Database:
    """Generic Database class."""

    def __init__(self, name: str, client: Any) -> None:
        self.name = name
        self.client = client

    def __repr__(self) -> str:
        """Return type name and the name of the database."""
        return f'{type(self).__name__}({self.name!r})'

    def __dir__(self) -> list[str]:
        """Return the attributes and tables of the database.

        Returns
        -------
        list[str]
            A list of the attributes and tables available in the database.
        """
        attrs = dir(type(self))
        unqualified_tables = [self._unqualify(x) for x in self.tables]
        return sorted(frozenset(attrs + unqualified_tables))

    def __contains__(self, table: str) -> bool:
        """Check if the given table is available in the current database.

        Parameters
        ----------
        table
            Table name

        Returns
        -------
        bool
            True if the given table is available in the current database.
        """
        return table in self.tables

    @property
    def tables(self) -> list[str]:
        """Return a list with all available tables.

        Returns
        -------
        list[str]
            The list of tables in the database
        """
        return self.list_tables()

    def __getitem__(self, table: str) -> ir.Table:
        """Return a Table for the given table name.

        Parameters
        ----------
        table
            Table name

        Returns
        -------
        Table
            Table expression
        """
        return self.table(table)

    def __getattr__(self, table: str) -> ir.Table:
        """Return a Table for the given table name.

        Parameters
        ----------
        table
            Table name

        Returns
        -------
        Table
            Table expression
        """
        return self.table(table)

    def _qualify(self, value):
        return value

    def _unqualify(self, value):
        return value

    def drop(self, force: bool = False) -> None:
        """Drop the database.

        Parameters
        ----------
        force
            If `True`, drop any objects that exist, and do not fail if the
            database does not exist.
        """
        self.client.drop_database(self.name, force=force)

    def table(self, name: str) -> ir.Table:
        """Return a table expression referencing a table in this database.

        Parameters
        ----------
        name
            The name of a table

        Returns
        -------
        Table
            Table expression
        """
        qualified_name = self._qualify(name)
        return self.client.table(qualified_name, self.name)

    def list_tables(self, like=None, database=None):
        """List the tables in the database.

        Parameters
        ----------
        like
            A pattern to use for listing tables.
        database
            The database to perform the list against
        """
        return self.client.list_tables(like, database=database or self.name)


class TablesAccessor(collections.abc.Mapping):
    """A mapping-like object for accessing tables off a backend.

    Tables may be accessed by name using either index or attribute access:

    Examples
    --------
    >>> con = ibis.sqlite.connect("example.db")
    >>> people = con.tables['people']  # access via index
    >>> people = con.tables.people  # access via attribute
    """

    def __init__(self, backend: BaseBackend):
        self._backend = backend

    def __getitem__(self, name) -> ir.Table:
        try:
            return self._backend.table(name)
        except Exception as exc:  # noqa: BLE001
            raise KeyError(name) from exc

    def __getattr__(self, name) -> ir.Table:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._backend.table(name)
        except Exception as exc:  # noqa: BLE001
            raise AttributeError(name) from exc

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._backend.list_tables()))

    def __len__(self) -> int:
        return len(self._backend.list_tables())

    def __dir__(self) -> list[str]:
        o = set()
        o.update(dir(type(self)))
        o.update(
            name
            for name in self._backend.list_tables()
            if name.isidentifier() and not keyword.iskeyword(name)
        )
        return list(o)

    def __repr__(self) -> str:
        tables = self._backend.list_tables()
        rows = ["Tables", "------"]
        rows.extend(f"- {name}" for name in sorted(tables))
        return "\n".join(rows)

    def _ipython_key_completions_(self) -> list[str]:
        return self._backend.list_tables()


class _FileIOHandler:
    @staticmethod
    def _import_pyarrow():
        try:
            import pyarrow  # noqa: ICN001
        except ImportError:
            raise ModuleNotFoundError(
                "Exporting to arrow formats requires `pyarrow` but it is not installed"
            )
        else:
            return pyarrow

    @util.experimental
    def to_pyarrow(
        self,
        expr: ir.Expr,
        *,
        params: Mapping[ir.Scalar, Any] | None = None,
        limit: int | str | None = None,
        **kwargs: Any,
    ) -> pa.Table:
        """Execute expression and return results in as a pyarrow table.

        This method is eager and will execute the associated expression
        immediately.

        Parameters
        ----------
        expr
            Ibis expression to export to pyarrow
        params
            Mapping of scalar parameter expressions to value.
        limit
            An integer to effect a specific row limit. A value of `None` means
            "no limit". The default is in `ibis/config.py`.
        kwargs
            Keyword arguments

        Returns
        -------
        Table
            A pyarrow table holding the results of the executed expression.
        """
        pa = self._import_pyarrow()
        try:
            # Can't construct an array from record batches
            # so construct at one column table (if applicable)
            # then return the column _from_ the table
            table = pa.Table.from_batches(
                self.to_pyarrow_batches(expr, params=params, limit=limit, **kwargs)
            )
        except pa.lib.ArrowInvalid:
            raise
        except ValueError:
            # The pyarrow batches iterator is empty so pass in an empty
            # iterator and a pyarrow schema
            schema = expr.as_table().schema()
            table = pa.Table.from_batches([], schema=schema.to_pyarrow())

        if isinstance(expr, ir.Table):
            return table
        elif isinstance(expr, ir.Column):
            # Column will be a ChunkedArray, `combine_chunks` will
            # flatten it
            if len(table.columns[0]):
                return table.columns[0].combine_chunks()
            else:
                return pa.array(table.columns[0])
        elif isinstance(expr, ir.Scalar):
            return table.columns[0][0]
        else:
            raise ValueError

    @util.experimental
    def to_pyarrow_batches(
        self,
        expr: ir.Expr,
        *,
        params: Mapping[ir.Scalar, Any] | None = None,
        limit: int | str | None = None,
        chunk_size: int = 1_000_000,
        **kwargs: Any,
    ) -> pa.ipc.RecordBatchReader:
        """Execute expression and return a RecordBatchReader.

        This method is eager and will execute the associated expression
        immediately.

        Parameters
        ----------
        expr
            Ibis expression to export to pyarrow
        limit
            An integer to effect a specific row limit. A value of `None` means
            "no limit". The default is in `ibis/config.py`.
        params
            Mapping of scalar parameter expressions to value.
        chunk_size
            Maximum number of rows in each returned record batch.
        kwargs
            Keyword arguments

        Returns
        -------
        results
            RecordBatchReader
        """
        raise NotImplementedError

    def read_parquet(
        self, path: str | Path, table_name: str | None = None, **kwargs: Any
    ) -> ir.Table:
        """Register a parquet file as a table in the current backend.

        Parameters
        ----------
        path
            The data source.
        table_name
            An optional name to use for the created table. This defaults to
            a sequentially generated name.
        **kwargs
            Additional keyword arguments passed to the backend loading function.

        Returns
        -------
        ir.Table
            The just-registered table
        """
        raise NotImplementedError(
            f"{self.name} does not support direct registration of parquet data."
        )

    def read_csv(
        self, path: str | Path, table_name: str | None = None, **kwargs: Any
    ) -> ir.Table:
        """Register a CSV file as a table in the current backend.

        Parameters
        ----------
        path
            The data source. A string or Path to the CSV file.
        table_name
            An optional name to use for the created table. This defaults to
            a sequentially generated name.
        **kwargs
            Additional keyword arguments passed to the backend loading function.

        Returns
        -------
        ir.Table
            The just-registered table
        """
        raise NotImplementedError(
            f"{self.name} does not support direct registration of CSV data."
        )

    @util.experimental
    def to_parquet(
        self,
        expr: ir.Table,
        path: str | Path,
        *,
        params: Mapping[ir.Scalar, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Write the results of executing the given expression to a parquet file.

        This method is eager and will execute the associated expression
        immediately.

        Parameters
        ----------
        expr
            The ibis expression to execute and persist to parquet.
        path
            The data source. A string or Path to the parquet file.
        params
            Mapping of scalar parameter expressions to value.
        **kwargs
            Additional keyword arguments passed to pyarrow.parquet.ParquetWriter

        https://arrow.apache.org/docs/python/generated/pyarrow.parquet.ParquetWriter.html
        """
        self._import_pyarrow()
        import pyarrow.parquet as pq

        batch_reader = expr.to_pyarrow_batches(params=params)

        with pq.ParquetWriter(path, batch_reader.schema) as writer:
            for batch in batch_reader:
                writer.write_batch(batch)

    @util.experimental
    def to_csv(
        self,
        expr: ir.Table,
        path: str | Path,
        *,
        params: Mapping[ir.Scalar, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Write the results of executing the given expression to a CSV file.

        This method is eager and will execute the associated expression
        immediately.

        Parameters
        ----------
        expr
            The ibis expression to execute and persist to CSV.
        path
            The data source. A string or Path to the CSV file.
        params
            Mapping of scalar parameter expressions to value.
        kwargs
            Additional keyword arguments passed to pyarrow.csv.CSVWriter

        https://arrow.apache.org/docs/python/generated/pyarrow.csv.CSVWriter.html
        """
        self._import_pyarrow()
        import pyarrow.csv as pcsv

        batch_reader = expr.to_pyarrow_batches(params=params)

        with pcsv.CSVWriter(path, batch_reader.schema) as writer:
            for batch in batch_reader:
                writer.write_batch(batch)


class BaseBackend(abc.ABC, _FileIOHandler):
    """Base backend class.

    All Ibis backends must subclass this class and implement all the
    required methods.
    """

    database_class = Database
    table_class: type[ops.DatabaseTable] = ops.DatabaseTable
    name: ClassVar[str]

    def __init__(self, *args, **kwargs):
        self._con_args: tuple[Any] = args
        self._con_kwargs: dict[str, Any] = kwargs
        # expression cache
        self._query_cache = RefCountedCache(
            populate=self._load_into_cache,
            lookup=lambda name: self.table(name).op(),
            finalize=self._clean_up_cached_table,
            generate_name=functools.partial(util.gen_name, "cache"),
            key=lambda expr: expr.op(),
        )

    def __getstate__(self):
        return dict(
            database_class=self.database_class,
            table_class=self.table_class,
            _con_args=self._con_args,
            _con_kwargs=self._con_kwargs,
        )

    def __rich_repr__(self):
        yield "name", self.name

    def __hash__(self):
        return hash(self.db_identity)

    def __eq__(self, other):
        return self.db_identity == other.db_identity

    @functools.cached_property
    def db_identity(self) -> str:
        """Return the identity of the database.

        Multiple connections to the same
        database will return the same value for `db_identity`.

        The default implementation assumes connection parameters uniquely
        specify the database.

        Returns
        -------
        Hashable
            Database identity
        """
        parts = [self.table_class.__name__]
        parts.extend(self._con_args)
        parts.extend(f'{k}={v}' for k, v in self._con_kwargs.items())
        return '_'.join(map(str, parts))

    def connect(self, *args, **kwargs) -> BaseBackend:
        """Connect to the database.

        Parameters
        ----------
        *args
            Mandatory connection parameters, see the docstring of `do_connect`
            for details.
        **kwargs
            Extra connection parameters, see the docstring of `do_connect` for
            details.

        Notes
        -----
        This creates a new backend instance with saved `args` and `kwargs`,
        then calls `reconnect` and finally returns the newly created and
        connected backend instance.

        Returns
        -------
        BaseBackend
            An instance of the backend
        """
        new_backend = self.__class__(*args, **kwargs)
        new_backend.reconnect()
        return new_backend

    def _from_url(self, url: str, **kwargs) -> BaseBackend:
        """Construct an ibis backend from a SQLAlchemy-conforming URL."""
        raise NotImplementedError(
            f"`_from_url` not implemented for the {self.name} backend"
        )

    @staticmethod
    def _convert_kwargs(kwargs: MutableMapping) -> None:
        """Manipulate keyword arguments to `.connect` method."""

    def reconnect(self) -> None:
        """Reconnect to the database already configured with connect."""
        self.do_connect(*self._con_args, **self._con_kwargs)

    def do_connect(self, *args, **kwargs) -> None:
        """Connect to database specified by `args` and `kwargs`."""

    @util.deprecated(instead='use equivalent methods in the backend')
    def database(self, name: str | None = None) -> Database:
        """Return a `Database` object for the `name` database.

        Parameters
        ----------
        name
            Name of the database to return the object for.

        Returns
        -------
        Database
            A database object for the specified database.
        """
        return self.database_class(name=name or self.current_database, client=self)

    @property
    @abc.abstractmethod
    def current_database(self) -> str | None:
        """Return the name of the current database.

        Backends that don't support different databases will return None.

        Returns
        -------
        str | None
            Name of the current database.
        """

    @abc.abstractmethod
    def list_databases(self, like: str = None) -> list[str]:
        """List existing databases in the current connection.

        Parameters
        ----------
        like
            A pattern in Python's regex format to filter returned database
            names.

        Returns
        -------
        list[str]
            The database names that exist in the current connection, that match
            the `like` pattern if provided.
        """

    @staticmethod
    def _filter_with_like(
        values: Iterable[str],
        like: str | None = None,
    ) -> list[str]:
        """Filter names with a `like` pattern (regex).

        The methods `list_databases` and `list_tables` accept a `like`
        argument, which filters the returned tables with tables that match the
        provided pattern.

        We provide this method in the base backend, so backends can use it
        instead of reinventing the wheel.

        Parameters
        ----------
        values
            Iterable of strings to filter
        like
            Pattern to use for filtering names

        Returns
        -------
        list[str]
            Names filtered by the `like` pattern.
        """
        if like is None:
            return list(values)

        pattern = re.compile(like)
        return sorted(filter(lambda t: pattern.findall(t), values))

    @abc.abstractmethod
    def list_tables(
        self, like: str | None = None, database: str | None = None
    ) -> list[str]:
        """Return the list of table names in the current database.

        For some backends, the tables may be files in a directory,
        or other equivalent entities in a SQL database.

        Parameters
        ----------
        like : str, optional
            A pattern in Python's regex format.
        database : str, optional
            The database to list tables of, if not the current one.

        Returns
        -------
        list[str]
            The list of the table names that match the pattern `like`.
        """

    @abc.abstractmethod
    def table(self, name: str, database: str | None = None) -> ir.Table:
        """Construct a table expression.

        Parameters
        ----------
        name
            Table name
        database
            Database name

        Returns
        -------
        Table
            Table expression
        """

    @functools.cached_property
    def tables(self):
        """An accessor for tables in the database.

        Tables may be accessed by name using either index or attribute access:

        Examples
        --------
        >>> con = ibis.sqlite.connect("example.db")
        >>> people = con.tables['people']  # access via index
        >>> people = con.tables.people  # access via attribute
        """
        return TablesAccessor(self)

    @property
    @abc.abstractmethod
    def version(self) -> str:
        """Return the version of the backend engine.

        For database servers, return the server version.

        For others such as SQLite and pandas return the version of the
        underlying library or application.

        Returns
        -------
        str
            The backend version
        """

    @classmethod
    def register_options(cls) -> None:
        """Register custom backend options."""
        options = ibis.config.options
        backend_name = cls.name
        try:
            backend_options = cls.Options()
        except AttributeError:
            pass
        else:
            try:
                setattr(options, backend_name, backend_options)
            except ValueError as e:
                raise exc.BackendConfigurationNotRegistered(backend_name) from e

    def compile(
        self,
        expr: ir.Expr,
        params: Mapping[ir.Expr, Any] | None = None,
    ) -> Any:
        """Compile an expression."""
        return self.compiler.to_sql(expr, params=params)

    def _to_sql(self, expr: ir.Expr, **kwargs) -> str:
        """Convert an expression to a SQL string.

        Called by `ibis.to_sql`/`ibis.show_sql`, gives the backend an
        opportunity to generate nicer SQL for human consumption.
        """
        raise NotImplementedError(f"Backend '{self.name}' backend doesn't support SQL")

    def execute(self, expr: ir.Expr) -> Any:
        """Execute an expression."""

    def add_operation(self, operation: ops.Node) -> Callable:
        """Add a translation function to the backend for a specific operation.

        Operations are defined in `ibis.expr.operations`, and a translation
        function receives the translator object and an expression as
        parameters, and returns a value depending on the backend. For example,
        in SQL backends, a NullLiteral operation could be translated to the
        string `"NULL"`.

        Examples
        --------
        >>> @ibis.sqlite.add_operation(ibis.expr.operations.NullLiteral)
        ... def _null_literal(translator, expression):
        ...     return 'NULL'
        """
        if not hasattr(self, 'compiler'):
            raise RuntimeError('Only SQL-based backends support `add_operation`')

        def decorator(translation_function: Callable) -> None:
            self.compiler.translator_class.add_operation(
                operation, translation_function
            )

        return decorator

    def create_database(self, name: str, force: bool = False) -> None:
        """Create a new database.

        Not all backends implement this method.

        Parameters
        ----------
        name
            Name of the new database.
        force
            If `False`, an exception is raised if the database already exists.
        """
        raise NotImplementedError(
            f'Backend "{self.name}" does not implement "create_database"'
        )

    @abc.abstractmethod
    def create_table(
        self,
        name: str,
        obj: pd.DataFrame | ir.Table | None = None,
        *,
        schema: ibis.Schema | None = None,
        database: str | None = None,
        temp: bool = False,
        overwrite: bool = False,
    ) -> ir.Table:
        """Create a new table.

        Parameters
        ----------
        name
            Name of the new table.
        obj
            An Ibis table expression or pandas table that will be used to
            extract the schema and the data of the new table. If not provided,
            `schema` must be given.
        schema
            The schema for the new table. Only one of `schema` or `obj` can be
            provided.
        database
            Name of the database where the table will be created, if not the
            default.
        temp
            Whether a table is temporary or not
        overwrite
            Whether to clobber existing data

        Returns
        -------
        Table
            The table that was created.
        """

    @abc.abstractmethod
    def drop_table(
        self,
        name: str,
        *,
        database: str | None = None,
        force: bool = False,
    ) -> None:
        """Drop a table.

        Parameters
        ----------
        name
            Name of the table to drop.
        database
            Name of the database where the table exists, if not the default.
        force
            If `False`, an exception is raised if the table does not exist.
        """
        raise NotImplementedError(
            f'Backend "{self.name}" does not implement "drop_table"'
        )

    @abc.abstractmethod
    def create_view(
        self,
        name: str,
        obj: ir.Table,
        *,
        database: str | None = None,
        overwrite: bool = False,
    ) -> ir.Table:
        """Create a new view from an expression.

        Parameters
        ----------
        name
            Name of the new view.
        obj
            An Ibis table expression that will be used to create the view.
        database
            Name of the database where the view will be created, if not
            provided the database's default is used.
        overwrite
            Whether to clobber an existing view with the same name

        Returns
        -------
        Table
            The view that was created.
        """

    @abc.abstractmethod
    def drop_view(
        self, name: str, *, database: str | None = None, force: bool = False
    ) -> None:
        """Drop a view.

        Parameters
        ----------
        name
            Name of the view to drop.
        database
            Name of the database where the view exists, if not the default.
        force
            If `False`, an exception is raised if the view does not exist.
        """

    @classmethod
    def has_operation(cls, operation: type[ops.Value]) -> bool:
        """Return whether the backend implements support for `operation`.

        Parameters
        ----------
        operation
            A class corresponding to an operation.

        Returns
        -------
        bool
            Whether the backend implements the operation.

        Examples
        --------
        >>> import ibis
        >>> import ibis.expr.operations as ops
        >>> ibis.sqlite.has_operation(ops.ArrayIndex)
        False
        >>> ibis.postgres.has_operation(ops.ArrayIndex)
        True
        """
        raise NotImplementedError(
            f"{cls.name} backend has not implemented `has_operation` API"
        )

    def _cached(self, expr: ir.Table):
        """Cache the provided expression.

        All subsequent operations on the returned expression will be performed on the cached data.

        Parameters
        ----------
        expr
            Table expression to cache

        Returns
        -------
        Expr
            Cached table

        """
        op = expr.op()
        if (result := self._query_cache.get(op)) is None:
            self._query_cache.store(expr)
            result = self._query_cache[op]
        return ir.CachedTable(result)

    def _release_cached(self, expr: ir.CachedTable) -> None:
        """Releases the provided cached expression.

        Parameters
        ----------
        expr
            Cached expression to release
        """
        del self._query_cache[expr.op()]

    def _load_into_cache(self, name, expr):
        raise NotImplementedError(self.name)

    def _clean_up_cached_table(self, op):
        raise NotImplementedError(self.name)


@functools.lru_cache(maxsize=None)
def _get_backend_names() -> frozenset[str]:
    """Return the set of known backend names.

    Notes
    -----
    This function returns a frozenset to prevent cache pollution.

    If a `set` is used, then any in-place modifications to the set
    are visible to every caller of this function.
    """

    if sys.version_info < (3, 10):
        entrypoints = importlib.metadata.entry_points()["ibis.backends"]
    else:
        entrypoints = importlib.metadata.entry_points(group="ibis.backends")
    return frozenset(ep.name for ep in entrypoints)


def connect(resource: Path | str, **kwargs: Any) -> BaseBackend:
    """Connect to `resource`, inferring the backend automatically.

    Parameters
    ----------
    resource
        A URL or path to the resource to be connected to.
    kwargs
        Backend specific keyword arguments

    Examples
    --------
    Connect to an in-memory duckdb database:
    >>> con = ibis.connect("duckdb://")

    Connect to an on-disk sqlite database:
    >>> con = ibis.connect("sqlite://relative/path/to/data.db")
    >>> con = ibis.connect("sqlite:///absolute/path/to/data.db")

    Connect to a postgres server:
    >>> con = ibis.connect("postgres://user:password@hostname:5432")
    """
    url = resource = str(resource)

    if re.match("[A-Za-z]:", url):
        # windows path with drive, treat it as a file
        url = f"file://{url}"

    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme or "file"

    orig_kwargs = kwargs.copy()
    kwargs = dict(urllib.parse.parse_qsl(parsed.query))

    if scheme == "file":
        path = parsed.netloc + parsed.path
        # Merge explicit kwargs with query string, explicit kwargs
        # taking precedence
        kwargs.update(orig_kwargs)
        if path.endswith(".duckdb"):
            return ibis.duckdb.connect(path, **kwargs)
        elif path.endswith((".sqlite", ".db")):
            return ibis.sqlite.connect(path, **kwargs)
        elif path.endswith((".parquet", ".csv", ".csv.gz")):
            # Load parquet/csv/csv.gz files with duckdb by default
            con = ibis.duckdb.connect(**kwargs)
            con.register(path)
            return con
        else:
            raise ValueError(f"Don't know how to connect to {resource!r}")

    if kwargs:
        # If there are kwargs (either explicit or from the query string),
        # re-add them to the parsed URL
        query = urllib.parse.urlencode(kwargs)
        parsed = parsed._replace(query=query)

    if scheme in ("postgres", "postgresql"):
        # Treat `postgres://` and `postgresql://` the same, just as postgres
        # does. We normalize to `postgresql` since that's what SQLAlchemy
        # accepts.
        scheme = "postgres"
        parsed = parsed._replace(scheme="postgresql")

    # Convert all arguments back to a single URL string
    url = parsed.geturl()
    if "://" not in url:
        # SQLAlchemy requires a `://`, while urllib may roundtrip
        # `duckdb://` to `duckdb:`. Here we re-add the missing `//`.
        url = url.replace(":", "://", 1)
    if scheme in ("duckdb", "sqlite", "pyspark"):
        # SQLAlchemy wants an extra slash for URLs where the path
        # maps to a relative/absolute location on the filesystem
        url = url.replace(":", ":/", 1)

    try:
        backend = getattr(ibis, scheme)
    except AttributeError:
        raise ValueError(f"Don't know how to connect to {resource!r}") from None

    return backend._from_url(url, **orig_kwargs)
