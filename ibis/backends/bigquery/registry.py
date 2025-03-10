"""Module to convert from Ibis expression to SQL string."""

from __future__ import annotations

import base64
import datetime
from typing import Literal

import numpy as np
from multipledispatch import Dispatcher

import ibis
import ibis.common.exceptions as com
import ibis.expr.datatypes as dt
import ibis.expr.operations as ops
from ibis.backends.base.sql import compiler
from ibis.backends.base.sql.registry import (
    fixed_arity,
    literal,
    operation_registry,
    reduction,
    unary,
)
from ibis.backends.bigquery.datatypes import ibis_type_to_bigquery_type
from ibis.common.enums import DateUnit, IntervalUnit, TimeUnit


def _extract_field(sql_attr):
    def extract_field_formatter(translator, op):
        arg = translator.translate(op.args[0])
        if sql_attr == "epochseconds":
            return f"UNIX_SECONDS({arg})"
        else:
            return f"EXTRACT({sql_attr} from {arg})"

    return extract_field_formatter


bigquery_cast = Dispatcher("bigquery_cast")


@bigquery_cast.register(str, dt.Timestamp, dt.Integer)
def bigquery_cast_timestamp_to_integer(compiled_arg, from_, to):
    """Convert TIMESTAMP to INT64 (seconds since Unix epoch)."""
    return f"UNIX_MICROS({compiled_arg})"


@bigquery_cast.register(str, dt.Integer, dt.Timestamp)
def bigquery_cast_integer_to_timestamp(compiled_arg, from_, to):
    """Convert INT64 (seconds since Unix epoch) to Timestamp."""
    return f"TIMESTAMP_SECONDS({compiled_arg})"


@bigquery_cast.register(str, dt.Interval, dt.Integer)
def bigquery_cast_interval_to_integer(compiled_arg, from_, to):
    return f"EXTRACT({from_.resolution.upper()} from {compiled_arg})"


@bigquery_cast.register(str, dt.DataType, dt.DataType)
def bigquery_cast_generate(compiled_arg, from_, to):
    """Cast to desired type."""
    sql_type = ibis_type_to_bigquery_type(to)
    return f"CAST({compiled_arg} AS {sql_type})"


@bigquery_cast.register(str, dt.DataType)
def bigquery_cast_generate_simple(compiled_arg, to):
    return bigquery_cast(compiled_arg, to, to)


def _cast(translator, op):
    arg, target_type = op.args
    arg_formatted = translator.translate(arg)
    input_dtype = arg.output_dtype
    return bigquery_cast(arg_formatted, input_dtype, target_type)


def integer_to_timestamp(translator: compiler.ExprTranslator, op) -> str:
    """Interprets an integer as a timestamp."""
    arg = translator.translate(op.arg)
    unit = op.unit.short

    if unit == "s":
        return f"TIMESTAMP_SECONDS({arg})"
    elif unit == "ms":
        return f"TIMESTAMP_MILLIS({arg})"
    elif unit == "us":
        return f"TIMESTAMP_MICROS({arg})"
    elif unit == "ns":
        # Timestamps are represented internally as elapsed microseconds, so some
        # rounding is required if an integer represents nanoseconds.
        # https://cloud.google.com/bigquery/docs/reference/standard-sql/data-types#timestamp_type
        return f"TIMESTAMP_MICROS(CAST(ROUND({arg} / 1000) AS INT64))"

    raise NotImplementedError(f"cannot cast unit {op.unit}")


def _struct_field(translator, op):
    arg = translator.translate(op.arg)
    return f"{arg}.`{op.field}`"


def _struct_column(translator, op):
    cols = (
        f'{translator.translate(value)} AS {name}'
        for name, value, in zip(op.names, op.values)
    )
    return "STRUCT({})".format(", ".join(cols))


def _array_concat(translator, op):
    return "ARRAY_CONCAT({})".format(", ".join(map(translator.translate, op.args)))


def _array_column(translator, op):
    return "[{}]".format(", ".join(map(translator.translate, op.cols)))


def _array_index(translator, op):
    # SAFE_OFFSET returns NULL if out of bounds
    arg = translator.translate(op.arg)
    index = translator.translate(op.index)
    return f"{arg}[SAFE_OFFSET({index})]"


def _hash(translator, op):
    arg, how = op.args

    arg_formatted = translator.translate(arg)

    if how == "farm_fingerprint":
        return f"farm_fingerprint({arg_formatted})"
    else:
        raise NotImplementedError(how)


def _string_find(translator, op):
    haystack, needle, start, end = op.args

    if start is not None:
        raise NotImplementedError("start not implemented for string find")
    if end is not None:
        raise NotImplementedError("end not implemented for string find")

    return "STRPOS({}, {}) - 1".format(
        translator.translate(haystack), translator.translate(needle)
    )


def _translate_pattern(translator, op):
    # add 'r' to string literals to indicate to BigQuery this is a raw string
    return "r" * isinstance(op, ops.Literal) + translator.translate(op)


def _regex_search(translator, op):
    arg = translator.translate(op.arg)
    regex = _translate_pattern(translator, op.pattern)
    return f"REGEXP_CONTAINS({arg}, {regex})"


def _regex_extract(translator, op):
    arg = translator.translate(op.arg)
    regex = _translate_pattern(translator, op.pattern)
    index = translator.translate(op.index)
    matches = f"REGEXP_CONTAINS({arg}, {regex})"
    extract = f"REGEXP_EXTRACT_ALL({arg}, {regex})[SAFE_ORDINAL({index})]"
    return f"IF({matches}, IF(COALESCE({index}, 0) = 0, {arg}, {extract}), NULL)"


def _regex_replace(translator, op):
    arg = translator.translate(op.arg)
    regex = _translate_pattern(translator, op.pattern)
    replacement = translator.translate(op.replacement)
    return f"REGEXP_REPLACE({arg}, {regex}, {replacement})"


def _string_concat(translator, op):
    args = ", ".join(map(translator.translate, op.arg))
    return f"CONCAT({args})"


def _string_join(translator, op):
    sep, args = op.args
    return "ARRAY_TO_STRING([{}], {})".format(
        ", ".join(map(translator.translate, args)), translator.translate(sep)
    )


def _string_ascii(translator, op):
    arg = translator.translate(op.arg)
    return f"TO_CODE_POINTS({arg})[SAFE_OFFSET(0)]"


def _string_right(translator, op):
    arg, nchars = map(translator.translate, op.args)
    return f"SUBSTR({arg}, -LEAST(LENGTH({arg}), {nchars}))"


def _string_substring(translator, op):
    length = op.length
    if (length := getattr(length, "value", None)) is not None and length < 0:
        raise ValueError("Length parameter must be a non-negative value.")

    arg = translator.translate(op.arg)
    start = translator.translate(op.start)

    arg_length = f"LENGTH({arg})"
    if op.length is not None:
        suffix = f", {translator.translate(op.length)}"
    else:
        suffix = ""

    if_pos = f"SUBSTR({arg}, {start} + 1{suffix})"
    if_neg = f"SUBSTR({arg}, {arg_length} + {start} + 1{suffix})"
    return f"IF({start} >= 0, {if_pos}, {if_neg})"


def _array_literal_format(op):
    return str(list(op.value))


def _log(translator, op):
    arg, base = op.args
    arg_formatted = translator.translate(arg)

    if base is None:
        return f"ln({arg_formatted})"

    base_formatted = translator.translate(base)
    return f"log({arg_formatted}, {base_formatted})"


def _literal(translator, op):
    dtype = op.output_dtype
    value = op.value

    if value is None:
        return "NULL"

    if dtype.is_decimal():
        if value.is_nan():
            return "CAST('NaN' AS FLOAT64)"
        if value.is_infinite():
            prefix = "-" * value.is_signed()
            return f"CAST('{prefix}inf' AS FLOAT64)"
        else:
            return f"{ibis_type_to_bigquery_type(dtype)} '{value}'"
    elif dtype.is_uuid():
        return translator.translate(ops.Literal(str(value), dtype=dt.str))

    if isinstance(dtype, dt.Numeric):
        if not np.isfinite(value):
            return f"CAST({str(value)!r} AS FLOAT64)"

    # special case literal timestamp, date, and time scalars
    if isinstance(op, ops.Literal):
        if isinstance(dtype, dt.Date):
            if isinstance(value, datetime.datetime):
                raw_value = value.date()
            else:
                raw_value = value
            return f"DATE '{raw_value}'"
        elif isinstance(dtype, dt.Timestamp):
            return f"TIMESTAMP '{value}'"
        elif isinstance(dtype, dt.Time):
            # TODO: define extractors on TimeValue expressions
            return f"TIME '{value}'"
        elif isinstance(dtype, dt.Binary):
            return "FROM_BASE64('{}')".format(
                base64.b64encode(value).decode(encoding="utf-8")
            )
        elif dtype.is_struct():
            cols = (
                f'{translator.translate(ops.Literal(value[name], dtype=type_))} AS {name}'
                for name, type_ in zip(dtype.names, dtype.types)
            )
            return "STRUCT({})".format(", ".join(cols))

    try:
        return literal(translator, op)
    except NotImplementedError:
        if isinstance(dtype, dt.Array):
            return _array_literal_format(op)
        raise NotImplementedError(f'Unsupported type: {dtype!r}')


def _arbitrary(translator, op):
    arg, how, where = op.args

    if where is not None:
        arg = ops.Where(where, arg, ibis.NA)

    if how != "first":
        raise com.UnsupportedOperationError(
            f"{how!r} value not supported for arbitrary in BigQuery"
        )

    return f"ANY_VALUE({translator.translate(arg)})"


def _truncate(kind, units):
    def truncator(translator, op):
        arg, unit = op.args
        trans_arg = translator.translate(arg)
        if unit not in units:
            raise com.UnsupportedOperationError(
                f"BigQuery does not support truncating {arg.output_dtype} values to unit {unit!r}"
            )
        if unit.name == "WEEK":
            unit = "WEEK(MONDAY)"
        else:
            unit = unit.name
        return f"{kind}_TRUNC({trans_arg}, {unit})"

    return truncator


def _timestamp_op(func, units):
    def _formatter(translator, op):
        arg, offset = op.args

        unit = offset.output_dtype.unit
        if unit not in units:
            raise com.UnsupportedOperationError(
                "BigQuery does not allow binary operation "
                f"{func} with INTERVAL offset {unit}"
            )
        formatted_arg = translator.translate(arg)
        formatted_offset = translator.translate(offset)
        result = f"{func}({formatted_arg}, {formatted_offset})"
        return result

    return _formatter


def _geo_boundingbox(dimension_name):
    def _formatter(translator, op):
        geog = op.args[0]
        geog_formatted = translator.translate(geog)
        return f"ST_BOUNDINGBOX({geog_formatted}).{dimension_name}"

    return _formatter


def _geo_simplify(translator, op):
    geog, tolerance, preserve_collapsed = op.args
    if preserve_collapsed.value:
        raise com.UnsupportedOperationError(
            "BigQuery simplify does not support preserving collapsed geometries, "
            "must pass preserve_collapsed=False"
        )
    geog, tolerance = map(translator.translate, (geog, tolerance))
    return f"ST_SIMPLIFY({geog}, {tolerance})"


STRFTIME_FORMAT_FUNCTIONS = {
    dt.Date: "DATE",
    dt.Time: "TIME",
    dt.Timestamp: "TIMESTAMP",
}


def bigquery_day_of_week_index(t, op):
    """Convert timestamp to day-of-week integer."""
    arg = op.args[0]
    arg_formatted = t.translate(arg)
    return f"MOD(EXTRACT(DAYOFWEEK FROM {arg_formatted}) + 5, 7)"


def bigquery_day_of_week_name(t, op):
    """Convert timestamp to day-of-week name."""
    return f"INITCAP(CAST({t.translate(op.arg)} AS STRING FORMAT 'DAY'))"


def bigquery_compiles_divide(t, op):
    """Floating point division."""
    return f"IEEE_DIVIDE({t.translate(op.left)}, {t.translate(op.right)})"


def compiles_strftime(translator, op):
    """Timestamp formatting."""
    arg = op.arg
    format_str = op.format_str
    arg_type = arg.output_dtype
    strftime_format_func_name = STRFTIME_FORMAT_FUNCTIONS[type(arg_type)]
    fmt_string = translator.translate(format_str)
    arg_formatted = translator.translate(arg)
    if isinstance(arg_type, dt.Timestamp):
        return "FORMAT_{}({}, {}, {!r})".format(
            strftime_format_func_name,
            fmt_string,
            arg_formatted,
            arg_type.timezone if arg_type.timezone is not None else "UTC",
        )
    return "FORMAT_{}({}, {})".format(
        strftime_format_func_name, fmt_string, arg_formatted
    )


def compiles_string_to_timestamp(translator, op):
    """Timestamp parsing."""
    fmt_string = translator.translate(op.format_str)
    arg_formatted = translator.translate(op.arg)
    return f"PARSE_TIMESTAMP({fmt_string}, {arg_formatted})"


def compiles_floor(t, op):
    bigquery_type = ibis_type_to_bigquery_type(op.output_dtype)
    arg = op.arg
    return f"CAST(FLOOR({t.translate(arg)}) AS {bigquery_type})"


def compiles_approx(translator, op):
    arg = op.arg
    where = op.where

    if where is not None:
        arg = ops.Where(where, arg, ibis.NA)

    return f"APPROX_QUANTILES({translator.translate(arg)}, 2)[OFFSET(1)]"


def compiles_covar_corr(func):
    def translate(translator, op):
        left = op.left
        right = op.right

        if (where := op.where) is not None:
            left = ops.Where(where, left, None)
            right = ops.Where(where, right, None)

        left = translator.translate(
            ops.Cast(left, dt.int64) if left.output_dtype.is_boolean() else left
        )
        right = translator.translate(
            ops.Cast(right, dt.int64) if right.output_dtype.is_boolean() else right
        )
        return f"{func}({left}, {right})"

    return translate


def _covar(translator, op):
    how = op.how[:4].upper()
    assert how in ("POP", "SAMP"), 'how not in ("POP", "SAMP")'
    return compiles_covar_corr(f"COVAR_{how}")(translator, op)


def _corr(translator, op):
    if (how := op.how) == "sample":
        raise ValueError(f"Correlation with how={how!r} is not supported.")
    return compiles_covar_corr("CORR")(translator, op)


def _identical_to(t, op):
    left = t.translate(op.left)
    right = t.translate(op.right)
    return f"{left} IS NOT DISTINCT FROM {right}"


def _floor_divide(t, op):
    left = t.translate(op.left)
    right = t.translate(op.right)
    return bigquery_cast(f"FLOOR(IEEE_DIVIDE({left}, {right}))", op.output_dtype)


def _log2(t, op):
    return f"LOG({t.translate(op.arg)}, 2)"


def _is_nan(t, op):
    return f"IS_NAN({t.translate(op.arg)})"


def _is_inf(t, op):
    return f"IS_INF({t.translate(op.arg)})"


def _nullifzero(t, op):
    casted = bigquery_cast('0', op.output_dtype)
    return f"NULLIF({t.translate(op.arg)}, {casted})"


def _zeroifnull(t, op):
    casted = bigquery_cast('0', op.output_dtype)
    return f"COALESCE({t.translate(op.arg)}, {casted})"


def _array_agg(t, op):
    arg = op.arg
    if (where := op.where) is not None:
        arg = ops.Where(where, arg, ibis.NA)
    return f"ARRAY_AGG({t.translate(arg)} IGNORE NULLS)"


def _arg_min_max(sort_dir: Literal["ASC", "DESC"]):
    def translate(t, op: ops.ArgMin | ops.ArgMax) -> str:
        arg = op.arg
        if (where := op.where) is not None:
            arg = ops.Where(where, arg, None)
        arg = t.translate(arg)
        key = t.translate(op.key)
        return f"ARRAY_AGG({arg} IGNORE NULLS ORDER BY {key} {sort_dir} LIMIT 1)[SAFE_OFFSET(0)]"

    return translate


def _array_repeat(t, op):
    start = step = 1
    times = t.translate(op.times)
    arg = t.translate(op.arg)
    array_length = f"ARRAY_LENGTH({arg})"
    stop = f"GREATEST({times}, 0) * {array_length}"
    idx = f"COALESCE(NULLIF(MOD(i, {array_length}), 0), {array_length})"
    series = f"GENERATE_ARRAY({start}, {stop}, {step})"
    return f"ARRAY(SELECT {arg}[SAFE_ORDINAL({idx})] FROM UNNEST({series}) AS i)"


def _neg_idx_to_pos(array, idx):
    return f"IF({idx} < 0, ARRAY_LENGTH({array}) + {idx}, {idx})"


def _array_slice(t, op):
    arg = t.translate(op.arg)
    cond = [f"index >= {_neg_idx_to_pos(arg, t.translate(op.start))}"]
    if stop := op.stop:
        cond.append(f"index < {_neg_idx_to_pos(arg, t.translate(stop))}")
    return (
        f"ARRAY("
        f"SELECT el "
        f"FROM UNNEST({arg}) AS el WITH OFFSET index "
        f"WHERE {' AND '.join(cond)}"
        f")"
    )


def _capitalize(t, op):
    arg = t.translate(op.arg)
    return f"CONCAT(UPPER(SUBSTR({arg}, 1, 1)), SUBSTR({arg}, 2))"


def _nth_value(t, op):
    arg = t.translate(op.arg)

    if not isinstance(nth_op := op.nth, ops.Literal):
        raise TypeError(f"Bigquery nth must be a literal; got {type(op.nth)}")

    return f'NTH_VALUE({arg}, {nth_op.value + 1})'


def _interval_multiply(t, op):
    if isinstance(op.left, ops.Literal) and isinstance(op.right, ops.Literal):
        value = op.left.value * op.right.value
        literal = ops.Literal(value, op.left.output_dtype)
        return t.translate(literal)

    left, right = t.translate(op.left), t.translate(op.right)
    unit = op.left.output_dtype.resolution.upper()
    return f"INTERVAL EXTRACT({unit} from {left}) * {right} {unit}"


# BigQuery doesn't support nanosecond intervals
_DATE_UNITS = frozenset(DateUnit)
_TIME_UNITS = frozenset(TimeUnit) - {TimeUnit.NANOSECOND}
_INTERVAL_UNITS = frozenset(IntervalUnit) - {IntervalUnit.NANOSECOND}
_INTERVAL_DATE_UNITS = _INTERVAL_UNITS - {
    IntervalUnit.HOUR,
    IntervalUnit.MINUTE,
    IntervalUnit.SECOND,
    IntervalUnit.MILLISECOND,
    IntervalUnit.MICROSECOND,
}
_INTERVAL_TIME_UNITS = _INTERVAL_UNITS - _INTERVAL_DATE_UNITS


OPERATION_REGISTRY = {
    **operation_registry,
    # Literal
    ops.Literal: _literal,
    # Logical
    ops.Any: reduction("LOGICAL_OR"),
    ops.All: reduction("LOGICAL_AND"),
    ops.IfNull: fixed_arity("IFNULL", 2),
    ops.NullIf: fixed_arity("NULLIF", 2),
    ops.NullIfZero: _nullifzero,
    ops.ZeroIfNull: _zeroifnull,
    # Reductions
    ops.ApproxMedian: compiles_approx,
    ops.Covariance: _covar,
    ops.Correlation: _corr,
    # Math
    ops.Divide: bigquery_compiles_divide,
    ops.Floor: compiles_floor,
    ops.Modulus: fixed_arity("MOD", 2),
    ops.Sign: unary("SIGN"),
    ops.BitwiseNot: lambda t, op: f"~ {t.translate(op.arg)}",
    ops.BitwiseXor: lambda t, op: f"{t.translate(op.left)} ^ {t.translate(op.right)}",
    ops.BitwiseOr: lambda t, op: f"{t.translate(op.left)} | {t.translate(op.right)}",
    ops.BitwiseAnd: lambda t, op: f"{t.translate(op.left)} & {t.translate(op.right)}",
    ops.BitwiseLeftShift: lambda t, op: f"{t.translate(op.left)} << {t.translate(op.right)}",
    ops.BitwiseRightShift: lambda t, op: f"{t.translate(op.left)} >> {t.translate(op.right)}",
    # Temporal functions
    ops.Date: unary("DATE"),
    ops.DateFromYMD: fixed_arity("DATE", 3),
    ops.DateAdd: _timestamp_op("DATE_ADD", _INTERVAL_DATE_UNITS),
    ops.DateSub: _timestamp_op("DATE_SUB", _INTERVAL_DATE_UNITS),
    ops.DateTruncate: _truncate("DATE", _DATE_UNITS | _INTERVAL_DATE_UNITS),
    ops.DayOfWeekIndex: bigquery_day_of_week_index,
    ops.DayOfWeekName: bigquery_day_of_week_name,
    ops.ExtractEpochSeconds: _extract_field("epochseconds"),
    ops.ExtractYear: _extract_field("year"),
    ops.ExtractQuarter: _extract_field("quarter"),
    ops.ExtractMonth: _extract_field("month"),
    ops.ExtractWeekOfYear: _extract_field("isoweek"),
    ops.ExtractDay: _extract_field("day"),
    ops.ExtractDayOfYear: _extract_field("dayofyear"),
    ops.ExtractHour: _extract_field("hour"),
    ops.ExtractMinute: _extract_field("minute"),
    ops.ExtractSecond: _extract_field("second"),
    ops.ExtractMillisecond: _extract_field("millisecond"),
    ops.Strftime: compiles_strftime,
    ops.StringToTimestamp: compiles_string_to_timestamp,
    ops.Time: unary("TIME"),
    ops.TimeFromHMS: fixed_arity("TIME", 3),
    ops.TimeTruncate: _truncate("TIME", _TIME_UNITS | _INTERVAL_TIME_UNITS),
    ops.TimestampAdd: _timestamp_op("TIMESTAMP_ADD", _INTERVAL_TIME_UNITS),
    ops.TimestampFromUNIX: integer_to_timestamp,
    ops.TimestampFromYMDHMS: fixed_arity("DATETIME", 6),
    ops.TimestampNow: fixed_arity("CURRENT_TIMESTAMP", 0),
    ops.TimestampSub: _timestamp_op("TIMESTAMP_SUB", _INTERVAL_TIME_UNITS),
    ops.TimestampTruncate: _truncate(
        "TIMESTAMP",
        _DATE_UNITS | _TIME_UNITS | _INTERVAL_DATE_UNITS | _INTERVAL_TIME_UNITS,
    ),
    ops.IntervalMultiply: _interval_multiply,
    ops.Hash: _hash,
    ops.StringReplace: fixed_arity("REPLACE", 3),
    ops.StringSplit: fixed_arity("SPLIT", 2),
    ops.StringConcat: _string_concat,
    ops.StringJoin: _string_join,
    ops.StringAscii: _string_ascii,
    ops.StringFind: _string_find,
    ops.Substring: _string_substring,
    ops.StrRight: _string_right,
    ops.Capitalize: _capitalize,
    ops.Translate: fixed_arity("TRANSLATE", 3),
    ops.Repeat: fixed_arity("REPEAT", 2),
    ops.RegexSearch: _regex_search,
    ops.RegexExtract: _regex_extract,
    ops.RegexReplace: _regex_replace,
    ops.GroupConcat: reduction("STRING_AGG"),
    ops.Cast: _cast,
    ops.StructField: _struct_field,
    ops.StructColumn: _struct_column,
    ops.ArrayCollect: _array_agg,
    ops.ArrayConcat: _array_concat,
    ops.ArrayColumn: _array_column,
    ops.ArrayIndex: _array_index,
    ops.ArrayLength: unary("ARRAY_LENGTH"),
    ops.ArrayRepeat: _array_repeat,
    ops.ArraySlice: _array_slice,
    ops.Log: _log,
    ops.Log2: _log2,
    ops.Arbitrary: _arbitrary,
    # Geospatial Columnar
    ops.GeoUnaryUnion: unary("ST_UNION_AGG"),
    # Geospatial
    ops.GeoArea: unary("ST_AREA"),
    ops.GeoAsBinary: unary("ST_ASBINARY"),
    ops.GeoAsText: unary("ST_ASTEXT"),
    ops.GeoAzimuth: fixed_arity("ST_AZIMUTH", 2),
    ops.GeoBuffer: fixed_arity("ST_BUFFER", 2),
    ops.GeoCentroid: unary("ST_CENTROID"),
    ops.GeoContains: fixed_arity("ST_CONTAINS", 2),
    ops.GeoCovers: fixed_arity("ST_COVERS", 2),
    ops.GeoCoveredBy: fixed_arity("ST_COVEREDBY", 2),
    ops.GeoDWithin: fixed_arity("ST_DWITHIN", 3),
    ops.GeoDifference: fixed_arity("ST_DIFFERENCE", 2),
    ops.GeoDisjoint: fixed_arity("ST_DISJOINT", 2),
    ops.GeoDistance: fixed_arity("ST_DISTANCE", 2),
    ops.GeoEndPoint: unary("ST_ENDPOINT"),
    ops.GeoEquals: fixed_arity("ST_EQUALS", 2),
    ops.GeoGeometryType: unary("ST_GEOMETRYTYPE"),
    ops.GeoIntersection: fixed_arity("ST_INTERSECTION", 2),
    ops.GeoIntersects: fixed_arity("ST_INTERSECTS", 2),
    ops.GeoLength: unary("ST_LENGTH"),
    ops.GeoMaxDistance: fixed_arity("ST_MAXDISTANCE", 2),
    ops.GeoNPoints: unary("ST_NUMPOINTS"),
    ops.GeoPerimeter: unary("ST_PERIMETER"),
    ops.GeoPoint: fixed_arity("ST_GEOGPOINT", 2),
    ops.GeoPointN: fixed_arity("ST_POINTN", 2),
    ops.GeoSimplify: _geo_simplify,
    ops.GeoStartPoint: unary("ST_STARTPOINT"),
    ops.GeoTouches: fixed_arity("ST_TOUCHES", 2),
    ops.GeoUnion: fixed_arity("ST_UNION", 2),
    ops.GeoWithin: fixed_arity("ST_WITHIN", 2),
    ops.GeoX: unary("ST_X"),
    ops.GeoXMax: _geo_boundingbox("xmax"),
    ops.GeoXMin: _geo_boundingbox("xmin"),
    ops.GeoY: unary("ST_Y"),
    ops.GeoYMax: _geo_boundingbox("ymax"),
    ops.GeoYMin: _geo_boundingbox("ymin"),
    ops.BitAnd: reduction("BIT_AND"),
    ops.BitOr: reduction("BIT_OR"),
    ops.BitXor: reduction("BIT_XOR"),
    ops.ApproxCountDistinct: reduction("APPROX_COUNT_DISTINCT"),
    ops.ApproxMedian: compiles_approx,
    ops.IdenticalTo: _identical_to,
    ops.FloorDivide: _floor_divide,
    ops.IsNan: _is_nan,
    ops.IsInf: _is_inf,
    ops.ArgMin: _arg_min_max("ASC"),
    ops.ArgMax: _arg_min_max("DESC"),
    ops.Pi: lambda *_: "ACOS(-1)",
    ops.E: lambda *_: "EXP(1)",
    ops.RandomScalar: fixed_arity("RAND", 0),
    ops.NthValue: _nth_value,
    ops.JSONGetItem: lambda t, op: f"{t.translate(op.arg)}[{t.translate(op.index)}]",
    ops.ArrayStringJoin: lambda t, op: f"ARRAY_TO_STRING({t.translate(op.arg)}, {t.translate(op.sep)})",
    ops.StartsWith: fixed_arity("STARTS_WITH", 2),
    ops.EndsWith: fixed_arity("ENDS_WITH", 2),
}

_invalid_operations = {
    ops.FindInSet,
    ops.DateDiff,
    ops.TimestampDiff,
    ops.ExtractAuthority,
    ops.ExtractFile,
    ops.ExtractFragment,
    ops.ExtractHost,
    ops.ExtractPath,
    ops.ExtractProtocol,
    ops.ExtractQuery,
    ops.ExtractUserInfo,
}

OPERATION_REGISTRY = {
    k: v for k, v in OPERATION_REGISTRY.items() if k not in _invalid_operations
}
