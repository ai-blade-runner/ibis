from __future__ import annotations

from typing import TYPE_CHECKING

from public import public

import ibis.expr.operations as ops
from ibis.expr.types.core import _binop
from ibis.expr.types.numeric import NumericColumn, NumericScalar, NumericValue

if TYPE_CHECKING:
    import ibis.expr.types as ir


@public
class BooleanValue(NumericValue):
    def ifelse(
        self,
        true_expr: ir.Value,
        false_expr: ir.Value,
    ) -> ir.Value:
        """Construct a ternary conditional expression.

        Parameters
        ----------
        true_expr
            Expression to return if `self` evaluates to `True`
        false_expr
            Expression to return if `self` evaluates to `False`

        Returns
        -------
        Value
            The value of `true_expr` if `arg` is `True` else `false_expr`

        Examples
        --------
        >>> import ibis
        >>> t = ibis.table([("is_person", "boolean")], name="t")
        >>> expr = t.is_person.ifelse("yes", "no")
        >>> print(ibis.impala.compile(expr.name("tmp")))
        SELECT if(t0.`is_person`, 'yes', 'no') AS `tmp`
        FROM t t0
        """
        # Result will be the result of promotion of true/false exprs. These
        # might be conflicting types; same type resolution as case expressions
        # must be used.
        return ops.Where(self, true_expr, false_expr).to_expr()

    def __and__(self, other: BooleanValue) -> BooleanValue:
        """Construct a binary AND conditional expression with `self` and `other`.


        Parameters
        ----------
        self
            Left operand
        other
            Right operand

        Returns
        -------
        BooleanValue
            A Boolean expression

        Examples
        --------
        >>> import ibis
        >>> ibis.options.interactive = True
        >>> t = ibis.memtable({"arr": [[1], [], [42, 42], None]})
        >>> t.arr.contains(42) & (t.arr.contains(1))
        ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
        ┃ And(ArrayContains(arr, 42), ArrayContains(arr, 1)) ┃
        ┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
        │ boolean                                            │
        ├────────────────────────────────────────────────────┤
        │ False                                              │
        │ False                                              │
        │ False                                              │
        │ NULL                                               │
        └────────────────────────────────────────────────────┘

        >>> t.arr.contains(42) & (t.arr.contains(42))
        ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
        ┃ And(ArrayContains(arr, 42), ArrayContains(arr, 42)) ┃
        ┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
        │ boolean                                             │
        ├─────────────────────────────────────────────────────┤
        │ False                                               │
        │ False                                               │
        │ True                                                │
        │ NULL                                                │
        └─────────────────────────────────────────────────────┘
        """
        return _binop(ops.And, self, other)

    __rand__ = __and__

    def __or__(self, other: BooleanValue) -> BooleanValue:
        """Construct a binary OR conditional expression with `self` and `other`.


        Parameters
        ----------
        self
            Left operand
        other
            Right operand

        Returns
        -------
        BooleanValue
            A Boolean expression

        Examples
        --------
        >>> import ibis
        >>> ibis.options.interactive = True
        >>> t = ibis.memtable({"arr": [1, 2, 3, None]})
        >>> (t.arr > 1) | (t.arr > 2)
        ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
        ┃ Or(Greater(arr, 1), Greater(arr, 2)) ┃
        ┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
        │ boolean                              │
        ├──────────────────────────────────────┤
        │ False                                │
        │ True                                 │
        │ True                                 │
        │ NULL                                 │
        └──────────────────────────────────────┘
        """
        return _binop(ops.Or, self, other)

    __ror__ = __or__

    def __xor__(self, other: BooleanValue) -> BooleanValue:
        """Construct a binary XOR conditional expression with `self` and `other`.


        Parameters
        ----------
        self
            Left operand
        other
            Right operand

        Returns
        -------
        BooleanValue
            A Boolean expression

        Examples
        --------
        >>> import ibis
        >>> ibis.options.interactive = True
        >>> t = ibis.memtable({"arr": [1, 2, 3, None]})
        >>> t.arr == 2
        ┏━━━━━━━━━━━━━━━━┓
        ┃ Equals(arr, 2) ┃
        ┡━━━━━━━━━━━━━━━━┩
        │ boolean        │
        ├────────────────┤
        │ False          │
        │ True           │
        │ False          │
        │ NULL           │
        └────────────────┘

        >>> (t.arr > 2)
        ┏━━━━━━━━━━━━━━━━━┓
        ┃ Greater(arr, 2) ┃
        ┡━━━━━━━━━━━━━━━━━┩
        │ boolean         │
        ├─────────────────┤
        │ False           │
        │ False           │
        │ True            │
        │ NULL            │
        └─────────────────┘

        >>> (t.arr == 2) ^ (t.arr > 2)
        ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
        ┃ Xor(Equals(arr, 2), Greater(arr, 2)) ┃
        ┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
        │ boolean                              │
        ├──────────────────────────────────────┤
        │ False                                │
        │ True                                 │
        │ True                                 │
        │ NULL                                 │
        └──────────────────────────────────────┘
        """

        return _binop(ops.Xor, self, other)

    __rxor__ = __xor__

    def __invert__(self) -> BooleanValue:
        """Construct a unary NOT conditional expression with `self`.


        Parameters
        ----------
        self
            Operand

        Returns
        -------
        BooleanValue
            A Boolean expression

        Examples
        --------
        >>> import ibis
        >>> ibis.options.interactive = True
        >>> t = ibis.memtable({"arr": [True, False, False, None]})
        >>> ~t.arr
        ┏━━━━━━━━━━┓
        ┃ Not(arr) ┃
        ┡━━━━━━━━━━┩
        │ boolean  │
        ├──────────┤
        │ False    │
        │ True     │
        │ True     │
        │ NULL     │
        └──────────┘
        """
        return self.negate()

    @staticmethod
    def __negate_op__():
        return ops.Not


@public
class BooleanScalar(NumericScalar, BooleanValue):
    pass


@public
class BooleanColumn(NumericColumn, BooleanValue):
    def any(self, where: BooleanValue | None = None) -> BooleanValue:
        """Return whether at least one element is `True`.

        Parameters
        ----------
        where
            Optional filter for the aggregation

        Returns
        -------
        Boolean Value

        Examples
        --------
        >>> import ibis
        >>> ibis.options.interactive = True
        >>> t = ibis.memtable({"arr": [1, 2, 3, None]})
        >>> (t.arr > 2).any()
        True
        >>> (t.arr > 4).any()
        False
        >>> m = ibis.memtable({"arr": [True, True, True, False]})
        >>> (t.arr == None).any(where=t.arr != None)
        False
        """
        import ibis.expr.analysis as an

        return an._make_any(self, ops.Any, where=where)

    def notany(self, where: BooleanValue | None = None) -> BooleanValue:
        """Return whether no elements are `True`.

        Parameters
        ----------
        where
            Optional filter for the aggregation

        Returns
        -------
        Boolean Value
        Examples
        --------
        >>> import ibis
        >>> ibis.options.interactive = True
        >>> t = ibis.memtable({"arr": [1, 2, 3, 4]})
        >>> (t.arr > 1).notany()
        False
        >>> (t.arr > 4).notany()
        True
        >>> m = ibis.memtable({"arr": [True, True, True, False]})
        >>> (t.arr == None).notany(where=t.arr != None)
        True
        """
        import ibis.expr.analysis as an

        return an._make_any(self, ops.NotAny, where=where)

    def all(self, where: BooleanValue | None = None) -> BooleanScalar:
        """Return whether all elements are `True`.

        Parameters
        ----------
        where
            Optional filter for the aggregation

        Returns
            -------
            Boolean Value

        Examples
        --------
        >>> import ibis
        >>> ibis.options.interactive = True
        >>> t = ibis.memtable({"arr": [1, 2, 3, 4]})
        >>> (t.arr >= 1).all()
        True
        >>> (t.arr > 2).all()
        False
        >>> (t.arr == 2).all(where=t.arr == 2)
        True
        >>> (t.arr == 2).all(where=t.arr >= 2)
        False

        """
        return ops.All(self, where=where).to_expr()

    def notall(self, where: BooleanValue | None = None) -> BooleanScalar:
        """Return whether not all elements are `True`.

        Parameters
        ----------
        where
            Optional filter for the aggregation

        Returns
        -------
        Boolean Value

        Examples
        --------
        >>> import ibis
        >>> ibis.options.interactive = True
        >>> t = ibis.memtable({"arr": [1, 2, 3, 4]})
        >>> (t.arr >= 1).notall()
        False
        >>> (t.arr > 2).notall()
        True
        >>> (t.arr == 2).notall(where=t.arr == 2)
        False
        >>> (t.arr == 2).notall(where=t.arr >= 2)
        True
        """
        return ops.NotAll(self, where=where).to_expr()

    def cumany(self) -> BooleanColumn:
        """Accumulate the `any` aggregate.

        Returns
        -------
        BooleanColumns

        Examples
        --------
        >>> import ibis
        >>> ibis.options.interactive = True
        >>> t = ibis.memtable({"arr": [1, 2, 3, 4]})
        >>> ((t.arr > 1) | (t.arr >= 1)).cumany()
        ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
        ┃ CumulativeAny(Or(Greater(arr, 1), GreaterEqual(arr, 1))) ┃
        ┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
        │ boolean                                                  │
        ├──────────────────────────────────────────────────────────┤
        │ True                                                     │
        │ True                                                     │
        │ True                                                     │
        │ True                                                     │
        └──────────────────────────────────────────────────────────┘

        >>> ((t.arr > 1) & (t.arr >= 1)).cumany()
        ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
        ┃ CumulativeAny(And(Greater(arr, 1), GreaterEqual(arr, 1))) ┃
        ┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
        │ boolean                                                   │
        ├───────────────────────────────────────────────────────────┤
        │ False                                                     │
        │ True                                                      │
        │ True                                                      │
        │ True                                                      │
        └───────────────────────────────────────────────────────────┘
        """
        return ops.CumulativeAny(self).to_expr()

    def cumall(self) -> BooleanColumn:
        """Accumulate the `all` aggregate.

        Returns
        -------
        BooleanColumns

        Examples
        --------
        >>> import ibis
        >>> ibis.options.interactive = True
        >>> t = ibis.memtable({"arr": [1, 2, 3, 4]})
        >>> ((t.arr > 1) & (t.arr >= 1)).cumall()
        ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
        ┃ CumulativeAll(And(Greater(arr, 1), GreaterEqual(arr, 1))) ┃
        ┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
        │ boolean                                                   │
        ├───────────────────────────────────────────────────────────┤
        │ False                                                     │
        │ False                                                     │
        │ False                                                     │
        │ False                                                     │
        └───────────────────────────────────────────────────────────┘

        >>> ((t.arr > 0) & (t.arr >= 1)).cumall()
        ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
        ┃ CumulativeAll(And(Greater(arr, 0), GreaterEqual(arr, 1))) ┃
        ┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
        │ boolean                                                   │
        ├───────────────────────────────────────────────────────────┤
        │ True                                                      │
        │ True                                                      │
        │ True                                                      │
        │ True                                                      │
        └───────────────────────────────────────────────────────────┘
        """
        return ops.CumulativeAll(self).to_expr()
