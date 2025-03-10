from __future__ import annotations

import importlib
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Mapping, MutableMapping

import pandas as pd

import ibis.common.exceptions as com
import ibis.config
import ibis.expr.operations as ops
import ibis.expr.schema as sch
import ibis.expr.types as ir
from ibis.backends.base import BaseBackend
from ibis.backends.pandas.client import (
    PandasDatabase,
    PandasTable,
    ibis_schema_to_pandas,
)

if TYPE_CHECKING:
    import pyarrow as pa


class BasePandasBackend(BaseBackend):
    """Base class for backends based on pandas."""

    name = "pandas"
    backend_table_type = pd.DataFrame

    class Options(ibis.config.Config):
        enable_trace: bool = False

    def do_connect(
        self,
        dictionary: MutableMapping[str, pd.DataFrame] | None = None,
    ) -> None:
        """Construct a client from a dictionary of pandas DataFrames.

        Parameters
        ----------
        dictionary
            An optional mapping of string table names to pandas DataFrames.

        Examples
        --------
        >>> import ibis
        >>> ibis.pandas.connect({"t": pd.DataFrame({"a": [1, 2, 3]})})
        <ibis.backends.pandas.Backend at 0x...>
        """
        # register dispatchers
        from ibis.backends.pandas import execution, udf  # noqa: F401

        self.dictionary = dictionary or {}
        self.schemas: MutableMapping[str, sch.Schema] = {}

    def from_dataframe(
        self,
        df: pd.DataFrame,
        name: str = 'df',
        client: BasePandasBackend | None = None,
    ) -> ir.Table:
        """Construct an ibis table from a pandas DataFrame.

        Parameters
        ----------
        df
            A pandas DataFrame
        name
            The name of the pandas DataFrame
        client
            Client dictionary will be mutated with the name of the DataFrame,
            if not provided a new client is created

        Returns
        -------
        Table
            A table expression
        """
        if client is None:
            return self.connect({name: df}).table(name)
        client.dictionary[name] = df
        return client.table(name)

    @property
    def version(self) -> str:
        return pd.__version__

    @property
    def current_database(self):
        raise NotImplementedError('pandas backend does not support databases')

    def list_databases(self, like=None):
        raise NotImplementedError('pandas backend does not support databases')

    def list_tables(self, like=None, database=None):
        return self._filter_with_like(list(self.dictionary.keys()), like)

    def table(self, name: str, schema: sch.Schema = None):
        df = self.dictionary[name]
        schema = sch.infer(df, schema=schema or self.schemas.get(name, None))
        return self.table_class(name, schema, self).to_expr()

    def database(self, name=None):
        return self.database_class(name, self)

    def get_schema(self, table_name, database=None):
        schemas = self.schemas
        try:
            schema = schemas[table_name]
        except KeyError:
            schemas[table_name] = schema = sch.infer(self.dictionary[table_name])
        return schema

    def compile(self, expr, *args, **kwargs):
        return expr

    def create_table(
        self,
        name: str,
        obj: pd.DataFrame | ir.Table | None = None,
        *,
        schema: sch.Schema | None = None,
        database: str | None = None,
        temp: bool | None = None,
        overwrite: bool = False,
    ) -> ir.Table:
        """Create a table."""
        if temp:
            com.IbisError(
                "Passing `temp=True` to the Pandas backend create_table method has no "
                "effect: all tables are in memory and temporary."
            )
        if database:
            com.IbisError(
                "Passing `database` to the Pandas backend create_table method has no "
                "effect: Pandas cannot set a database."
            )
        if obj is None and schema is None:
            raise com.IbisError("The schema or obj parameter is required")

        if obj is not None:
            if not self._supports_conversion(obj):
                raise com.BackendConversionError(
                    f"Unable to convert {obj.__class__} object "
                    f"to backend type: {self.__class__.backend_table_type}"
                )
            df = self._convert_object(obj)
        else:
            pandas_schema = self._convert_schema(schema)
            dtypes = dict(pandas_schema)
            df = self._from_pandas(pd.DataFrame(columns=dtypes.keys()).astype(dtypes))

        if name in self.dictionary and not overwrite:
            raise com.IbisError(f"Cannot overwrite existing table `{name}`")

        self.dictionary[name] = df

        if schema is not None:
            self.schemas[name] = schema
        return self.table(name)

    def create_view(
        self,
        name: str,
        obj: ir.Table,
        *,
        database: str | None = None,
        overwrite: bool = False,
    ) -> ir.Table:
        return self.create_table(
            name, obj=obj, temp=None, database=database, overwrite=overwrite
        )

    def drop_view(self, name: str, *, force: bool = False) -> None:
        self.drop_table(name, force=force)

    def drop_table(self, name: str, *, force: bool = False) -> None:
        if not force and name in self.dictionary:
            raise com.IbisError(
                "Cannot drop existing table. Call drop_table with force=True to drop existing table."
            )
        del self.dictionary[name]

    @classmethod
    def _supports_conversion(cls, obj: Any) -> bool:
        return True

    @staticmethod
    def _convert_schema(schema: sch.Schema):
        return ibis_schema_to_pandas(schema)

    @staticmethod
    def _from_pandas(df: pd.DataFrame) -> pd.DataFrame:
        return df

    @classmethod
    def _convert_object(cls, obj: Any) -> Any:
        return cls.backend_table_type(obj)

    @classmethod
    @lru_cache
    def _get_operations(cls):
        backend = f"ibis.backends.{cls.name}"

        execution = importlib.import_module(f"{backend}.execution")
        execute_node = execution.execute_node

        # import UDF to pick up AnalyticVectorizedUDF and others
        importlib.import_module(f"{backend}.udf")

        dispatch = importlib.import_module(f"{backend}.dispatch")
        pre_execute = dispatch.pre_execute

        return frozenset(
            op
            for op, *_ in execute_node.funcs.keys() | pre_execute.funcs.keys()
            if issubclass(op, ops.Value)
        )

    @classmethod
    def has_operation(cls, operation: type[ops.Value]) -> bool:
        # Pandas doesn't support geospatial ops, but the dispatcher implements
        # a common base class that makes it appear that it does. Explicitly
        # exclude these operations.
        if issubclass(operation, (ops.GeoSpatialUnOp, ops.GeoSpatialBinOp)):
            return False
        op_classes = cls._get_operations()
        return operation in op_classes or any(
            issubclass(operation, op_impl) for op_impl in op_classes
        )

    def _clean_up_cached_table(self, op):
        del self.dictionary[op.name]


class Backend(BasePandasBackend):
    name = 'pandas'
    database_class = PandasDatabase
    table_class = PandasTable

    def to_pyarrow(
        self,
        expr: ir.Expr,
        params: Mapping[ir.Scalar, Any] | None = None,
        limit: int | str | None = None,
        **kwargs: Any,
    ) -> pa.Table:
        pa = self._import_pyarrow()
        output = self.execute(expr, params=params, limit=limit)

        if isinstance(output, pd.DataFrame):
            return pa.Table.from_pandas(output)
        elif isinstance(output, pd.Series):
            return pa.Array.from_pandas(output)
        else:
            return pa.scalar(output)

    def to_pyarrow_batches(
        self,
        expr: ir.Expr,
        *,
        params: Mapping[ir.Scalar, Any] | None = None,
        limit: int | str | None = None,
        chunk_size: int = 1000000,
        **kwargs: Any,
    ) -> pa.ipc.RecordBatchReader:
        pa = self._import_pyarrow()
        pa_table = self.to_pyarrow(expr, params=params, limit=limit)
        return pa.RecordBatchReader.from_batches(
            pa_table.schema, pa_table.to_batches(max_chunksize=chunk_size)
        )

    def execute(self, query, params=None, limit='default', **kwargs):
        from ibis.backends.pandas.core import execute_and_reset

        if limit != 'default' and limit is not None:
            raise ValueError(
                'limit parameter to execute is not yet implemented in the '
                'pandas backend'
            )

        if not isinstance(query, ir.Expr):
            raise TypeError(
                "`query` has type {!r}, expected ibis.expr.types.Expr".format(
                    type(query).__name__
                )
            )

        node = query.op()

        if params is None:
            params = {}
        else:
            params = {
                k.op() if isinstance(k, ir.Expr) else k: v for k, v in params.items()
            }

        return execute_and_reset(node, params=params, **kwargs)

    def _load_into_cache(self, name, expr):
        self.create_table(name, expr.execute())
