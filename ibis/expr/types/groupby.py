# Copyright 2014 Cloudera Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""User API for grouping operations."""

from __future__ import annotations

import itertools
import types
from typing import Iterable, Sequence

import ibis
import ibis.expr.analysis as an
import ibis.expr.operations as ops
import ibis.expr.types as ir
from ibis import util
from ibis.expr.deferred import Deferred
from ibis.selectors import Selector

_function_types = tuple(
    filter(
        None,
        (
            types.BuiltinFunctionType,
            types.BuiltinMethodType,
            types.FunctionType,
            types.LambdaType,
            types.MethodType,
            getattr(types, "UnboundMethodType", None),
        ),
    )
)


def _get_group_by_key(table, value):
    if isinstance(value, str):
        yield table[value]
    elif isinstance(value, _function_types):
        yield value(table)
    elif isinstance(value, Deferred):
        yield value.resolve(table)
    elif isinstance(value, Selector):
        yield from value.expand(table)
    elif isinstance(value, ir.Expr):
        yield an.sub_immediate_parents(value.op(), table.op()).to_expr()
    else:
        yield value


# TODO(kszucs): make a builder class for this
class GroupedTable:
    """An intermediate table expression to hold grouping information."""

    def __init__(
        self, table, by, having=None, order_by=None, window=None, **expressions
    ):
        self.table = table
        self.by = list(
            itertools.chain(
                itertools.chain.from_iterable(
                    _get_group_by_key(table, v) for v in util.promote_list(by)
                ),
                (
                    expr.name(k)
                    for k, v in expressions.items()
                    for expr in _get_group_by_key(table, v)
                ),
            )
        )
        self._order_by = order_by or []
        self._having = having or []
        self._window = window

    def __getitem__(self, args):
        # Shortcut for projection with window functions
        return self.select(*args)

    def __getattr__(self, attr):
        if hasattr(self.table, attr):
            return self._column_wrapper(attr)

        raise AttributeError("GroupBy has no attribute %r" % attr)

    def _column_wrapper(self, attr):
        col = self.table[attr]
        if isinstance(col, ir.NumericValue):
            return GroupedNumbers(col, self)
        else:
            return GroupedArray(col, self)

    def aggregate(self, metrics=None, **kwds):
        """Compute aggregates over a group by."""
        return self.table.aggregate(metrics, by=self.by, having=self._having, **kwds)

    agg = aggregate

    def having(self, expr: ir.BooleanScalar) -> GroupedTable:
        """Add a post-aggregation result filter `expr`.

        !!! warning "Expressions like `x is None` return `bool` and **will not** generate a SQL comparison to `NULL`"

        Parameters
        ----------
        expr
            An expression that filters based on an aggregate value.

        Returns
        -------
        GroupedTable
            A grouped table expression
        """
        return self.__class__(
            self.table,
            self.by,
            having=self._having + util.promote_list(expr),
            order_by=self._order_by,
            window=self._window,
        )

    def order_by(self, expr: ir.Value | Iterable[ir.Value]) -> GroupedTable:
        """Sort a grouped table expression by `expr`.

        Notes
        -----
        This API call is ignored in aggregations.

        Parameters
        ----------
        expr
            Expressions to order the results by

        Returns
        -------
        GroupedTable
            A sorted grouped GroupedTable
        """
        return self.__class__(
            self.table,
            self.by,
            having=self._having,
            order_by=self._order_by + util.promote_list(expr),
            window=self._window,
        )

    def mutate(self, *exprs: ir.Value | Sequence[ir.Value], **kwexprs: ir.Value):
        """Return a table projection with window functions applied.

        Any arguments can be functions.

        Parameters
        ----------
        exprs
            List of expressions
        kwexprs
            Expressions

        Examples
        --------
        >>> import ibis
        >>> t = ibis.table([
        ...     ('foo', 'string'),
        ...     ('bar', 'string'),
        ...     ('baz', 'double'),
        ... ], name='t')
        >>> t
        UnboundTable: t
          foo string
          bar string
          baz float64
        >>> expr = (t.group_by('foo')
        ...          .order_by(ibis.desc('bar'))
        ...          .mutate(qux=lambda x: x.baz.lag(), qux2=t.baz.lead()))
        >>> print(expr)
        r0 := UnboundTable: t
          foo string
          bar string
          baz float64
        Selection[r0]
          selections:
            r0
            qux:  WindowFunction(...)
            qux2: WindowFunction(...)

        Returns
        -------
        Table
            A table expression with window functions applied
        """

        exprs = self._selectables(*exprs, **kwexprs)
        return self.table.mutate(exprs)

    def select(self, *exprs, **kwexprs):
        """Project new columns out of the grouped table.

        See Also
        --------
        [`GroupedTable.mutate`][ibis.expr.types.groupby.GroupedTable.mutate]
        """
        exprs = self._selectables(*exprs, **kwexprs)
        return self.table.select(exprs)

    def _selectables(self, *exprs, **kwexprs):
        """Project new columns out of the grouped table.

        See Also
        --------
        [`GroupedTable.mutate`][ibis.expr.types.groupby.GroupedTable.mutate]
        """
        table = self.table
        default_frame = self._get_window()
        return [
            an.windowize_function(e2, frame=default_frame)
            for expr in exprs
            for e1 in util.promote_list(expr)
            for e2 in util.promote_list(table._ensure_expr(e1))
        ] + [
            an.windowize_function(e, frame=default_frame).name(k)
            for k, expr in kwexprs.items()
            for e in util.promote_list(table._ensure_expr(expr))
        ]

    projection = select

    def _get_window(self):
        if self._window is None:
            return ops.RowsWindowFrame(
                table=self.table,
                group_by=self.by,
                order_by=self._order_by,
            )
        else:
            return self._window.copy(
                groupy_by=self._window.group_by + self.by,
                order_by=self._window.order_by + self._order_by,
            )

    def over(
        self,
        window=None,
        *,
        rows=None,
        range=None,
        group_by=None,
        order_by=None,
    ) -> GroupedTable:
        """Apply a window over the input expressions.

        Parameters
        ----------
        window
            Window to add to the input
        rows
            Whether to use the `ROWS` window clause
        range
            Whether to use the `RANGE` window clause
        group_by
            Grouping key
        order_by
            Ordering key

        Returns
        -------
        GroupedTable
            A new grouped table expression
        """
        if window is None:
            window = ibis.window(
                rows=rows,
                range=range,
                group_by=group_by,
                order_by=order_by,
            )

        return self.__class__(
            self.table,
            self.by,
            having=self._having,
            order_by=self._order_by,
            window=window,
        )

    def count(self, metric_name: str = 'count') -> ir.Table:
        """Computing the number of rows per group.

        Parameters
        ----------
        metric_name
            Name to use for the row count metric

        Returns
        -------
        Table
            The aggregated table
        """
        metric = self.table.count().name(metric_name)
        return self.table.aggregate([metric], by=self.by, having=self._having)

    size = count


def _group_agg_dispatch(name):
    def wrapper(self, *args, **kwargs):
        f = getattr(self.arr, name)
        metric = f(*args, **kwargs)
        alias = f'{name}({self.arr.get_name()})'
        return self.parent.aggregate(metric.name(alias))

    wrapper.__name__ = name
    return wrapper


class GroupedArray:
    def __init__(self, arr, parent):
        self.arr = arr
        self.parent = parent

    count = _group_agg_dispatch('count')
    size = count
    min = _group_agg_dispatch('min')
    max = _group_agg_dispatch('max')
    approx_nunique = _group_agg_dispatch('approx_nunique')
    approx_median = _group_agg_dispatch('approx_median')
    group_concat = _group_agg_dispatch('group_concat')


class GroupedNumbers(GroupedArray):
    mean = _group_agg_dispatch('mean')
    sum = _group_agg_dispatch('sum')
