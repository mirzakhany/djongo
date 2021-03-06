from sqlparse import parse as sql_parse
from sqlparse import tokens
from sqlparse.sql import IdentifierList, \
    Identifier, Parenthesis, Where, Comparison, Token
from pymongo import ReturnDocument, ASCENDING, DESCENDING
from pymongo.cursor import Cursor as PymongoCursor
from pymongo.command_cursor import CommandCursor as PymongoCommandCursor
import re
import logging

logger = logging.getLogger(__name__)

OPERATOR_MAP = {
    '=': '$eq',
    '>': '$gt',
    '<': '$lt',
    '>=': '$gte',
    '<=': '$lte',
}

OPERATOR_PRECEDENCE = {
    'IN': 1,
    'NOT': 2,
    'AND': 3,
    'OR': 4,
    'generic': 50
}

ORDER_BY_MAP = {
    'ASC': ASCENDING,
    'DESC': DESCENDING
}


class SQLDecodeError(ValueError):
    pass


class Parse:

    def __init__(self, connection, sql, params):
        self.params = params
        logger.debug('params: {}'.format(params))
        self.p_index = -1
        self.sql = re.sub(r'%s', self.param_index, sql)
        self.connection = connection
        self.left_tb = None
        self.right_tb = []

    def parse_result(self, doc):
        ret_tup = []
        for sql_ob in self.pro:
            if sql_ob.field in doc:
                ret_tup.append(doc[sql_ob.field])
            elif '{}.{}'.format(sql_ob.coll, sql_ob.field) in doc:
                ret_tup.append(doc['{}.{}'.
                               format(sql_ob.coll, sql_ob.field)])
            else:  # This is possible only because we have not implemented multiple joins.
                ret_tup.append(None)
        return tuple(ret_tup)

    def param_index(self, _):
        self.p_index += 1
        return '%({})s'.format(self.p_index)

    def get_mongo_cur(self):
        logger.debug('\n mongo_cur: {}'.format(self.sql))
        statement = sql_parse(self.sql)

        if len(statement) > 1:
            raise SQLDecodeError('Sql: {}'.format(self.sql))

        statement = statement[0]
        sm_type = statement.get_type()

        # Some of these commands can be ignored, some need to be implemented.
        if sm_type in ('CREATE', 'ALTER', 'DROP'):
            return None

        try:
            return self.FUNC_MAP[sm_type](self, statement)
        except KeyError:
            logger.debug('\n Not implemented {} {}'.format(sm_type, statement))
            raise NotImplementedError('{} command not implemented for SQL {}'.format(sm_type, self.sql))

    @staticmethod
    def _iter_tok(tok):
        nextid, nexttok = tok.token_next(0)
        while nextid:
            yield nexttok
            nextid, nexttok = tok.token_next(nextid)

    def _update(self, sm):
        db_con = self.connection
        kw = {}
        next_id, next_tok = sm.token_next(0)
        sql_ob = next(SQLObj.token_2_obj(next_tok, self))
        collection = sql_ob.field
        self.left_tb = sql_ob.field

        next_id, next_tok = sm.token_next(next_id)

        if not next_tok.match(tokens.Keyword, 'SET'):
            raise SQLDecodeError('statement:{}'.format(sm))

        upd = {}
        next_id, next_tok = sm.token_next(next_id)
        for cmp_ob in SQLObj.token_2_obj(next_tok, self):
            upd[cmp_ob.field] = cmp_ob.rhs_obj
        kw['update'] = {'$set': upd}

        next_id, next_tok = sm.token_next(next_id)

        while next_id:
            if isinstance(next_tok, Where):
                where_op = Op.token_2_op(next_tok, self)
                kw['filter'] = where_op.to_mongo()
            next_id, next_tok = sm.token_next(next_id)

        result = db_con[collection].update_many(**kw)
        logger.debug('update_many:{} matched:{}'.format(result.modified_count, result.matched_count))
        return None

    def _delete(self, sm):
        db_con = self.connection
        kw = {}
        next_id, next_tok = sm.token_next(2)
        sql_ob = next(SQLObj.token_2_obj(next_tok, self))
        collection = sql_ob.field
        self.left_tb = sql_ob.field
        next_id, next_tok = sm.token_next(next_id)
        while next_id:
            if isinstance(next_tok, Where):
                where_op = Op.token_2_op(next_tok, self)
                kw['filter'] = where_op.to_mongo()
            next_id, next_tok = sm.token_next(next_id)

        result = db_con[collection].delete_many(**kw)
        logger.debug('delete_many: {}'.format(result.deleted_count))

    def _insert(self, sm):
        db_con = self.connection
        insert = {}
        nextid, nexttok = sm.token_next(2)
        if isinstance(nexttok, Identifier):
            collection = nexttok.get_name()
            auto = db_con['__schema__'].find_one_and_update({'name': collection} \
                                                            , {'$inc': {'auto.seq': 1}},
                                                            return_document=ReturnDocument.AFTER)
            if auto:
                auto_field_name = auto['auto']['field_name']
                auto_field_id = auto['auto']['seq']
                insert[auto_field_name] = auto_field_id
            else:
                auto_field_id = None
        else:
            raise SQLDecodeError('statement: {}'.format(sm))

        nextid, nexttok = sm.token_next(nextid)

        for sql_ob in SQLObj.token_2_obj(nexttok, self):
            insert[sql_ob.field] = self.params.pop(0)

        if self.params:
            raise SQLDecodeError('unexpected params {}'.format(self.params))

        result = db_con[collection].insert_one(insert)
        if not auto_field_id:
            auto_field_id = str(result.inserted_id)

        self.last_row_id = auto_field_id
        logger.debug('insert id {}'.format(result.inserted_id))
        return None

    def _find(self, sm):
        collection = ''
        kwargs = {}
        pro = None
        self.pro = None
        self.return_const = None
        aggr = False
        pipeline = []
        next_id, next_tok = sm.token_next(0)
        if next_tok.value == '*':
            kwargs['projection'] = {}

        elif isinstance(next_tok, Identifier) and isinstance(next_tok.tokens[0], Parenthesis):
            self.return_const = int(next_tok.tokens[0].tokens[1].value)
            kwargs['projection'] = {'_id': True}

        elif isinstance(next_tok, Identifier) and next_tok.tokens[0].token_first().value == 'COUNT':
            next_id, next_tok = sm.token_next(next_id)

            if not next_tok.match(tokens.Keyword, 'FROM'):
                raise SQLDecodeError('statement: {}'.format(sm))

            next_id, next_tok = sm.token_next(next_id)
            if not isinstance(next_tok, Identifier):
                raise SQLDecodeError('statement: {}'.format(sm))

            return self.connection[next_tok.value.strip('"')].find().count()

        else:
            self.pro = pro = []
            for sql_ob in SQLObj.token_2_obj(next_tok, self):
                if not collection:
                    collection = sql_ob.coll
                elif not collection == sql_ob.coll:
                    aggr = True
                pro.append(sql_ob)
            if not aggr:
                kwargs['projection'] = {'_id': False}
                for sql_ob in pro:
                    kwargs['projection'].update({sql_ob.field: True})

        next_id, next_tok = sm.token_next(next_id)

        if not next_tok.match(tokens.Keyword, 'FROM'):
            raise SQLDecodeError('statement: {}'.format(sm))

        next_id, next_tok = sm.token_next(next_id)
        sql_ob = next(SQLObj.token_2_obj(next_tok, self))
        if not collection:
            collection = sql_ob.field
        else:
            if collection != sql_ob.field:
                raise SQLDecodeError('statement: {}'.format(sm))

        left_tb = sql_ob.field
        self.left_tb = left_tb

        next_id, next_tok = sm.token_next(next_id)

        while next_id:
            if isinstance(next_tok, Where):
                kwargs['filter'] = {}

                where_op = Op.token_2_op(next_tok, self)
                kwargs['filter'] = where_op.to_mongo()

            elif next_tok.match(tokens.Keyword, 'LIMIT'):
                next_id, next_tok = sm.token_next(next_id)
                kwargs['limit'] = int(next_tok.value)

            elif next_tok.match(tokens.Keyword, 'INNER JOIN'):
                aggr = True

                next_id, next_tok = sm.token_next(next_id)
                sql_ob = next(SQLObj.token_2_obj(next_tok, self))
                right_tb = sql_ob.field
                self.right_tb.append(right_tb)
                next_id, next_tok = sm.token_next(next_id)
                if not next_tok.match(tokens.Keyword, 'ON'):
                    raise SQLDecodeError('statement: {}'.format(sm))

                next_id, next_tok = sm.token_next(next_id)
                join_ob = next(SQLObj.token_2_obj(next_tok, self))
                if right_tb == join_ob.other_coll:
                    local_field = join_ob.field
                    foreign_field = join_ob.other_field
                else:
                    local_field = join_ob.other_field
                    foreign_field = join_ob.field

                lookup = {
                    '$lookup': {
                        'from': left_tb,
                        'localField': local_field,
                        'foreignField': foreign_field,
                        'as': right_tb
                    }
                }
                unwind = {'$unwind': {'path': '${}'.format(right_tb)}}
                pipeline.append(lookup)
                pipeline.append(unwind)

            elif next_tok.match(tokens.Keyword, 'LEFT OUTER JOIN'):
                aggr = True

                next_id, next_tok = sm.token_next(next_id)
                sql_ob = next(SQLObj.token_2_obj(next_tok, self))
                right_tb = sql_ob.field
                self.right_tb.append(right_tb)
                next_id, next_tok = sm.token_next(next_id)
                if not next_tok.match(tokens.Keyword, 'ON'):
                    raise SQLDecodeError('statement: {}'.format(sm))

                next_id, next_tok = sm.token_next(next_id)
                join_ob = next(SQLObj.token_2_obj(next_tok, self))
                if right_tb == join_ob.other_coll:
                    local_field = join_ob.field
                    foreign_field = join_ob.other_field
                else:
                    local_field = join_ob.other_field
                    foreign_field = join_ob.field

                lookup = {
                    '$lookup': {
                        'from': left_tb,
                        'localField': local_field,
                        'foreignField': foreign_field,
                        'as': right_tb
                    }
                }
                unwind = {
                    '$unwind': {
                        'path': '${}'.format(right_tb),
                        'preserveNullAndEmptyArrays': True
                    }
                }
                pipeline.append(lookup)
                pipeline.append(unwind)

            elif next_tok.match(tokens.Keyword, 'ORDER'):
                kwargs['sort'] = {} if aggr else []
                next_id, next_tok = sm.token_next(next_id)
                if not next_tok.match(tokens.Keyword, 'BY'):
                    raise SQLDecodeError('statement: {}'.format(sm))

                next_id, next_tok = sm.token_next(next_id)
                for order, sql_ob in SQLObj.token_2_obj(next_tok, self):
                    if not aggr:
                        kwargs['sort'].append((sql_ob.field, ORDER_BY_MAP[order]))
                    else:
                        if sql_ob.coll == left_tb:
                            kwargs['sort'][sql_ob.field] = ORDER_BY_MAP[order]
                        else:
                            kwargs['sort']['{}.{}'.format(sql_ob.coll, sql_ob.field)] = ORDER_BY_MAP[order]

            else:
                raise SQLDecodeError('statement: {}'.format(sm))

            next_id, next_tok = sm.token_next(next_id)
        if aggr:
            if 'sort' in kwargs:
                pipeline.append({'$sort': kwargs['sort']})
            if 'filter' in kwargs:
                pipeline.append({'$match': kwargs['filter']})
            if 'limit' in kwargs:
                pipeline.append({'$limit': kwargs['limit']})
            if pro:
                spec = {}
                for sql_ob in pro:
                    if sql_ob.coll == left_tb:
                        spec['{}.{}'.format(sql_ob.coll, sql_ob.field)] = '${}'.format(sql_ob.field)
                    else:
                        spec['{}.{}'.format(sql_ob.coll, sql_ob.field)] = True
                spec['_id'] = False
                pipeline.append({'$project': spec})
                cur = self.connection[collection].aggregate(pipeline)
            return self.connection[collection].aggregate(pipeline)
        return self.connection[collection].find(**kwargs)

    FUNC_MAP = {
        'SELECT': _find,
        'UPDATE': _update,
        'INSERT': _insert,
        'DELETE': _delete
    }


class SQLObj:
    def __init__(self, field, coll=None, parse=None):
        self.field = field
        self.coll = coll
        self.parse = parse

    @staticmethod
    def token_2_obj(token, parse):
        if isinstance(token, Identifier):
            tok_first = token.token_first()
            if isinstance(tok_first, Identifier):
                yield token.get_ordering(), SQLObj(tok_first.get_name(), tok_first.get_parent_name(), parse)
            else:
                yield SQLObj(token.get_name(), token.get_parent_name(), parse)

        elif isinstance(token, IdentifierList):
            for anIden in token.get_identifiers():
                yield from SQLObj.token_2_obj(anIden, parse)
                pass

        elif isinstance(token, Comparison):
            lhs = next(SQLObj.token_2_obj(token.left, parse))
            if isinstance(token.right, Identifier):
                rhs = next(SQLObj.token_2_obj(token.right, parse))
                yield JoinOb(rhs.field, rhs.coll, lhs.field, lhs.coll, parse)
            else:
                op = OPERATOR_MAP[token.token_next(0)[1].value]
                index = int(re.match(r'%\(([0-9]+)\)s', token.right.value, flags=re.IGNORECASE).group(1))
                yield CmpOb(**vars(lhs), operator=op, rhs_obj=parse.params[index])

        elif isinstance(token, Parenthesis):
            next_id, next_tok = token.token_next(0)
            while next_tok.value != ')':
                yield from SQLObj.token_2_obj(next_tok, parse)
                next_id, next_tok = token.token_next(next_id)

        elif token.match(tokens.Name.Placeholder, '.*', regex=True):
            index = int(re.match(r'%\(([0-9]+)\)s', token.value, flags=re.IGNORECASE).group(1))
            yield parse.params[index]

        else:
            raise SQLDecodeError

    def to_mongo(self):
        raise SQLDecodeError


class JoinOb(SQLObj):
    def __init__(self, other_field, other_coll, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.other_field = other_field
        self.other_coll = other_coll


class CmpOb(SQLObj):
    def __init__(self, operator, rhs_obj, *args, **kwargs):
        super(CmpOb, self).__init__(*args, **kwargs)
        self.operator = operator
        self.rhs_obj = rhs_obj
        self.is_not = False

    def to_mongo(self):
        if self.coll == self.parse.left_tb:
            field = self.field
        else:
            field = '{}.{}'.format(self.coll, self.field)

        if not self.is_not:
            return {field: {self.operator: self.rhs_obj}}
        else:
            return {field: {'$not': {self.operator: self.rhs_obj}}}


class Op:
    def __init__(self, lhs=None, rhs=None, parse=None, op_name='generic'):
        self.lhs = lhs
        self.rhs = rhs
        self.parse = parse
        self.is_not = False
        self._op_name = op_name
        self.precedence = OPERATOR_PRECEDENCE[op_name]

    @staticmethod
    def token_2_op(token, parse):
        def resolve_token(token):
            logger.debug('resolving token: {}'.format(token.value))

            def helper():
                nonlocal lhs_obj, hanging_obj, next_id, next_tok, hanging_obj_used, kw

                if not hanging_obj:
                    raise SQLDecodeError

                kw['lhs'] = hanging_obj
                next_id, next_tok = token.token_next(next_id)
                hanging_obj = {'obj': next_tok}
                kw['rhs'] = hanging_obj
                hanging_obj_used = True

            nonlocal parse
            next_id, next_tok = token.token_next(0)
            hanging_obj = {}
            kw = {
                'parse': parse
            }
            hanging_obj_used = False
            lhs_obj = {}

            while next_id:
                if next_tok.match(tokens.Keyword, 'AND'):
                    helper()
                    yield AndOp(**kw)

                elif next_tok.match(tokens.Keyword, 'OR'):
                    helper()
                    yield OrOp(**kw)

                elif next_tok.match(tokens.Keyword, 'IN'):
                    helper()
                    yield InOp(**kw)

                elif next_tok.match(tokens.Keyword, 'NOT'):
                    x, next_not = token.token_next(next_id)
                    if next_not.match(tokens.Keyword, 'IN'):
                        next_id, next_tok = token.token_next(next_id)
                        helper()
                        in_ob = InOp(**kw)
                        in_ob.is_not = True
                        yield in_ob
                    else:
                        helper()
                        yield NotOp(**kw)

                elif next_tok.match(tokens.Keyword, '.*', regex=True):
                    helper()
                    yield Op(**kw)

                elif next_tok.match(tokens.Punctuation, ')'):
                    break

                else:
                    hanging_obj = {'obj': next_tok}
                    hanging_obj_used = False
                next_id, next_tok = token.token_next(next_id)

            if not hanging_obj_used:
                if isinstance(hanging_obj['obj'], Comparison):
                    yield AndOp(lhs={'obj': None}, rhs=hanging_obj, parse=parse)
                elif isinstance(hanging_obj['obj'], Parenthesis):
                    yield Op.token_2_op(hanging_obj['obj'], parse)
                else:
                    raise SQLDecodeError

        def op_precedence(operator_obj):
            nonlocal op_list
            if not op_list:
                op_list.append(operator_obj)
                return
            for i in range(len(op_list)):
                if operator_obj.precedence > op_list[i].precedence:
                    op_list.insert(i, operator_obj)
                    break
            else:
                op_list.insert(len(op_list), operator_obj)

        op_list = []

        for op in resolve_token(token):
            op_precedence(op)

        while op_list:
            eval_op = op_list.pop(0)
            eval_op.evaluate()
        return eval_op

    def evaluate(self):
        self.lhs['obj'].rhs['obj'] = self.rhs['obj']
        self.rhs['obj'].lhs['obj'] = self.lhs['obj']

    def to_mongo(self):
        raise SQLDecodeError


class InOp(Op):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs, op_name='IN')
        self.is_not = False

    def evaluate(self):
        if not (self.lhs and self.lhs['obj']):
            raise SQLDecodeError

        if not (self.rhs and self.rhs['obj']):
            raise SQLDecodeError

        if isinstance(self.lhs['obj'], Identifier):
            sql_ob = next(SQLObj.token_2_obj(self.lhs['obj'], self.parse))
        else:
            raise SQLDecodeError

        if sql_ob.coll:
            if sql_ob.coll == self.parse.left_tb:
                self.field = sql_ob.field
            else:
                self.field = '{}.{}'.format(sql_ob.coll, sql_ob.field)
        else:
            self.field = sql_ob.field

        if not isinstance(self.rhs['obj'], Parenthesis):
            raise SQLDecodeError

        self._in = [ob for ob in SQLObj.token_2_obj(self.rhs['obj'], self.parse)]

        self.lhs['obj'] = self
        self.rhs['obj'] = self

    def to_mongo(self):
        if self.is_not is False:
            op = '$in'
        else:
            op = '$nin'
        return {self.field: {op: self._in}}


class NotOp(Op):
    def __init__(self, *args, **kwargs):
        super(NotOp, self).__init__(*args, **kwargs, op_name='NOT')

    def evaluate(self):
        if not (self.rhs and self.rhs['obj']):
            raise SQLDecodeError

        if isinstance(self.rhs['obj'], Parenthesis):
            self.op = self.token_2_op(self.rhs['obj'], self.parse)
        elif isinstance(self.rhs['obj'], Comparison):
            self.op = SQLObj.token_2_obj(self.rhs['obj'], self.parse)
        else:
            raise SQLDecodeError

        self.op.is_not = True

    def to_mongo(self):
        return self.op.to_mongo()


class AndOp(Op):
    def __init__(self, *args, **kwargs):
        super(AndOp, self).__init__(*args, **kwargs, op_name='AND')
        self._and = []

    def evaluate(self):
        # assert self.lhs or self.lhs['obj']
        if not (self.rhs and self.rhs['obj']):
            raise SQLDecodeError

        if self.lhs and self.lhs['obj']:
            if isinstance(self.lhs['obj'], AndOp):
                self._and.extend(self.lhs['obj']._and)
            elif isinstance(self.lhs['obj'], Op):
                self._and.append(self.lhs['obj'])
            elif isinstance(self.lhs['obj'], Parenthesis):
                self._and.append(self.token_2_op(self.lhs['obj'], self.parse))
            elif isinstance(self.lhs['obj'], Comparison):
                self._and.append(next(SQLObj.token_2_obj(self.lhs['obj'], self.parse)))
            else:
                raise SQLDecodeError

        if isinstance(self.rhs['obj'], AndOp):
            self._and.extend(self.rhs['obj']._and)
        elif isinstance(self.rhs['obj'], Op):
            self._and.append(self.rhs['obj'])
        elif isinstance(self.rhs['obj'], Parenthesis):
            self._and.append(self.token_2_op(self.rhs['obj'], self.parse))
        elif isinstance(self.rhs['obj'], Comparison):
            self._and.append(next(SQLObj.token_2_obj(self.rhs['obj'], self.parse)))
        else:
            raise SQLDecodeError

        self.lhs['obj'] = self
        self.rhs['obj'] = self

    def to_mongo(self):
        if self.is_not is False:
            ret_doc = {'$and': []}
            for itm in self._and:
                ret_doc['$and'].append(itm.to_mongo())
        else:
            ret_doc = {'$or': []}
            for itm in self._and:
                itm.is_not = True
                ret_doc['$or'].append(itm.to_mongo())

        return ret_doc


class OrOp(Op):
    def __init__(self, *args, **kwargs):
        super(OrOp, self).__init__(*args, **kwargs, op_name='OR')
        self._or = []

    def evaluate(self):
        if not (self.lhs and self.lhs['obj']):
            raise SQLDecodeError

        if not (self.rhs and self.rhs['obj']):
            raise SQLDecodeError

        if isinstance(self.lhs['obj'], OrOp):
            self._or.extend(self.lhs['obj']._or)
        elif isinstance(self.lhs['obj'], Op):
            self._or.append(self.lhs['obj'])
        elif isinstance(self.lhs['obj'], Parenthesis):
            self._or.append(self.token_2_op(self.lhs['obj'], self.parse))
        elif isinstance(self.lhs['obj'], Comparison):
            self._or.append(next(SQLObj.token_2_obj(self.lhs['obj'], self.parse)))
        else:
            raise SQLDecodeError

        if isinstance(self.rhs['obj'], OrOp):
            self._or.extend(self.rhs['obj']._or)
        elif isinstance(self.rhs['obj'], Op):
            self._or.append(self.rhs['obj'])
        elif isinstance(self.rhs['obj'], Parenthesis):
            self._or.append(self.token_2_op(self.rhs['obj'], self.parse))
        elif isinstance(self.rhs['obj'], Comparison):
            self._or.append(next(SQLObj.token_2_obj(self.rhs['obj'], self.parse)))
        else:
            raise SQLDecodeError

        self.lhs['obj'] = self
        self.rhs['obj'] = self

    def to_mongo(self):
        if not self.is_not:
            oper = '$or'
        else:
            oper = '$nor'

        ret_doc = {oper: []}
        for itm in self._or:
            ret_doc[oper].append(itm.to_mongo())
        return ret_doc


class Cursor():
    def __init__(self, m_cli_connection):
        self.m_cli_connection = m_cli_connection
        self.mongo_cursor = None
        self.result_ob = None

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        if isinstance(self.mongo_cursor, (PymongoCursor, PymongoCommandCursor)):
            self.mongo_cursor.close()
        self.mongo_cursor = None
        self.result_ob = None

    def __iter__(self):
        if isinstance(self.mongo_cursor, (PymongoCursor, PymongoCommandCursor)):
            yield from self.mongo_cursor
        else:
            raise RuntimeError('Iteration over a dead cursor')

    def __getattr__(self, name):
        try:
            return getattr(self.result_ob, name)
        except AttributeError:
            pass

        try:
            return getattr(self.m_cli_connection, name)
        except AttributeError:
            raise

    def execute(self, sql, params=None):
        self.result_ob = Parse(self.m_cli_connection, sql, params)

        try:
            self.mongo_cursor = self.result_ob.get_mongo_cur()
        except Exception as e:
            logger.debug(e)
            raise

        else:
            if (isinstance(self.mongo_cursor, (PymongoCursor, PymongoCommandCursor))
                    and self.mongo_cursor.alive
                    and hasattr(self.mongo_cursor, 'count')):
                self.rowcount = self.mongo_cursor.count()
            else:
                self.rowcount = 1

    def _prefetch(self):
        if self.mongo_cursor is None:
            raise RuntimeError('Non existent cursor operation')

        if not isinstance(self.mongo_cursor, (PymongoCursor, PymongoCommandCursor)):
            return self.mongo_cursor,

        if not self.mongo_cursor.alive:
            return []

        return None

    def fetchmany(self, size=1):
        ret = self._prefetch()
        if ret is not None:
            return ret

        if self.result_ob.return_const is not None:
            return [self.result_ob.return_const] * self.mongo_cursor.count(with_limit_and_skip=True)

        ret = []
        for i, row in enumerate(self.mongo_cursor):
            ret.append(self.result_ob.parse_result(row))
            if i == size - 1:
                break
        return ret

    def fetchone(self):
        ret = self._prefetch()
        if ret is not None:
            return ret

        if self.result_ob.return_const:
            try:
                self.mongo_cursor.next()
            except StopIteration:
                return []
            else:
                return (self.result_ob.return_const,)

        else:
            try:
                res = self.result_ob.parse_result(self.mongo_cursor.next())
            except StopIteration:
                res = []
            return res

    def fetchall(self):
        ret = self._prefetch()
        if ret is not None:
            return ret

        if self.result_ob.return_const is not None:
            return [self.result_ob.return_const] * self.mongo_cursor.count(with_limit_and_skip=True)
        return [self.result_ob.parse_result(row) for row in self.mongo_cursor]

