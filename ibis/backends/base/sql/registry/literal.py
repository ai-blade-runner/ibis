from __future__ import annotations

import datetime
import math

import ibis.expr.types as ir


def _set_literal_format(translator, expr):
    value_type = expr.type().value_type

    formatted = [
        translator.translate(ir.literal(x, type=value_type)) for x in expr.op().value
    ]

    return '(' + ', '.join(formatted) + ')'


def _boolean_literal_format(translator, op):
    return 'TRUE' if op.value else 'FALSE'


def _string_literal_format(translator, op):
    return "'{}'".format(op.value.replace("'", "\\'"))


def _number_literal_format(translator, op):
    if math.isfinite(op.value):
        formatted = repr(op.value)
    else:
        if math.isnan(op.value):
            formatted_val = 'NaN'
        elif math.isinf(op.value):
            if op.value > 0:
                formatted_val = 'Infinity'
            else:
                formatted_val = '-Infinity'
        formatted = f"CAST({formatted_val!r} AS DOUBLE)"

    return formatted


def _interval_literal_format(translator, op):
    return f'INTERVAL {op.value} {op.output_dtype.resolution.upper()}'


def _date_literal_format(translator, op):
    value = op.value
    if isinstance(value, datetime.date):
        value = value.strftime('%Y-%m-%d')

    return repr(value)


def _timestamp_literal_format(translator, op):
    value = op.value
    if isinstance(value, datetime.datetime):
        value = value.isoformat()

    return repr(value)


literal_formatters = {
    'boolean': _boolean_literal_format,
    'number': _number_literal_format,
    'string': _string_literal_format,
    'interval': _interval_literal_format,
    'timestamp': _timestamp_literal_format,
    'date': _date_literal_format,
    'set': _set_literal_format,
}


def literal(translator, op):
    """Return the expression as its literal value."""

    dtype = op.output_dtype

    if op.value is None:
        return "NULL"

    if dtype.is_boolean():
        typeclass = 'boolean'
    elif dtype.is_string():
        typeclass = 'string'
    elif dtype.is_date():
        typeclass = 'date'
    elif dtype.is_numeric():
        typeclass = 'number'
    elif dtype.is_timestamp():
        typeclass = 'timestamp'
    elif dtype.is_interval():
        typeclass = 'interval'
    elif dtype.is_set():
        typeclass = 'set'
    else:
        raise NotImplementedError(f'Unsupported type: {dtype!r}')

    return literal_formatters[typeclass](translator, op)


def null_literal(translator, expr):
    return 'NULL'
