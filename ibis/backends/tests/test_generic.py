import contextlib
import decimal
from operator import invert, methodcaller, neg

import numpy as np
import pandas as pd
import pytest
import sqlalchemy as sa
import toolz
from pytest import param

import ibis
import ibis.common.exceptions as com
import ibis.expr.datatypes as dt
import ibis.selectors as s
from ibis import _
from ibis import literal as L

try:
    import duckdb

    DuckDBConversionException = duckdb.ConversionException
except ImportError:
    DuckDBConversionException = None

try:
    import clickhouse_driver

    ClickhouseDriverOperationalError = clickhouse_driver.dbapi.errors.OperationalError
except ImportError:
    ClickhouseDriverOperationalError = None


NULL_BACKEND_TYPES = {
    'bigquery': "NULL",
    'clickhouse': 'Nullable(Nothing)',
    'datafusion': "NULL",
    'duckdb': "NULL",
    'impala': 'BOOLEAN',
    'snowflake': None,
    'sqlite': "null",
    'trino': 'unknown',
    "postgres": "null",
}


@pytest.mark.broken(["duckdb", "impala", "bigquery"], 'assert nan is None')
def test_null_literal(con, backend):
    expr = ibis.null()
    result = con.execute(expr)
    assert result is None

    with contextlib.suppress(com.OperationNotDefinedError):
        backend_name = backend.name()
        assert con.execute(expr.typeof()) == NULL_BACKEND_TYPES[backend_name]


BOOLEAN_BACKEND_TYPE = {
    'bigquery': "BOOL",
    'clickhouse': 'UInt8',
    'impala': 'BOOLEAN',
    'snowflake': "BOOLEAN",
    'sqlite': "integer",
    'trino': 'boolean',
    "duckdb": "BOOLEAN",
    "postgres": "boolean",
}


def test_boolean_literal(con, backend):
    expr = ibis.literal(False, type=dt.boolean)
    result = con.execute(expr)
    assert not result
    assert type(result) in (np.bool_, bool)

    with contextlib.suppress(com.OperationNotDefinedError):
        backend_name = backend.name()
        assert con.execute(expr.typeof()) == BOOLEAN_BACKEND_TYPE[backend_name]


@pytest.mark.parametrize(
    ('expr', 'expected'),
    [
        param(
            ibis.NA.fillna(5),
            5,
            marks=pytest.mark.notimpl(["mssql", "druid"]),
            id="na_fillna",
        ),
        param(
            L(5).fillna(10),
            5,
            marks=pytest.mark.notimpl(["mssql", "druid"]),
            id="non_na_fillna",
        ),
        param(L(5).nullif(5), None, id="nullif_null"),
        param(L(10).nullif(5), 10, id="nullif_not_null"),
    ],
)
@pytest.mark.notimpl(["datafusion"])
def test_scalar_fillna_nullif(con, expr, expected):
    if expected is None:
        # The exact kind of null value used differs per backend (and version).
        # Example 1: Pandas returns np.nan while BigQuery returns None.
        # Example 2: PySpark returns np.nan if pyspark==3.0.0, but returns None
        # if pyspark <=3.0.0.
        # TODO: Make this behavior consistent (#2365)
        assert pd.isna(con.execute(expr))
    else:
        assert con.execute(expr) == expected


@pytest.mark.parametrize(
    ("col", "filt"),
    [
        param(
            "nan_col",
            _.nan_col.isnan(),
            marks=pytest.mark.notimpl(["datafusion", "mysql", "sqlite"]),
            id="nan_col",
        ),
        param(
            "none_col",
            _.none_col.isnull(),
            marks=[pytest.mark.notimpl(["datafusion", "mysql"])],
            id="none_col",
        ),
    ],
)
@pytest.mark.notimpl(["mssql", "druid"])
def test_isna(backend, alltypes, col, filt):
    table = alltypes.select(
        nan_col=ibis.literal(np.nan), none_col=ibis.NA.cast("float64")
    )
    df = table.execute()

    result = table[filt].execute().reset_index(drop=True)
    expected = df[df[col].isna()].reset_index(drop=True)

    backend.assert_frame_equal(result, expected)


@pytest.mark.parametrize(
    "value",
    [
        None,
        param(
            np.nan,
            marks=pytest.mark.notimpl(
                [
                    "bigquery",
                    "clickhouse",
                    "duckdb",
                    "impala",
                    "postgres",
                    "mysql",
                    "snowflake",
                    "polars",
                    "trino",
                ],
                reason="NaN != NULL for these backends",
            ),
            id="nan_col",
        ),
    ],
)
@pytest.mark.notimpl(["datafusion", "mssql", "druid"])
def test_column_fillna(backend, alltypes, value):
    table = alltypes.mutate(missing=ibis.literal(value).cast("float64"))
    pd_table = table.execute()

    res = table.mutate(missing=table.missing.fillna(0.0)).execute()
    sol = pd_table.assign(missing=pd_table.missing.fillna(0.0))
    backend.assert_frame_equal(res.reset_index(drop=True), sol.reset_index(drop=True))


@pytest.mark.parametrize(
    ('expr', 'expected'),
    [
        param(ibis.coalesce(5, None, 4), 5, id="generic"),
        param(ibis.coalesce(ibis.NA, 4, ibis.NA), 4, id="null_start_end"),
        param(ibis.coalesce(ibis.NA, ibis.NA, 3.14), 3.14, id="non_null_last"),
    ],
)
@pytest.mark.notimpl(["datafusion"])
def test_coalesce(con, expr, expected):
    result = con.execute(expr.name("tmp"))

    if isinstance(result, decimal.Decimal):
        # in case of Impala the result is decimal
        # >>> decimal.Decimal('5.56') == 5.56
        # False
        assert result == decimal.Decimal(str(expected))
    else:
        assert result == pytest.approx(expected)


# TODO(dask) - identicalTo - #2553
@pytest.mark.notimpl(
    ["clickhouse", "datafusion", "polars", "dask", "pyspark", "mssql", "druid"]
)
def test_identical_to(backend, alltypes, sorted_df):
    sorted_alltypes = alltypes.order_by('id')
    df = sorted_df
    dt = df[['tinyint_col', 'double_col']]

    ident = sorted_alltypes.tinyint_col.identical_to(sorted_alltypes.double_col)
    expr = sorted_alltypes['id', ident.name('tmp')].order_by('id')
    result = expr.execute().tmp

    expected = (dt.tinyint_col.isnull() & dt.double_col.isnull()) | (
        dt.tinyint_col == dt.double_col
    )

    expected = backend.default_series_rename(expected)
    backend.assert_series_equal(result, expected)


@pytest.mark.parametrize(
    ('column', 'elements'),
    [
        ('int_col', [1, 2, 3]),
        ('int_col', (1, 2, 3)),
        ('string_col', ['1', '2', '3']),
        ('string_col', ('1', '2', '3')),
        ('int_col', {1}),
        ('int_col', frozenset({1})),
    ],
)
@pytest.mark.notimpl(["mssql", "druid"])
def test_isin(backend, alltypes, sorted_df, column, elements):
    sorted_alltypes = alltypes.order_by('id')
    expr = sorted_alltypes[
        'id', sorted_alltypes[column].isin(elements).name('tmp')
    ].order_by('id')
    result = expr.execute().tmp

    expected = sorted_df[column].isin(elements)
    expected = backend.default_series_rename(expected)
    backend.assert_series_equal(result, expected)


@pytest.mark.parametrize(
    ('column', 'elements'),
    [
        ('int_col', [1, 2, 3]),
        ('int_col', (1, 2, 3)),
        ('string_col', ['1', '2', '3']),
        ('string_col', ('1', '2', '3')),
        ('int_col', {1}),
        ('int_col', frozenset({1})),
    ],
)
@pytest.mark.notimpl(["mssql", "druid"])
def test_notin(backend, alltypes, sorted_df, column, elements):
    sorted_alltypes = alltypes.order_by('id')
    expr = sorted_alltypes[
        'id', sorted_alltypes[column].notin(elements).name('tmp')
    ].order_by('id')
    result = expr.execute().tmp

    expected = ~sorted_df[column].isin(elements)
    expected = backend.default_series_rename(expected)
    backend.assert_series_equal(result, expected)


@pytest.mark.parametrize(
    ('predicate_fn', 'expected_fn'),
    [
        param(
            lambda t: t['bool_col'],
            lambda df: df['bool_col'],
            id="no_op",
            marks=pytest.mark.min_version(datafusion="0.5.0"),
        ),
        param(lambda t: ~t['bool_col'], lambda df: ~df['bool_col'], id="negate"),
        param(
            lambda t: t.bool_col & t.bool_col,
            lambda df: df.bool_col & df.bool_col,
            id="and",
        ),
        param(
            lambda t: t.bool_col | t.bool_col,
            lambda df: df.bool_col | df.bool_col,
            id="or",
        ),
        param(
            lambda t: t.bool_col ^ t.bool_col,
            lambda df: df.bool_col ^ df.bool_col,
            id="xor",
            marks=pytest.mark.notimpl(["datafusion"]),
        ),
    ],
)
@pytest.mark.notimpl(["druid"])
def test_filter(backend, alltypes, sorted_df, predicate_fn, expected_fn):
    sorted_alltypes = alltypes.order_by('id')
    table = sorted_alltypes[predicate_fn(sorted_alltypes)].order_by('id')
    result = table.execute()
    expected = sorted_df[expected_fn(sorted_df)]

    backend.assert_frame_equal(result, expected)


@pytest.mark.notimpl(
    [
        "bigquery",
        "clickhouse",
        "datafusion",
        "duckdb",
        "impala",
        "mysql",
        "postgres",
        "sqlite",
        "snowflake",
        "polars",
        "mssql",
        "trino",
        "druid",
    ]
)
def test_filter_with_window_op(backend, alltypes, sorted_df):
    sorted_alltypes = alltypes.order_by('id')
    table = sorted_alltypes
    window = ibis.window(group_by=table.id)
    table = table.filter(lambda t: t['id'].mean().over(window) > 3).order_by('id')
    result = table.execute()
    expected = (
        sorted_df.groupby(['id'])
        .filter(lambda t: t['id'].mean() > 3)
        .reset_index(drop=True)
    )
    backend.assert_frame_equal(result, expected)


@pytest.mark.notimpl(["datafusion"])
def test_case_where(backend, alltypes, df):
    table = alltypes
    table = table.mutate(
        new_col=(
            ibis.case()
            .when(table['int_col'] == 1, 20)
            .when(table['int_col'] == 0, 10)
            .else_(0)
            .end()
            .cast('int64')
        )
    )

    result = table.execute()

    expected = df.copy()
    mask_0 = expected['int_col'] == 1
    mask_1 = expected['int_col'] == 0

    expected['new_col'] = 0
    expected.loc[mask_0, 'new_col'] = 20
    expected.loc[mask_1, 'new_col'] = 10

    backend.assert_frame_equal(result, expected)


# TODO: some of these are notimpl (datafusion) others are probably never
@pytest.mark.notimpl(["datafusion", "mysql", "sqlite", "mssql", "druid"])
@pytest.mark.min_version(duckdb="0.3.3", reason="isnan/isinf unsupported")
def test_select_filter_mutate(backend, alltypes, df):
    """Test that select, filter and mutate are executed in right order.

    Before PR #2635, try_fusion in analysis.py would fuse these
    operations together in a way that the order of the operations were
    wrong. (mutate was executed before filter).
    """
    t = alltypes

    # Prepare the float_col so that filter must execute
    # before the cast to get the correct result.
    t = t.mutate(
        float_col=ibis.case().when(t['bool_col'], t['float_col']).else_(np.nan).end()
    )

    # Actual test
    t = t[t.columns]
    t = t[~t['float_col'].isnan()]
    t = t.mutate(float_col=t['float_col'].cast('float64'))
    result = t.execute()

    expected = df.copy()
    expected.loc[~df['bool_col'], 'float_col'] = None
    expected = expected[~expected['float_col'].isna()].reset_index(drop=True)
    expected = expected.assign(float_col=expected['float_col'].astype('float64'))

    backend.assert_series_equal(result.float_col, expected.float_col)


def test_table_fillna_invalid(alltypes):
    with pytest.raises(
        com.IbisTypeError, match=r"Column 'invalid_col' is not found in table"
    ):
        alltypes.fillna({'invalid_col': 0.0})

    with pytest.raises(
        com.IbisTypeError, match="Cannot fillna on column 'string_col' of type.*"
    ):
        alltypes[["int_col", "string_col"]].fillna(0)

    with pytest.raises(
        com.IbisTypeError, match="Cannot fillna on column 'int_col' of type.*"
    ):
        alltypes.fillna({"int_col": "oops"})


@pytest.mark.parametrize(
    "replacements",
    [
        {"int_col": 20},
        {"double_col": -1, "string_col": "missing"},
        {"double_col": -1.5, "string_col": "missing"},
    ],
)
@pytest.mark.notimpl(["datafusion", "mssql", "clickhouse", "druid"])
def test_table_fillna_mapping(backend, alltypes, replacements):
    table = alltypes.mutate(
        int_col=alltypes.int_col.nullif(1),
        double_col=alltypes.double_col.nullif(3.0),
        string_col=alltypes.string_col.nullif("2"),
    ).select("id", "int_col", "double_col", "string_col")
    pd_table = table.execute()

    result = table.fillna(replacements).execute().reset_index(drop=True)
    expected = pd_table.fillna(replacements).reset_index(drop=True)

    backend.assert_frame_equal(result, expected, check_dtype=False)


@pytest.mark.notimpl(["datafusion", "mssql", "clickhouse", "druid"])
def test_table_fillna_scalar(backend, alltypes):
    table = alltypes.mutate(
        int_col=alltypes.int_col.nullif(1),
        double_col=alltypes.double_col.nullif(3.0),
        string_col=alltypes.string_col.nullif("2"),
    ).select("id", "int_col", "double_col", "string_col")
    pd_table = table.execute()

    res = table[["int_col", "double_col"]].fillna(0).execute().reset_index(drop=True)
    sol = pd_table[["int_col", "double_col"]].fillna(0).reset_index(drop=True)
    backend.assert_frame_equal(res, sol, check_dtype=False)

    res = table[["string_col"]].fillna("missing").execute().reset_index(drop=True)
    sol = pd_table[["string_col"]].fillna("missing").reset_index(drop=True)
    backend.assert_frame_equal(res, sol, check_dtype=False)


def test_mutate_rename(alltypes):
    table = alltypes.select(["bool_col", "string_col"])
    table = table.mutate(dupe_col=table["bool_col"])
    result = table.execute()
    # check_dtype is False here because there are dtype diffs between
    # Pyspark and Pandas on Java 8 - filling the 'none_col' with an int
    # results in float in Pyspark, and int in Pandas. This diff does
    # not exist in Java 11.
    assert list(result.columns) == ["bool_col", "string_col", "dupe_col"]


def test_dropna_invalid(alltypes):
    with pytest.raises(
        com.IbisTypeError, match=r"Column 'invalid_col' is not found in table"
    ):
        alltypes.dropna(subset=['invalid_col'])

    with pytest.raises(ValueError, match=r".*is not in.*"):
        alltypes.dropna(how='invalid')


@pytest.mark.parametrize(
    'how', ['any', pytest.param('all', marks=pytest.mark.notyet("polars"))]
)
@pytest.mark.parametrize(
    'subset', [None, [], 'col_1', ['col_1', 'col_2'], ['col_1', 'col_3']]
)
@pytest.mark.notimpl(["datafusion"])
def test_dropna_table(backend, alltypes, how, subset):
    is_two = alltypes.int_col == 2
    is_four = alltypes.int_col == 4

    table = alltypes.mutate(
        col_1=is_two.ifelse(ibis.NA, alltypes.float_col),
        col_2=is_four.ifelse(ibis.NA, alltypes.float_col),
        col_3=(is_two | is_four).ifelse(ibis.NA, alltypes.float_col),
    ).select("col_1", "col_2", "col_3")

    table_pandas = table.execute()

    result = table.dropna(subset, how).execute().reset_index(drop=True)
    expected = table_pandas.dropna(how=how, subset=subset).reset_index(drop=True)

    backend.assert_frame_equal(result, expected)


def test_select_sort_sort(alltypes):
    query = alltypes[alltypes.year, alltypes.bool_col]
    query = query.order_by(query.year).order_by(query.bool_col)


@pytest.mark.parametrize(
    "key, df_kwargs",
    [
        param("id", {"by": "id"}),
        param(_.id, {"by": "id"}),
        param(lambda _: _.id, {"by": "id"}),
        param(
            ("id", False),
            {"by": "id", "ascending": False},
            marks=pytest.mark.notimpl(["dask"]),
        ),
        param(
            ibis.desc("id"),
            {"by": "id", "ascending": False},
            marks=pytest.mark.notimpl(["dask"]),
        ),
        param(
            ["id", "int_col"],
            {"by": ["id", "int_col"]},
            marks=pytest.mark.notimpl(["dask"]),
        ),
        param(
            ["id", ("int_col", False)],
            {"by": ["id", "int_col"], "ascending": [True, False]},
            marks=pytest.mark.notimpl(["dask"]),
        ),
    ],
)
@pytest.mark.notimpl(["druid"])
def test_order_by(backend, alltypes, df, key, df_kwargs):
    result = alltypes.filter(_.id < 100).order_by(key).execute()
    expected = df.loc[df.id < 100].sort_values(**df_kwargs)
    backend.assert_frame_equal(result, expected)


@pytest.mark.notimpl(["dask", "pandas", "polars", "mssql", "druid"])
def test_order_by_random(alltypes):
    expr = alltypes.filter(_.id < 100).order_by(ibis.random()).limit(5)
    r1 = expr.execute()
    r2 = expr.execute()
    assert len(r1) == 5
    assert len(r2) == 5
    # Ensure that multiple executions returns different results
    assert not r1.equals(r2)


@pytest.mark.notyet(
    ["druid"],
    raises=sa.exc.ProgrammingError,
    reason="Druid only supports trivial unions",
)
@pytest.mark.notimpl(["datafusion"], raises=com.OperationNotDefinedError)
def test_table_info(alltypes):
    expr = alltypes.info()
    df = expr.execute()
    assert alltypes.columns == list(df.name)
    assert expr.columns == [
        "name",
        "type",
        "nullable",
        "nulls",
        "non_nulls",
        "null_frac",
        "pos",
    ]
    assert expr.columns == list(df.columns)


@pytest.mark.parametrize(
    ("ibis_op", "pandas_op"),
    [
        param(
            _.string_col.isin([]),
            lambda df: df.string_col.isin([]),
            id="isin",
        ),
        param(
            _.string_col.notin([]),
            lambda df: ~df.string_col.isin([]),
            id="notin",
        ),
        param(
            (_.string_col.length() * 1).isin([1]),
            lambda df: (df.string_col.str.len() * 1).isin([1]),
            id="isin_non_empty",
        ),
        param(
            (_.string_col.length() * 1).notin([1]),
            lambda df: ~(df.string_col.str.len() * 1).isin([1]),
            id="notin_non_empty",
        ),
    ],
)
def test_isin_notin(backend, alltypes, df, ibis_op, pandas_op):
    expr = alltypes[ibis_op]
    expected = df.loc[pandas_op(df)].sort_values(["id"]).reset_index(drop=True)
    result = expr.execute().sort_values(["id"]).reset_index(drop=True)
    backend.assert_frame_equal(result, expected)


@pytest.mark.notyet(
    ["dask"],
    reason="dask doesn't support Series as isin/notin argument",
    raises=NotImplementedError,
)
@pytest.mark.notimpl(["datafusion", "druid"])
@pytest.mark.parametrize(
    ("ibis_op", "pandas_op"),
    [
        param(
            _.string_col.isin(_.string_col),
            lambda df: df.string_col.isin(df.string_col),
            id="isin_col",
        ),
        param(
            (_.bigint_col + 1).isin(_.string_col.length() + 1),
            lambda df: df.bigint_col.add(1).isin(df.string_col.str.len().add(1)),
            id="isin_expr",
        ),
        param(
            _.string_col.notin(_.string_col),
            lambda df: ~df.string_col.isin(df.string_col),
            id="notin_col",
        ),
        param(
            (_.bigint_col + 1).notin(_.string_col.length() + 1),
            lambda df: ~(df.bigint_col.add(1)).isin(df.string_col.str.len().add(1)),
            id="notin_expr",
        ),
    ],
)
def test_isin_notin_column_expr(backend, alltypes, df, ibis_op, pandas_op):
    expr = alltypes[ibis_op].order_by("id")
    expected = df[pandas_op(df)].sort_values(["id"]).reset_index(drop=True)
    result = expr.execute()
    backend.assert_frame_equal(result, expected)


@pytest.mark.parametrize(
    ("expr", "expected", "op"),
    [
        param(True, True, toolz.identity, id="true_noop"),
        param(False, False, toolz.identity, id="false_noop"),
        param(
            True, False, invert, id="true_invert", marks=pytest.mark.notimpl(["mssql"])
        ),
        param(
            False, True, invert, id="false_invert", marks=pytest.mark.notimpl(["mssql"])
        ),
        param(True, False, neg, id="true_negate", marks=pytest.mark.notimpl(["mssql"])),
        param(
            False, True, neg, id="false_negate", marks=pytest.mark.notimpl(["mssql"])
        ),
    ],
)
def test_logical_negation_literal(con, expr, expected, op):
    assert con.execute(op(ibis.literal(expr)).name("tmp")) == expected


@pytest.mark.parametrize(
    "op",
    [
        toolz.identity,
        param(invert, marks=pytest.mark.notimpl(["mssql"])),
        param(neg, marks=pytest.mark.notimpl(["mssql"])),
    ],
)
def test_logical_negation_column(backend, alltypes, df, op):
    result = op(alltypes["bool_col"]).name("tmp").execute()
    expected = op(df["bool_col"])
    backend.assert_series_equal(result, expected, check_names=False)


@pytest.mark.notimpl(["datafusion"])
@pytest.mark.parametrize(
    ("dtype", "zero", "expected"),
    [("int64", 0, 1), ("float64", 0.0, 1.0)],
)
def test_zeroifnull_literals(con, dtype, zero, expected):
    assert con.execute(ibis.NA.cast(dtype).zeroifnull()) == zero
    assert con.execute(ibis.literal(expected, type=dtype).zeroifnull()) == expected


@pytest.mark.notimpl(["datafusion"])
@pytest.mark.min_version(
    dask="2022.01.1",
    reason="unsupported operation with later versions of pandas",
)
def test_zeroifnull_column(backend, alltypes, df):
    expr = alltypes.int_col.nullif(1).zeroifnull().name('tmp')
    result = expr.execute().astype("int32")
    expected = df.int_col.replace(1, 0).rename("tmp").astype("int32")
    backend.assert_series_equal(result, expected)


@pytest.mark.notimpl(["datafusion"])
def test_where_select(backend, alltypes, df):
    table = alltypes
    table = table.select(
        [
            "int_col",
            (ibis.where(table["int_col"] == 0, 42, -1).cast("int64").name("where_col")),
        ]
    )

    result = table.execute()

    expected = df.loc[:, ["int_col"]].copy()

    expected['where_col'] = -1
    expected.loc[expected['int_col'] == 0, 'where_col'] = 42

    backend.assert_frame_equal(result, expected)


@pytest.mark.notimpl(["datafusion"])
def test_where_column(backend, alltypes, df):
    expr = ibis.where(alltypes["int_col"] == 0, 42, -1).cast("int64").name("where_col")
    result = expr.execute()

    expected = pd.Series(
        np.where(df.int_col == 0, 42, -1),
        name="where_col",
        dtype="int64",
    )

    backend.assert_series_equal(result, expected)


def test_select_filter(backend, alltypes, df):
    t = alltypes

    expr = t.select("int_col").filter(t.string_col == "4")
    result = expr.execute()

    expected = df.loc[df.string_col == "4", ["int_col"]].reset_index(drop=True)
    backend.assert_frame_equal(result, expected)


def test_select_filter_select(backend, alltypes, df):
    t = alltypes
    expr = t.select("int_col").filter(t.string_col == "4").int_col
    result = expr.execute().rename("int_col")

    expected = df.loc[df.string_col == "4", "int_col"].reset_index(drop=True)
    backend.assert_series_equal(result, expected)


@pytest.mark.notimpl(["datafusion"], raises=com.OperationNotDefinedError)
@pytest.mark.broken(["mssql"], raises=sa.exc.OperationalError)
def test_between(backend, alltypes, df):
    expr = alltypes.double_col.between(5, 10)
    result = expr.execute().rename("double_col")

    expected = df.double_col.between(5, 10)
    backend.assert_series_equal(result, expected)


@pytest.mark.notimpl(["druid"])
def test_interactive(alltypes):
    expr = alltypes.mutate(
        str_col=_.string_col.replace("1", "").nullif("2"),
        date_col=_.timestamp_col.date(),
        delta_col=lambda t: ibis.now() - t.timestamp_col,
    )

    orig = ibis.options.interactive
    ibis.options.interactive = True
    try:
        repr(expr)
    finally:
        ibis.options.interactive = orig


def test_correlated_subquery(alltypes):
    expr = alltypes[_.double_col > _.view().double_col]
    assert expr.compile() is not None


@pytest.mark.notimpl(["dask", "polars", "pyspark", "datafusion"])
def test_uncorrelated_subquery(backend, batting, batting_df):
    subset_batting = batting[batting.yearID <= 2000]
    expr = batting[_.yearID == subset_batting.yearID.max()]['playerID', 'yearID']
    result = expr.execute()

    expected = batting_df[batting_df.yearID == 2000][['playerID', 'yearID']]
    backend.assert_frame_equal(result, expected)


def test_int_column(alltypes):
    expr = alltypes.mutate(x=1).x
    result = expr.execute()
    assert expr.type() == dt.int8
    assert result.dtype == np.int8


@pytest.mark.notimpl(["datafusion", "druid"])
@pytest.mark.never(
    ["bigquery", "sqlite", "snowflake"], reason="backend only implements int64"
)
def test_int_scalar(alltypes):
    expr = alltypes.smallint_col.min()
    result = expr.execute()
    assert expr.type() == dt.int16
    assert result.dtype == np.int16


@pytest.mark.notimpl(["dask", "datafusion", "pandas", "polars", "druid"])
@pytest.mark.notyet(
    ["clickhouse"], reason="https://github.com/ClickHouse/ClickHouse/issues/6697"
)
@pytest.mark.notyet(["pyspark"])
@pytest.mark.parametrize("method_name", ["any", "notany"])
def test_exists(batting, awards_players, method_name):
    years = [1980, 1981]
    batting_years = [1871, *years]
    batting = batting[batting.yearID.isin(batting_years)]
    awards_players = awards_players[awards_players.yearID.isin(years)]
    method = methodcaller(method_name)
    expr = batting[method(batting.yearID == awards_players.yearID)]
    result = expr.execute()
    assert not result.empty


@pytest.mark.notimpl(
    [
        "dask",
        "datafusion",
        "mssql",
        "mysql",
        "pandas",
        "pyspark",
        "polars",
        "druid",
    ],
    raises=com.OperationNotDefinedError,
)
def test_typeof(backend, con):
    # Other tests also use the typeof operation, but only this test has this operation required.
    expr = ibis.literal(1).typeof()
    result = con.execute(expr)

    assert result is not None


@pytest.mark.broken(["polars"], reason="incorrect answer")
@pytest.mark.notyet(["impala"], reason="can't find table in subquery")
@pytest.mark.notimpl(["datafusion", "pyspark", "druid"])
@pytest.mark.notyet(["dask", "mssql"], reason="not supported by the backend")
def test_isin_uncorrelated(
    backend, batting, awards_players, batting_df, awards_players_df
):
    expr = batting.select(
        "playerID",
        "yearID",
        has_year_id=batting.yearID.isin(awards_players.yearID),
    ).order_by(["playerID", "yearID"])
    result = expr.execute().has_year_id
    expected = (
        batting_df.sort_values(["playerID", "yearID"])
        .reset_index(drop=True)
        .yearID.isin(awards_players_df.yearID)
        .rename("has_year_id")
    )
    backend.assert_series_equal(result, expected)


@pytest.mark.broken(["polars"], reason="incorrect answer")
@pytest.mark.notimpl(["datafusion", "pyspark", "druid"])
@pytest.mark.notyet(["dask"], reason="not supported by the backend")
def test_isin_uncorrelated_filter(
    backend, batting, awards_players, batting_df, awards_players_df
):
    expr = (
        batting.select("playerID", "yearID")
        .filter(batting.yearID.isin(awards_players.yearID))
        .order_by(["playerID", "yearID"])
    )
    result = expr.execute()
    expected = (
        batting_df.loc[
            batting_df.yearID.isin(awards_players_df.yearID), ["playerID", "yearID"]
        ]
        .sort_values(["playerID", "yearID"])
        .reset_index(drop=True)
    )
    backend.assert_frame_equal(result, expected)


@pytest.mark.parametrize(
    "dtype",
    [
        "bool",
        param("bytes", marks=pytest.mark.notyet(["polars"])),
        "str",
        "int",
        "float",
        "int8",
        "int16",
        "int32",
        "int64",
        "float32",
        "float64",
        "timestamp",
        "date",
        "time",
    ],
)
def test_literal_na(con, dtype):
    expr = ibis.literal(None, type=dtype)
    result = con.execute(expr)
    assert pd.isna(result)


@pytest.mark.notimpl(["datafusion"], raises=com.OperationNotDefinedError)
def test_memtable_bool_column(backend, con, monkeypatch):
    monkeypatch.setattr(ibis.options, "default_backend", con)

    t = ibis.memtable({"a": [True, False, True]})
    backend.assert_series_equal(t.a.execute(), pd.Series([True, False, True], name="a"))


@pytest.mark.notimpl(["datafusion"], raises=com.OperationNotDefinedError)
@pytest.mark.broken(
    ["druid"],
    raises=AssertionError,
    reason="result contains empty strings instead of None",
)
def test_memtable_construct(backend, con, monkeypatch):
    pa = pytest.importorskip("pyarrow")
    monkeypatch.setattr(ibis.options, "default_backend", con)

    pa_t = pa.Table.from_pydict(
        {
            "a": list("abc"),
            "b": [1, 2, 3],
            "c": [1.0, 2.0, 3.0],
            "d": [None, "b", None],
        }
    )
    t = ibis.memtable(pa_t)
    backend.assert_frame_equal(t.execute(), pa_t.to_pandas())


@pytest.mark.notimpl(
    ["dask", "datafusion", "pandas", "polars"],
    raises=NotImplementedError,
    reason="not a SQL backend",
)
@pytest.mark.notimpl(
    ["pyspark"], reason="pyspark doesn't generate SQL", raises=NotImplementedError
)
@pytest.mark.notimpl(["druid"], reason="no sqlglot dialect", raises=ValueError)
def test_many_subqueries(con, snapshot):
    def query(t, group_cols):
        t2 = t.mutate(key=ibis.row_number().over(ibis.window(order_by=group_cols)))
        return t2.inner_join(t2[["key"]], "key")

    t = ibis.table(dict(street="str"), name="data")

    t2 = query(t, group_cols=["street"])
    t3 = query(t2, group_cols=["street"])

    snapshot.assert_match(str(ibis.to_sql(t3, dialect=con.name)), "out.sql")


@pytest.mark.notimpl(["dask", "pandas"], raises=com.OperationNotDefinedError)
@pytest.mark.notyet(["polars"], reason="polars doesn't expand > 1 explode")
@pytest.mark.notimpl(["druid"], raises=AssertionError)
@pytest.mark.notyet(
    ["bigquery"],
    reason="backend doesn't implement unnest",
    raises=com.OperationNotDefinedError,
)
@pytest.mark.notyet(
    ["datafusion", "impala", "mssql", "mysql", "sqlite"],
    reason="backend doesn't support arrays and we don't implement pivot_longer with unions yet",
    raises=com.OperationNotDefinedError,
)
def test_pivot_longer(backend):
    diamonds = backend.diamonds
    df = diamonds.execute()
    res = diamonds.pivot_longer(s.c("x", "y", "z"), names_to="pos", values_to="xyz")
    assert res.schema().names == (
        "carat",
        "cut",
        "color",
        "clarity",
        "depth",
        "table",
        "price",
        "pos",
        "xyz",
    )
    expected = pd.melt(
        df,
        id_vars=[
            "carat",
            "cut",
            "color",
            "clarity",
            "depth",
            "table",
            "price",
        ],
        value_vars=list('xyz'),
        var_name="pos",
        value_name="xyz",
    )
    assert len(res.execute()) == len(expected)


@pytest.mark.notyet(["datafusion"], raises=com.OperationNotDefinedError)
def test_pivot_wider(backend):
    diamonds = backend.diamonds
    expr = (
        diamonds.group_by(["cut", "color"])
        .agg(carat=_.carat.mean())
        .pivot_wider(
            names_from="cut", values_from="carat", names_sort=True, values_agg="mean"
        )
    )
    df = expr.execute()
    assert set(df.columns) == {"color"} | set(
        diamonds[["cut"]].distinct().cut.execute()
    )
    assert len(df) == diamonds.color.nunique().execute()


@pytest.mark.parametrize(
    "on",
    [
        param(
            ["cut"],
            marks=[
                pytest.mark.notimpl(
                    ["mssql", "mysql"], raises=com.OperationNotDefinedError
                ),
            ],
            id="one",
        ),
        param(
            ["clarity", "cut"],
            marks=[
                pytest.mark.notimpl(
                    ["mssql", "mysql"], raises=com.OperationNotDefinedError
                ),
            ],
            id="many",
        ),
    ],
)
@pytest.mark.parametrize(
    "keep",
    [
        "first",
        param(
            "last",
            marks=pytest.mark.notimpl(
                ["bigquery", "snowflake", "trino"],
                raises=com.UnsupportedOperationError,
                reason="backend doesn't support how='last'",
            ),
        ),
    ],
)
@pytest.mark.notimpl(
    ["druid", "impala"],
    raises=(NotImplementedError, sa.exc.ProgrammingError, com.OperationNotDefinedError),
    reason="arbitrary not implemented in the backend",
)
@pytest.mark.notimpl(
    ["dask", "datafusion", "polars"],
    raises=com.OperationNotDefinedError,
    reason="backend doesn't implement window functions",
)
@pytest.mark.notimpl(
    ["pandas"],
    raises=com.OperationNotDefinedError,
    reason="backend doesn't implement ops.WindowFunction",
)
def test_distinct_on_keep(backend, on, keep):
    from ibis import _

    t = backend.diamonds.mutate(one=ibis.literal(1)).mutate(
        idx=ibis.row_number().over(order_by=_.one, rows=(None, 0))
    )

    requires_cache = backend.name() in ("mysql", "impala")

    if requires_cache:
        t = t.cache()
    expr = t.distinct(on=on, keep=keep).order_by(ibis.asc("idx"))
    result = expr.execute()
    df = t.execute()
    expected = (
        df.drop_duplicates(subset=on, keep=keep or False)
        .sort_values(by=["idx"])
        .reset_index(drop=True)
    )
    assert len(result) == len(expected)


@pytest.mark.parametrize(
    "on",
    [
        param(
            ["cut"],
            marks=[
                pytest.mark.notimpl(
                    ["mssql", "mysql"], raises=com.OperationNotDefinedError
                ),
            ],
            id="one",
        ),
        param(
            ["clarity", "cut"],
            marks=[
                pytest.mark.notimpl(
                    ["mssql", "mysql"], raises=com.OperationNotDefinedError
                ),
            ],
            id="many",
        ),
    ],
)
@pytest.mark.notimpl(
    ["druid", "impala"],
    raises=(NotImplementedError, sa.exc.ProgrammingError, com.OperationNotDefinedError),
    reason="arbitrary not implemented in the backend",
)
@pytest.mark.notimpl(
    ["dask", "datafusion", "polars"],
    raises=com.OperationNotDefinedError,
    reason="backend doesn't implement window functions",
)
@pytest.mark.notimpl(
    ["pandas"],
    raises=com.OperationNotDefinedError,
    reason="backend doesn't implement ops.WindowFunction",
)
@pytest.mark.notimpl(
    ["pyspark"],
    raises=com.UnsupportedOperationError,
    reason="backend doesn't support `having` filters",
)
def test_distinct_on_keep_is_none(backend, on):
    from ibis import _

    t = backend.diamonds.mutate(one=ibis.literal(1)).mutate(
        idx=ibis.row_number().over(order_by=_.one, rows=(None, 0))
    )

    requires_cache = backend.name() in ("mysql", "impala")

    if requires_cache:
        t = t.cache()
    expr = t.distinct(on=on, keep=None).order_by(ibis.asc("idx"))
    result = expr.execute()
    df = t.execute()
    expected = (
        df.drop_duplicates(subset=on, keep=False)
        .sort_values(by=["idx"])
        .reset_index(drop=True)
    )
    assert len(result) == len(expected)
