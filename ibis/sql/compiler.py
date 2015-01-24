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

# An Ibis analytical expression will typically consist of a primary SELECT
# statement, with zero or more supporting DDL queries. For example we would
# want to support converting a text file in HDFS to a Parquet-backed Impala
# table, with optional teardown if the user wants the intermediate converted
# table to be temporary.


from io import BytesIO

import ibis.expr.base as ir
import ibis.common as com
import ibis.util as util


#----------------------------------------------------------------------
# The QueryContext (temporary name) will store useful information like table
# alias names for converting value expressions to SQL.


class QueryContext(object):

    """

    """

    def __init__(self):
        self.table_aliases = {}

    def _get_table_key(self, table):
        if isinstance(table, ir.TableExpr):
            table = table.op()
        return id(table)

    def make_alias(self, table_expr):
        i = len(self.table_aliases)
        alias = 't%d' % i
        self.set_alias(table_expr, alias)

    def has_alias(self, table_expr):
        key = self._get_table_key(table_expr)
        return key in self.table_aliases

    def need_aliases(self):
        return len(self.table_aliases) > 1

    def set_alias(self, table_expr, alias):
        key = self._get_table_key(table_expr)
        self.table_aliases[key] = alias

    def get_alias(self, table_expr):
        """
        Get the alias being used throughout a query to refer to a particular
        table or inline view
        """
        key = self._get_table_key(table_expr)
        return self.table_aliases.get(key)


#----------------------------------------------------------------------


class Select(object):

    """
    A SELECT statement which, after execution, might yield back to the user a
    table, array/list, or scalar value, depending on the expression that
    generated it
    """

    def __init__(self, table_set, select_set, where=None, group_by=None,
                 order_by=None, limit=None, having=None, subqueries=None,
                 parent_expr=None, indent=2):
        self.select_set = select_set
        self.table_set = table_set

        self.where = where or []

        # Group keys and post-predicates for aggregations
        self.group_by = group_by or []
        self.having = having or []
        self.order_by = order_by or []

        self.limit = limit
        self.parent_expr = parent_expr
        self.subqueries = []

        self.indent = indent

    def equals(self, other):
        if not isinstance(other, Select):
            return False

        this_exprs = self._all_exprs()
        other_exprs = other._all_exprs()

        if self.limit != other.limit:
            return False

        for x, y in zip(this_exprs, other_exprs):
            if not x.equals(y):
                return False

        return True

    def _all_exprs(self):
        # Gnarly, maybe we can improve this somehow
        expr_attrs = ['select_set', 'table_set', 'where', 'group_by', 'having',
                      'order_by', 'subqueries']
        exprs = []
        for attr in expr_attrs:
            val = getattr(self, attr)
            if isinstance(val, list):
                exprs.extend(val)
            else:
                exprs.append(val)

        return exprs

    def compile(self, context=None, semicolon=False):
        """

        """
        if context is None:
            context = QueryContext()

        self.populate_context(context)

        # If any subqueries, translate them and add to beginning of query as
        # part of the WITH section
        with_frag = self.format_subqueries(context)

        # SELECT
        select_frag = self.format_select_set(context)

        # FROM, JOIN, UNION
        from_frag = self.format_table_set(context)

        # WHERE
        where_frag = self.format_where(context)

        # GROUP BY and HAVING
        groupby_frag = self.format_group_by(context)

        # ORDER BY and LIMIT
        order_frag = self.format_postamble(context)

        # Glue together the query fragments and return
        query = _join_not_none('\n', [with_frag, select_frag, from_frag,
                                      where_frag, groupby_frag, order_frag])

        return query

    def populate_context(self, context):
        # Populate aliases for the distinct relations used to output this
        # select statement. For now we're going to assume they're either in the
        # table set or the subqueries.
        for query in self.subqueries:
            query.populate_context(context)

        roots = self.table_set._root_tables()
        for table in roots:
            context.make_alias(table)

    def format_subqueries(self, context):
        pass

    def format_select_set(self, context):
        # TODO:
        formatted = []
        for expr in self.select_set:
            if isinstance(expr, ir.ValueExpr):
                expr_str = translate_expr(expr, context=context, named=True)
            elif isinstance(expr, ir.TableExpr):
                # A * selection, possibly prefixed
                if context.need_aliases():
                    expr_str = '{}.*'.format(context.get_alias(expr))
                else:
                    expr_str = '*'
            formatted.append(expr_str)

        buf = BytesIO()
        line_length = 0
        max_length = 70

        for i, val in enumerate(formatted):
            # always line-break for multi-line expressions
            if val.count('\n'):
                if i: buf.write(',')
                buf.write('\n')
                buf.write(util.indent(val, self.indent))
                buf.write('\n')
                line_length = 0
            elif line_length and len(val) + line_length > max_length:
                # There is an expr, and adding this new one will make the line
                # too long
                buf.write(',\n') if i else buf.write('\n')
                buf.write(val)
                line_length = len(val)
            else:
                buf.write(', ') if i else None
                buf.write(val)
                line_length += len(val) + 2

        return 'SELECT {}'.format(buf.getvalue())

    def format_table_set(self, ctx):
        fragment = 'FROM '

        op = self.table_set.op()

        if isinstance(op, ir.Join):
            helper = _JoinFormatter(ctx, self.table_set)
            fragment += helper.get_result()
        elif isinstance(op, ir.TableNode):
            fragment += _format_table(ctx, self.table_set)

        return fragment

    def format_group_by(self, context):
        if len(self.group_by) == 0:
            # There is no aggregation, nothing to see here
            return None

        # Verify that the group by exprs match the first few tokens in the
        # select set
        for i, expr in enumerate(self.group_by):
            if expr is not self.select_set[i]:
                raise com.InternalError('Select was improperly formed')

        clause = 'GROUP BY {}'.format(', '.join([
            str(x + 1) for x in range(len(self.group_by))]))

        # TODO having

        return clause

    def format_where(self, context):
        if len(self.where) == 0:
            return None

        buf = BytesIO()
        buf.write('WHERE ')
        fmt_preds = [translate_expr(pred, context=context)
                     for pred in self.where]
        conj = ' AND\n{}'.format(' ' * 6)
        buf.write(conj.join(fmt_preds))
        return buf.getvalue()

    def format_postamble(self, context):
        buf = BytesIO()
        lines = 0

        if len(self.order_by) > 0:
            buf.write('ORDER BY ')
            formatted = []
            for key in self.order_by:
                translated = translate_expr(key.expr, context=context)
                if not key.ascending:
                    translated += ' DESC'
                formatted.append(translated)
            buf.write(', '.join(formatted))
            lines += 1

        if self.limit is not None:
            if lines:
                buf.write('\n')
            n, offset = self.limit['n'], self.limit['offset']
            buf.write('LIMIT {}'.format(n))
            if offset is not None:
                buf.write(' OFFSET {}'.format(offset))
            lines += 1

        if not lines:
            return None

        return buf.getvalue()

    def adapt_result(self, result):
        if isinstance(self.parent_expr, ir.TableExpr):
            result_type = 'table'
        elif isinstance(self.parent_expr, ir.ArrayExpr):
            result_type = 'array'
        elif isinstance(self.parent_expr, ir.ScalarExpr):
            aresult_type = 'scalar'
        pass


def _format_table(ctx, expr):
    op = expr.op()
    name = op.name

    if name is None:
        raise com.RelationError('Table did not have a name: {!r}'.format(expr))

    if ctx.need_aliases():
        name += ' {}'.format(ctx.get_alias(expr))
    return name


class _JoinFormatter(object):
    _join_names = {
        ir.InnerJoin: 'INNER JOIN',
        ir.LeftJoin: 'LEFT OUTER JOIN',
        ir.RightJoin: 'RIGHT OUTER JOIN',
        ir.OuterJoin: 'FULL OUTER JOIN',
        ir.LeftAntiJoin: 'LEFT ANTI JOIN',
        ir.LeftSemiJoin: 'LEFT SEMI JOIN',
        ir.CrossJoin: 'CROSS JOIN'
    }

    def __init__(self, context, expr, indent=2):
        self.context = context
        self.expr = expr
        self.indent = indent

        self.join_tables = []
        self.join_types = []
        self.join_predicates = []

    def get_result(self):
        # Got to unravel the join stack; the nesting order could be
        # arbitrary, so we do a depth first search and push the join tokens
        # and predicates onto a flat list, then format them
        self._walk_join_tree(self.expr.op())

        # TODO: Now actually format the things
        buf = BytesIO()
        buf.write(self.join_tables[0])
        for jtype, table, preds in zip(self.join_types, self.join_tables[1:],
                                       self.join_predicates):
            buf.write('\n')
            buf.write(util.indent('{} {}'.format(jtype, table), self.indent))

            if len(preds):
                buf.write('\n')
                fmt_preds = [translate_expr(pred, context=self.context)
                             for pred in preds]
                conj = ' AND\n{}'.format(' ' * 3)
                fmt_preds = util.indent('ON ' + conj.join(fmt_preds),
                                        self.indent * 2)
                buf.write(fmt_preds)

        return buf.getvalue()

    def _walk_join_tree(self, op):
        left = op.left.op()
        right = op.right.op()

        if util.all_of([left, right], ir.Join):
            raise NotImplementedError('Do not support joins between '
                                      'joins yet')

        jname = self._join_names[type(op)]

        # Read off tables and join predicates left-to-right in
        # depth-first order
        if isinstance(left, ir.Join):
            self._walk_join_tree(left)
            self.join_tables.append(self._format_table(op.right))
            self.join_types.append(jname)
            self.join_predicates.append(op.predicates)
        elif isinstance(right, ir.Join):
            # When rewrites are possible at the expression IR stage, we should
            # do them. Otherwise subqueries might be necessary in some cases
            # here
            raise NotImplementedError('not allowing joins on right '
                                      'side yet')
        else:
            # Both tables
            self.join_tables.append(self._format_table(op.left))
            self.join_tables.append(self._format_table(op.right))
            self.join_types.append(jname)
            self.join_predicates.append(op.predicates)

    def _format_table(self, expr):
        return _format_table(self.context, expr)



def _join_not_none(sep, pieces):
    pieces = [x for x in pieces if x is not None]
    return sep.join(pieces)


class QueryASTBuilder(object):

    """
    Transforms expression IR to a query pipeline (potentially multiple
    queries). There will typically be a primary SELECT query, perhaps with some
    subqueries and other DDL to ingest and tear down intermediate data sources.

    Walks the expression tree and catalogues distinct query units, builds
    select statements (and other DDL types, where necessary), and records
    relevant query unit aliases to be used when actually generating SQL.
    """

    def __init__(self, expr, context=None):
        self.expr = expr

        if context is None:
            context = QueryContext()

        self.substitute_memo = {}
        self.base_expr = ir.substitute_parents(self.expr, self.substitute_memo)

        self.context = context
        self.queries = []

    def get_result(self):
        # make idempotent
        if len(self.queries) > 0:
            return self._wrap_result()

        # Generate other kinds of DDL statements that may be required to
        # execute the passed query. For example, loding
        setup_queries = self._generate_setup_queries()

        # Make DDL statements to be executed after the main primary select
        # statement(s)
        teardown_queries = self._generate_teardown_queries()

        self.queries.extend(setup_queries)
        self.queries.append(self._build_select())
        self.queries.extend(teardown_queries)

        return self._wrap_result()

    def _wrap_result(self):
        return QueryAST(self.context, self.queries)

    def _build_select(self):
        # If expr is a ValueExpr, we must seek out the TableExprs that it
        # references, build their ASTs, and mark them in our QueryContext

        # For now, we need to make the simplifying assumption that a value
        # expression that is being translated only depends on a single table
        # expression.
        source_table = self._get_source_table_expr()

        modifiers = ir.collect_modifiers(source_table)

        # The base expression could be one or more types of operations now. It
        # could be a
        # - Projection
        # - Aggregation
        # - Unmaterialized join, without projection, needing materialization
        # - Materialized join
        # - An unmodified table

        # HACK: Shed filters on top of whatever is the root operation.
        base_expr = self.base_expr
        base_node = base_expr.op()
        while isinstance(base_node, (ir.Filter, ir.Limit, ir.SortBy)):
            base_expr = base_node.table
            base_node = base_expr.op()

        # hm, is this the best place for this?
        if isinstance(base_node, ir.Join):
            if not isinstance(base_node, ir.MaterializedJoin):
                # Unmaterialized join
                materialized = self.base_expr.materialize()
                base_node = materialized.op()

        if isinstance(base_node, ir.SelfReference):
            base_expr = base_node.table
            base_node = base_expr.op()

        group_by = None
        having = None
        if isinstance(base_node, ir.Projection):
            select_set = base_node.selections
            table_set = base_node.table
        elif isinstance(base_node, ir.Aggregation):
            # The select set includes the grouping keys (if any), and these are
            # duplicated in the group_by set. SQL translator can decide how to
            # format these depending on the database. Most likely the
            # GROUP BY 1, 2, ... style
            group_by = base_node.by
            having = base_node.having
            select_set = group_by + base_node.agg_exprs
            table_set = base_node.table
        elif isinstance(base_node, ir.MaterializedJoin):
            select_set = [base_node.left, base_node.left]
            table_set = base_expr
        elif isinstance(base_node, ir.PhysicalTable):
            select_set = [base_expr]
            table_set = base_expr
        else:
            raise NotImplementedError

        return Select(table_set, select_set, where=modifiers['filters'],
                      group_by=group_by,
                      having=having, limit=modifiers['limit'],
                      order_by=modifiers['sort_by'],
                      parent_expr=self.expr)

    def _generate_setup_queries(self):
        return []

    def _generate_teardown_queries(self):
        return []

    def _get_source_table_expr(self):
        if isinstance(self.expr, ir.TableExpr):
            return self.expr

        node = self.expr.op()

        # First table expression observed for each argument that the expr
        # depends no
        first_tables = []
        def push_first(arg):
            if isinstance(arg, (tuple, list)):
                [push_first(x) for x in arg]
                return

            if not isinstance(arg, ir.Expr):
                return
            if isinstance(arg, ir.TableExpr):
                first_tables.append(arg)
            else:
                collect(arg.op())

        def collect(node):
            for arg in node.args:
                push_first(arg)

        collect(node)
        return util.unique_by_key(first_tables, id)

    def _visit_aggregate(self):
        pass

    def _visit_projection(self):
        pass

    def _visit_filter(self):
        pass

    def _visit_value_op(self):
        pass

    def _walk_arg(self):
        pass



class QueryAST(object):

    def __init__(self, context, queries):
        self.context = context
        self.queries = queries


def build_ast(expr):
    builder = QueryASTBuilder(expr)
    return builder.get_result()


#----------------------------------------------------------------------
# Scalar and array expression formatting

_sql_type_names = {
    'int8': 'tinyint',
    'int16': 'smallint',
    'int32': 'int',
    'int64': 'bigint',
    'float': 'float',
    'double': 'double',
    'string': 'string',
    'boolean': 'boolean'
}

def _cast(translator, expr):
    op = expr.op()
    arg = translator.translate(op.value_expr)
    sql_type = _sql_type_names[op.target_type]
    return 'CAST({!s} AS {!s})'.format(arg, sql_type)


def _is_null(translator, expr):
    formatted_arg = translator.translate(expr.op().arg)
    return '{!s} IS NULL'.format(formatted_arg)


def _not_null(translator, expr):
    formatted_arg = translator.translate(expr.op().arg)
    return '{!s} IS NOT NULL'.format(formatted_arg)


def _negate(translator, expr):
    arg = expr.op().arg
    formatted_arg = translator.translate(arg)
    if isinstance(expr, ir.BooleanValue):
        return 'NOT {!s}'.format(formatted_arg)
    else:
        if _needs_parens(arg):
            formatted_arg = _parenthesize(formatted_arg)
        return '-{!s}'.format(formatted_arg)


def _parenthesize(what):
    return '({!s})'.format(what)


def _unary_op(func_name):
    def formatter(translator, expr):
        arg = translator.translate(expr.op().arg)
        return '{!s}({!s})'.format(func_name, arg)
    return formatter


def _binary_infix_op(infix_sym):
    def formatter(translator, expr):
        op = expr.op()

        left_arg = translator.translate(op.left)
        right_arg = translator.translate(op.right)

        if _needs_parens(op.left):
            left_arg = _parenthesize(left_arg)

        if _needs_parens(op.right):
            right_arg = _parenthesize(right_arg)

        return '{!s} {!s} {!s}'.format(left_arg, infix_sym, right_arg)
    return formatter


def _xor(translator, expr):
    op = expr.op()

    left_arg = translator.translate(op.left)
    right_arg = translator.translate(op.right)

    if _needs_parens(op.left):
        left_arg = _parenthesize(left_arg)

    if _needs_parens(op.right):
        right_arg = _parenthesize(right_arg)

    return ('{0} AND NOT {1}'
            .format('({0} {1} {2})'.format(left_arg, 'OR', right_arg),
                    '({0} {1} {2})'.format(left_arg, 'AND', right_arg)))


def _name_expr(formatted_expr, quoted_name):
    return '{!s} AS {!s}'.format(formatted_expr, quoted_name)


def _needs_parens(op):
    if isinstance(op, ir.Expr):
        op = op.op()
    op_klass = type(op)
    # function calls don't need parens
    return (op_klass in _binary_infix_ops or
            op_klass in [ir.Negate])


def _need_parenthesize_args(op):
    if isinstance(op, ir.Expr):
        op = op.op()
    op_klass = type(op)
    return (op_klass in _binary_infix_ops or
            op_klass in [ir.Negate])


def _boolean_literal_format(expr):
    value = expr.op().value
    return 'TRUE' if value else 'FALSE'


def _number_literal_format(expr):
    value = expr.op().value
    return repr(value)


def _string_literal_format(expr):
    value = expr.op().value
    return "'{!s}'".format(value.replace("'", "\\'"))


def _quote_field(name, quotechar='`'):
    if name.count(' '):
        return '{0}{1}{0}'.format(quotechar, name)
    else:
        return name


_literal_formatters = {
    'boolean': _boolean_literal_format,
    'number': _number_literal_format,
    'string': _string_literal_format
}


_unary_ops = {
    # Unary operations
    ir.NotNull: _not_null,
    ir.IsNull: _is_null,
    ir.Negate: _negate,
    ir.Exp: _unary_op('exp'),
    ir.Sqrt: _unary_op('sqrt'),
    ir.Log: _unary_op('log'),
    ir.Log2: _unary_op('log2'),
    ir.Log10: _unary_op('log10'),

    # Unary aggregates
    ir.Mean: _unary_op('avg'),
    ir.Sum: _unary_op('sum')
}

_binary_infix_ops = {
    # Binary operations
    ir.Add: _binary_infix_op('+'),
    ir.Subtract: _binary_infix_op('-'),
    ir.Multiply: _binary_infix_op('*'),
    ir.Divide: _binary_infix_op('/'),
    ir.Power: _binary_infix_op('^'),
    ir.And: _binary_infix_op('AND'),
    ir.Or: _binary_infix_op('OR'),
    ir.Xor: _xor,
    ir.Equals: _binary_infix_op('='),
    ir.NotEquals: _binary_infix_op('!='),
    ir.GreaterEqual: _binary_infix_op('>='),
    ir.Greater: _binary_infix_op('>'),
    ir.LessEqual: _binary_infix_op('<='),
    ir.Less: _binary_infix_op('<')
}

_other_ops = {
    ir.Cast: _cast
}


_operation_registry = {}
_operation_registry.update(_unary_ops)
_operation_registry.update(_binary_infix_ops)
_operation_registry.update(_other_ops)


class ExprTranslator(object):

    def __init__(self, expr, context=None, named=False):
        self.expr = expr

        if context is None:
            context = QueryContext()
        self.context = context

        # For now, governing whether the result will have a name
        self.named = named

    def get_result(self):
        """
        Build compiled SQL expression from the bottom up and return as a string
        """
        translated = self.translate(self.expr)
        if self._needs_name(self.expr):
            # TODO: this could fail in various ways
            name = self.expr.get_name()
            translated = _name_expr(translated, _quote_field(name))
        return translated

    def _needs_name(self, expr):
        if not self.named:
            return False

        op = expr.op()
        if isinstance(op, ir.TableColumn):
            # This column has been given an explicitly different name
            if expr.get_name() != op.name:
                return True
            return False

        return True

    def translate(self, expr):
        # The operation node type the typed expression wraps
        op = expr.op()

        if isinstance(op, ir.Literal):
            return self._trans_literal(expr)
        elif isinstance(op, ir.Parameter):
            return self._trans_param(expr)
        elif isinstance(op, ir.TableColumn):
            return self._trans_column_ref(expr)
        elif type(op) in _operation_registry:
            formatter = _operation_registry[type(op)]
            return formatter(self, expr)
        else:
            raise NotImplementedError('No translator rule for {0}'.format(op))

    def _trans_literal(self, expr):
        if isinstance(expr, ir.BooleanValue):
            typeclass = 'boolean'
        elif isinstance(expr, ir.StringValue):
            typeclass = 'string'
        elif isinstance(expr, ir.NumericValue):
            typeclass = 'number'
        else:
            raise NotImplementedError

        return _literal_formatters[typeclass](expr)

    def _trans_param(self, expr):
        raise NotImplementedError

    def _trans_column_ref(self, expr):
        op = expr.op()
        field_name = _quote_field(op.name)

        if self.context.need_aliases():
            alias = self.context.get_alias(op.table)
            if alias is not None:
                field_name = '{0}.{1}'.format(alias, field_name)

        return field_name


def translate_expr(expr, context=None, named=False):
    translator = ExprTranslator(expr, context=context, named=named)
    return translator.get_result()

def _get_query(expr):
    ast = build_ast(expr)
    return ast.queries[0]

def to_sql(expr):
    query = _get_query(expr)
    return query.compile()