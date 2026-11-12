# -*- coding: utf-8 -*-
from __future__ import absolute_import, print_function, division


# standard library dependencies
import logging
from petl.compat import next, text_type, string_types


# internal dependencies
from petl.errors import ArgumentError
from petl.comparison import Comparable
from petl.util.base import Table
from petl.io.db_utils import _is_dbapi_connection, _is_dbapi_cursor, \
    _is_sqlalchemy_connection, _is_sqlalchemy_engine, _is_sqlalchemy_session, \
    _is_clikchouse_dbapi_connection, _quote, _placeholders, \
    _execute_sqlalchemy, _sqlalchemy_transaction
from petl.io.db_create import drop_table, create_table
from petl.transform.history import _mergeversions


logger = logging.getLogger(__name__)
debug = logger.debug
warning = logger.warning


def fromdb(dbo, query, *args, **kwargs):
    """Provides access to data from any DB-API 2.0 connection via a given query.
    E.g., using :mod:`sqlite3`::

        >>> import petl as etl
        >>> import sqlite3
        >>> connection = sqlite3.connect('example.db')
        >>> table = etl.fromdb(connection, 'SELECT * FROM example', ownsdb=True)

    E.g., using :mod:`psycopg2` (assuming you've installed it first)::

        >>> import petl as etl
        >>> import psycopg2
        >>> connection = psycopg2.connect('dbname=example user=postgres')
        >>> table = etl.fromdb(connection, 'SELECT * FROM example')

    E.g., using :mod:`pymysql` (assuming you've installed it first)::

        >>> import petl as etl
        >>> import pymysql
        >>> connection = pymysql.connect(password='moonpie', database='thangs')
        >>> table = etl.fromdb(connection, 'SELECT * FROM example')

    The `dbo` argument may also be a function that creates a cursor. N.B., each
    call to the function should return a new cursor. E.g.::

        >>> import petl as etl
        >>> import psycopg2
        >>> connection = psycopg2.connect('dbname=example user=postgres')
        >>> mkcursor = lambda: connection.cursor(cursor_factory=psycopg2.extras.DictCursor)
        >>> table = etl.fromdb(mkcursor, 'SELECT * FROM example')

    The parameter `dbo` may also be an SQLAlchemy engine, session or
    connection object.

    The parameter `dbo` may also be a string, in which case it is interpreted as
    the name of a file containing an :mod:`sqlite3` database.

    When parameter `ownsdb` is True, the database connection will be closed when the
    cursor is out of scope and is being garbage collected.

    Note that the default behaviour of most database servers and clients is for
    the entire result set for each query to be sent from the server to the
    client. If your query returns a large result set this can result in
    significant memory usage at the client. Some databases support server-side
    cursors which provide a means for client libraries to fetch result sets
    incrementally, reducing memory usage at the client.

    To use a server-side cursor with a PostgreSQL database, e.g.::

        >>> import petl as etl
        >>> import psycopg2
        >>> connection = psycopg2.connect('dbname=example user=postgres')
        >>> table = etl.fromdb(lambda: connection.cursor(name='arbitrary'),
        ...                    'SELECT * FROM example')

    For more information on server-side cursors see the following links:

        * http://initd.org/psycopg/docs/usage.html#server-side-cursors
        * http://mysql-python.sourceforge.net/MySQLdb.html#using-and-extending

    """

    ownsdb = kwargs.pop('ownsdb', False)
    # convenience for working with sqlite3
    if isinstance(dbo, string_types):
        import sqlite3
        dbo = sqlite3.connect(dbo)
        ownsdb = True

    return DbView(dbo, query, *args, ownsdb=ownsdb, **kwargs)


class DbView(Table):

    def __init__(self, dbo, query, *args, **kwargs):
        self.dbo = dbo
        self.query = query
        self.args = args
        self.kwargs = kwargs
        self.ownsdb = kwargs.pop('ownsdb', False)

    def __del__(self):
        if self.ownsdb:
            try:
                self.dbo.close()
                self.dbo = None
            except Exception:
                pass

    def __iter__(self):

        # does it quack like a standard DB-API 2.0 connection?
        if _is_dbapi_connection(self.dbo):
            debug('assuming %r is standard DB-API 2.0 connection', self.dbo)
            _iter = _iter_dbapi_connection

        # does it quack like a standard DB-API 2.0 cursor?
        elif _is_dbapi_cursor(self.dbo):
            debug('assuming %r is standard DB-API 2.0 cursor')
            warning('using a DB-API cursor with fromdb() is not recommended '
                    'and may lead to unexpected results, a DB-API connection '
                    'is better')
            _iter = _iter_dbapi_cursor

        # does it quack like an SQLAlchemy engine?
        elif _is_sqlalchemy_engine(self.dbo):
            debug('assuming %r instance of sqlalchemy.engine.base.Engine',
                  self.dbo)
            _iter = _iter_sqlalchemy_engine

        # does it quack like an SQLAlchemy session?
        elif _is_sqlalchemy_session(self.dbo):
            debug('assuming %r instance of sqlalchemy.orm.session.Session',
                  self.dbo)
            _iter = _iter_sqlalchemy_session

        # does it quack like an SQLAlchemy connection?
        elif _is_sqlalchemy_connection(self.dbo):
            debug('assuming %r instance of sqlalchemy.engine.base.Connection',
                  self.dbo)
            _iter = _iter_sqlalchemy_connection

        elif callable(self.dbo):
            debug('assuming %r is a function returning a cursor', self.dbo)
            _iter = _iter_dbapi_mkcurs

        # some other sort of duck...
        else:
            raise ArgumentError('unsupported database object type: %r' % self.dbo)

        return _iter(self.dbo, self.query, *self.args, **self.kwargs)


def _iter_dbapi_mkcurs(mkcurs, query, *args, **kwargs):
    cursor = mkcurs()
    try:
        for row in _iter_dbapi_cursor(cursor, query, *args, **kwargs):
            yield row
    finally:
        cursor.close()


def _iter_dbapi_connection(connection, query, *args, **kwargs):
    cursor = connection.cursor()
    try:
        for row in _iter_dbapi_cursor(cursor, query, *args, **kwargs):
            yield row
    finally:
        cursor.close()


def _iter_dbapi_cursor(cursor, query, *args, **kwargs):
    cursor.execute(query, *args, **kwargs)
    # fetch one row before iterating, to force population of cursor.description
    # which may be postponed if using server-side cursors
    # not all database drivers populate cursor after execute so we call fetchall
    try:
        it = iter(cursor)
    except TypeError:
        it = iter(cursor.fetchall())
    try:
        first_row = next(it)
    except StopIteration:
        first_row = None
    # fields should be available now
    hdr = [d[0] for d in cursor.description]
    yield tuple(hdr)
    if first_row is None:
        return
    yield first_row
    for row in it:
        yield row  # don't wrap, return whatever the database engine returns


def _iter_sqlalchemy_engine(engine, query, *args, **kwargs):
    with engine.connect() as connection:
        iterator = _iter_sqlalchemy_connection(connection, query, *args, **kwargs)
        try:
            for row in iterator:
                yield row
        finally:
            iterator.close()


def _iter_sqlalchemy_connection(connection, query, *args, **kwargs):
    debug('connection: %r', connection)
    results = _execute_sqlalchemy(connection, query, *args, **kwargs)
    try:
        yield tuple(results.keys())
        for row in results:
            yield row
    finally:
        results.close()


def _iter_sqlalchemy_session(session, query, *args, **kwargs):
    if isinstance(query, string_types):
        from sqlalchemy import text
        query = text(query)
    results = session.execute(query, *args, **kwargs)
    try:
        yield tuple(results.keys())
        for row in results:
            yield row
    finally:
        results.close()


def todb(table, dbo, tablename, schema=None, commit=True,
         create=False, drop=False, constraints=True, metadata=None,
         dialect=None, sample=1000, truncate=True):
    """
    Load data into an existing database table via a DB-API 2.0
    connection or cursor. By default the database table will be truncated,
    i.e., all existing rows will be deleted prior to inserting the new data.
    Pass ``truncate=False`` to keep the existing rows and append instead, as
    :func:`petl.io.db.appenddb` does. E.g.::

        >>> import petl as etl
        >>> table = [['foo', 'bar'],
        ...          ['a', 1],
        ...          ['b', 2],
        ...          ['c', 2]]
        >>> # using sqlite3
        ... import sqlite3
        >>> connection = sqlite3.connect('example.db')
        >>> # assuming table "foobar" already exists in the database
        ... etl.todb(table, connection, 'foobar')
        >>> # using psycopg2
        >>> import psycopg2
        >>> connection = psycopg2.connect('dbname=example user=postgres')
        >>> # assuming table "foobar" already exists in the database
        ... etl.todb(table, connection, 'foobar')
        >>> # using pymysql
        >>> import pymysql
        >>> connection = pymysql.connect(password='moonpie', database='thangs')
        >>> # tell MySQL to use standard quote character
        ... connection.cursor().execute('SET SQL_MODE=ANSI_QUOTES')
        >>> # load data, assuming table "foobar" already exists in the database
        ... etl.todb(table, connection, 'foobar')

    N.B., for MySQL the statement ``SET SQL_MODE=ANSI_QUOTES`` is required to
    ensure MySQL uses SQL-92 standard quote characters.

    A cursor can also be provided instead of a connection, e.g.::

        >>> import psycopg2
        >>> connection = psycopg2.connect('dbname=example user=postgres')
        >>> cursor = connection.cursor()
        >>> etl.todb(table, cursor, 'foobar')

    The parameter `dbo` may also be an SQLAlchemy engine, session or
    connection object.

    Cursors created to truncate or insert rows are closed on success or failure.
    A cursor supplied directly by the caller is left open.

    The parameter `dbo` may also be a string, in which case it is interpreted
    as the name of a file containing an :mod:`sqlite3` database.

    If ``create=True`` this function will attempt to automatically create a
    database table before loading the data. This functionality requires
    `SQLAlchemy <http://www.sqlalchemy.org/>`_ to be installed.

    **Keyword arguments:**

    table : table container
        Table data to load
    dbo : database object
        DB-API 2.0 connection, callable returning a DB-API 2.0 cursor, or
        SQLAlchemy connection, engine or session
    tablename : string
        Name of the table in the database
    schema : string
        Name of the database schema to find the table in
    commit : bool
        If True commit the changes
    create : bool
        If True attempt to create the table before loading, inferring types
        from a sample of the data (requires SQLAlchemy)
    drop : bool or 'if_exists'
        If True attempt to drop the table before recreating (only relevant if
        create=True). If 'if_exists', do not raise an error if the table does
        not exist.
    constraints : bool
        If True use length and nullable constraints (only relevant if
        create=True)
    metadata : sqlalchemy.MetaData
        Custom table metadata (only relevant if create=True)
    dialect : string
        One of {'access', 'sybase', 'sqlite', 'informix', 'firebird', 'mysql',
        'oracle', 'maxdb', 'postgresql', 'mssql'} (only relevant if
        create=True)
    sample : int
        Number of rows to sample when inferring types etc. Set to 0 to use the
        whole table (only relevant if create=True)
    truncate : bool
        If True (the default) truncate the table before loading, deleting any
        existing rows. Set to False to keep existing rows and append the new
        data instead.

    .. note::

        This function is in principle compatible with any DB-API 2.0
        compliant database driver. However, at the time of writing some DB-API
        2.0 implementations, including cx_Oracle and MySQL's
        Connector/Python, are not compatible with this function, because they
        only accept a list argument to the cursor.executemany() function
        called internally by :mod:`petl`. This can be worked around by
        proxying the cursor objects, e.g.::

            >>> import cx_Oracle
            >>> connection = cx_Oracle.Connection(...)
            >>> class CursorProxy(object):
            ...     def __init__(self, cursor):
            ...         self._cursor = cursor
            ...     def executemany(self, statement, parameters, **kwargs):
            ...         # convert parameters to a list
            ...         parameters = list(parameters)
            ...         # pass through to proxied cursor
            ...         return self._cursor.executemany(statement, parameters, **kwargs)
            ...     def __getattr__(self, item):
            ...         return getattr(self._cursor, item)
            ...
            >>> def get_cursor():
            ...     return CursorProxy(connection.cursor())
            ...
            >>> import petl as etl
            >>> etl.todb(tbl, get_cursor, ...)

        Note however that this does imply loading the entire table into
        memory as a list prior to inserting into the database.

    """

    needs_closing = False

    # convenience for working with sqlite3
    if isinstance(dbo, string_types):
        import sqlite3
        dbo = sqlite3.connect(dbo)
        needs_closing = True

    try:
        if create:
            if drop:
                if_exists = drop == 'if_exists'
                drop_table(dbo, tablename, schema=schema, commit=commit,
                           if_exists=if_exists)
            create_table(table, dbo, tablename, schema=schema, commit=commit,
                         constraints=constraints, metadata=metadata,
                         dialect=dialect, sample=sample)
        _todb(table, dbo, tablename, schema=schema, commit=commit,
              truncate=truncate)

    finally:
        if needs_closing:
            dbo.close()


Table.todb = todb


def _todb(table, dbo, tablename, schema=None, commit=True, truncate=False):

    # need to deal with polymorphic dbo argument
    # what sort of duck is it?

    if _is_clikchouse_dbapi_connection(dbo):
        debug('assuming %r is clickhosue DB-API 2.0 connection', dbo)
        _todb_clikchouse_dbapi_connection(table, dbo, tablename, schema=schema,
                               commit=commit, truncate=truncate)

    # does it quack like a standard DB-API 2.0 connection?
    elif _is_dbapi_connection(dbo):
        debug('assuming %r is standard DB-API 2.0 connection', dbo)
        _todb_dbapi_connection(table, dbo, tablename, schema=schema,
                               commit=commit, truncate=truncate)

    # does it quack like a standard DB-API 2.0 cursor?
    elif _is_dbapi_cursor(dbo):
        debug('assuming %r is standard DB-API 2.0 cursor')
        _todb_dbapi_cursor(table, dbo, tablename, schema=schema, commit=commit,
                           truncate=truncate)

    # does it quack like an SQLAlchemy engine?
    elif _is_sqlalchemy_engine(dbo):
        debug('assuming %r instance of sqlalchemy.engine.base.Engine', dbo)
        _todb_sqlalchemy_engine(table, dbo, tablename, schema=schema,
                                commit=commit, truncate=truncate)

    # does it quack like an SQLAlchemy session?
    elif _is_sqlalchemy_session(dbo):
        debug('assuming %r instance of sqlalchemy.orm.session.Session', dbo)
        _todb_sqlalchemy_session(table, dbo, tablename, schema=schema,
                                 commit=commit, truncate=truncate)

    # does it quack like an SQLAlchemy connection?
    elif _is_sqlalchemy_connection(dbo):
        debug('assuming %r instance of sqlalchemy.engine.base.Connection', dbo)
        _todb_sqlalchemy_connection(table, dbo, tablename, schema=schema,
                                    commit=commit, truncate=truncate)

    elif callable(dbo):
        debug('assuming %r is a function returning standard DB-API 2.0 cursor '
              'objects', dbo)
        _todb_dbapi_mkcurs(table, dbo, tablename, schema=schema, commit=commit,
                           truncate=truncate)

    # some other sort of duck...
    else:
        raise ArgumentError('unsupported database object type: %r' % dbo)


SQL_TRUNCATE_QUERY = 'DELETE FROM %s'
SQL_INSERT_QUERY = 'INSERT INTO %s (%s) VALUES (%s)'


def _close_load_cursor(cursor, load_failed):
    try:
        cursor.close()
    except Exception:
        if not load_failed:
            raise
        debug('could not close cursor after failed load', exc_info=True)


def _todb_dbapi_connection(table, connection, tablename, schema=None,
                           commit=True, truncate=False):

    # sanitise table name
    tablename = _quote(tablename)
    if schema is not None:
        tablename = _quote(schema) + '.' + tablename
    debug('tablename: %r', tablename)

    # sanitise field names
    it = iter(table)
    hdr = next(it)
    flds = list(map(text_type, hdr))
    colnames = [_quote(n) for n in flds]
    debug('column names: %r', colnames)

    # determine paramstyle and build placeholders string
    placeholders = _placeholders(connection, colnames)
    debug('placeholders: %r', placeholders)

    # get a cursor
    cursor = connection.cursor()

    load_failed = True
    try:
        if truncate:
            # TRUNCATE is not supported in some databases and causing locks with
            # MySQL used via SQLAlchemy, fall back to DELETE FROM for now
            truncatequery = SQL_TRUNCATE_QUERY % tablename
            debug('truncate the table via query %r', truncatequery)
            cursor.execute(truncatequery)
            # just in case, close and resurrect cursor
            closing_cursor, cursor = cursor, None
            closing_cursor.close()
            cursor = connection.cursor()

        insertcolnames = ', '.join(colnames)
        insertquery = SQL_INSERT_QUERY % (tablename, insertcolnames, placeholders)
        debug('insert data via query %r' % insertquery)
        cursor.executemany(insertquery, it)
        load_failed = False
    finally:
        if cursor is not None:
            debug('close the cursor')
            _close_load_cursor(cursor, load_failed)

    if commit:
        debug('commit transaction')
        connection.commit()


def _todb_clikchouse_dbapi_connection(table, connection, tablename, schema=None,
                           commit=True, truncate=False):

    # sanitise table name
    tablename = _quote(tablename)
    if schema is not None:
        tablename = _quote(schema) + '.' + tablename
    debug('tablename: %r', tablename)

    # sanitise field names
    it = iter(table)
    hdr = next(it)
    flds = list(map(text_type, hdr))
    colnames = [_quote(n) for n in flds]
    debug('column names: %r', colnames)

    # determine paramstyle and build placeholders string
    placeholders = _placeholders(connection, colnames)
    debug('placeholders: %r', placeholders)

    # get a cursor
    cursor = connection.cursor()

    load_failed = True
    try:
        if truncate:
            # TRUNCATE is not supported in some databases and causing locks with
            # MySQL used via SQLAlchemy, fall back to DELETE FROM for now
            truncatequery = 'TRUNCATE TABLE IF EXISTS %s' % tablename
            debug('truncate the table via query %r', truncatequery)
            cursor.execute(truncatequery)
            # just in case, close and resurrect cursor
            closing_cursor, cursor = cursor, None
            closing_cursor.close()
            cursor = connection.cursor()

        insertcolnames = ', '.join(colnames)
        insertquery = 'INSERT INTO %s (%s) VALUES' % (tablename, insertcolnames)
        debug('insert data via query %r' % insertquery)
        cursor.executemany(insertquery, it)
        load_failed = False
    finally:
        if cursor is not None:
            debug('close the cursor')
            _close_load_cursor(cursor, load_failed)

    if commit:
        debug('commit transaction')
        connection.commit()


def _todb_dbapi_mkcurs(table, mkcurs, tablename, schema=None, commit=True,
                       truncate=False):

    # sanitise table name
    tablename = _quote(tablename)
    if schema is not None:
        tablename = _quote(schema) + '.' + tablename
    debug('tablename: %r', tablename)

    # sanitise field names
    it = iter(table)
    hdr = next(it)
    flds = list(map(text_type, hdr))
    colnames = [_quote(n) for n in flds]
    debug('column names: %r', colnames)

    debug('obtain cursor and connection')
    cursor = mkcurs()

    load_failed = True
    try:
        # N.B., we depend on this optional DB-API 2.0 attribute being implemented
        assert hasattr(cursor, 'connection'), \
            'could not obtain connection via cursor'
        connection = cursor.connection

        # determine paramstyle and build placeholders string
        placeholders = _placeholders(connection, colnames)
        debug('placeholders: %r', placeholders)

        if truncate:
            # TRUNCATE is not supported in some databases and causing locks with
            # MySQL used via SQLAlchemy, fall back to DELETE FROM for now
            truncatequery = SQL_TRUNCATE_QUERY % tablename
            debug('truncate the table via query %r', truncatequery)
            cursor.execute(truncatequery)
            # N.B., may be server-side cursor, need to resurrect
            closing_cursor, cursor = cursor, None
            closing_cursor.close()
            cursor = mkcurs()

        insertcolnames = ', '.join(colnames)
        insertquery = SQL_INSERT_QUERY % (tablename, insertcolnames, placeholders)
        debug('insert data via query %r' % insertquery)
        cursor.executemany(insertquery, it)
        load_failed = False
    finally:
        if cursor is not None:
            _close_load_cursor(cursor, load_failed)

    if commit:
        debug('commit transaction')
        connection.commit()


def _todb_dbapi_cursor(table, cursor, tablename, schema=None, commit=True,
                       truncate=False):

    # sanitise table name
    tablename = _quote(tablename)
    if schema is not None:
        tablename = _quote(schema) + '.' + tablename
    debug('tablename: %r', tablename)

    # sanitise field names
    it = iter(table)
    hdr = next(it)
    flds = list(map(text_type, hdr))
    colnames = [_quote(n) for n in flds]
    debug('column names: %r', colnames)

    debug('obtain connection via cursor')
    # N.B., we depend on this optional DB-API 2.0 attribute being implemented
    assert hasattr(cursor, 'connection'), \
        'could not obtain connection via cursor'
    connection = cursor.connection

    # determine paramstyle and build placeholders string
    placeholders = _placeholders(connection, colnames)
    debug('placeholders: %r', placeholders)

    if truncate:
        # TRUNCATE is not supported in some databases and causing locks with
        # MySQL used via SQLAlchemy, fall back to DELETE FROM for now
        truncatequery = SQL_TRUNCATE_QUERY % tablename
        debug('truncate the table via query %r', truncatequery)
        cursor.execute(truncatequery)

    insertcolnames = ', '.join(colnames)
    insertquery = SQL_INSERT_QUERY % (tablename, insertcolnames, placeholders)
    debug('insert data via query %r' % insertquery)
    cursor.executemany(insertquery, it)

    # N.B., don't close the cursor, leave that to the application

    if commit:
        debug('commit transaction')
        connection.commit()


def _todb_sqlalchemy_engine(table, engine, tablename, schema=None, commit=True,
                            truncate=False):

    with engine.connect() as connection:
        _todb_sqlalchemy_connection(table, connection, tablename,
                                    schema=schema, commit=commit, truncate=truncate)


def _todb_sqlalchemy_connection(table, connection, tablename, schema=None,
                                commit=True, truncate=False):

    debug('connection: %r', connection)

    # sanitise table name
    tablename = _quote(tablename)
    if schema is not None:
        tablename = _quote(schema) + '.' + tablename
    debug('tablename: %r', tablename)

    # sanitise field names
    it = iter(table)
    hdr = next(it)
    flds = list(map(text_type, hdr))
    colnames = [_quote(n) for n in flds]
    debug('column names: %r', colnames)

    # N.B., we need to obtain a reference to the underlying DB-API connection so
    # we can import the module and determine the paramstyle
    proxied_raw_connection = connection.connection
    if hasattr(proxied_raw_connection, 'driver_connection'):
        actual_raw_connection = proxied_raw_connection.driver_connection
    else:
        actual_raw_connection = proxied_raw_connection.connection

    # determine paramstyle and build placeholders string
    placeholders = _placeholders(actual_raw_connection, colnames)
    debug('placeholders: %r', placeholders)

    with _sqlalchemy_transaction(connection, commit):
        if truncate:
            # TRUNCATE is not supported in some databases and causes locks.
            truncatequery = SQL_TRUNCATE_QUERY % tablename
            debug('truncate the table via query %r', truncatequery)
            _execute_sqlalchemy(connection, truncatequery).close()

        insertcolnames = ', '.join(colnames)
        insertquery = SQL_INSERT_QUERY % (tablename, insertcolnames, placeholders)
        debug('insert data via query %r' % insertquery)
        for row in it:
            _execute_sqlalchemy(connection, insertquery, tuple(row)).close()

    # N.B., don't close connection, leave that to the application


def _todb_sqlalchemy_session(table, session, tablename, schema=None,
                             commit=True, truncate=False):

    with _sqlalchemy_transaction(session, commit):
        _todb_sqlalchemy_connection(table, session.connection(), tablename,
                                    schema=schema, commit=False,
                                    truncate=truncate)


def appenddb(table, dbo, tablename, schema=None, commit=True):
    """
    Load data into an existing database table via a DB-API 2.0
    connection or cursor. As :func:`petl.io.db.todb` except that the database
    table will be appended, i.e., the new data will be inserted into the
    table, and any existing rows will remain.

    """

    needs_closing = False

    # convenience for working with sqlite3
    if isinstance(dbo, string_types):
        import sqlite3
        dbo = sqlite3.connect(dbo)
        needs_closing = True

    try:
        _todb(table, dbo, tablename, schema=schema, commit=commit,
              truncate=False)

    finally:
        if needs_closing:
            dbo.close()


Table.appenddb = appenddb


def historydb(table, dbo, tablename, key, time=None, fromfield='valid_from',
              tofield='valid_to', schema=None, commit=True):
    """
    Maintain a dimension history (a.k.a. slowly changing dimension type 2)
    in an existing database table by applying a table of change events.
    E.g.::

        >>> import petl as etl
        >>> import sqlite3
        >>> connection = sqlite3.connect('example.db')
        >>> # assuming table "people" already exists in the database with
        >>> # columns id, name, valid_from and valid_to
        >>> events = [['id', 'name', 'valid_from'],
        ...           [1, 'foo', 1],
        ...           [1, 'bar', 3],
        ...           [2, 'baz', 2]]
        >>> etl.historydb(events, connection, 'people', key='id')
        >>> etl.look(etl.fromdb(connection,
        ...                     'select * from people order by id, valid_from'))
        +----+-------+------------+----------+
        | id | name  | valid_from | valid_to |
        +====+=======+============+==========+
        |  1 | 'foo' |          1 |        3 |
        +----+-------+------------+----------+
        |  1 | 'bar' |          3 | None     |
        +----+-------+------------+----------+
        |  2 | 'baz' |          2 | None     |
        +----+-------+------------+----------+

    The database table holds one row per version of a business key, with the
    version validity stored as a left-closed right-open interval in the
    `fromfield` (inclusive) and `tofield` (exclusive) columns. The `table`
    argument provides change events, one row per observed attribute set,
    with the business key field(s) given by `key`, the attribute fields and
    a field holding the time each event takes effect (named by `time`,
    defaulting to the value of `fromfield`).

    Events are applied per business key, following the semantics of
    :func:`petl.transform.history.applyhistory`:

    * An event whose time matches the start of an existing version corrects
      that version in place ("same-time correction").
    * An event whose time falls between existing versions is backfilled as a
      new version, regardless of whether it arrives out of chronological
      order.
    * An event that restates the attributes already recorded as in effect at
      its time is a duplicate and does not create a new version.
    * After merging, the validity intervals of the key are recomputed so
      that each version is valid until the next version starts, and the last
      version is open-ended (`tofield` is `None`). Adjacent versions with
      identical attributes are collapsed.

    The database table must already exist, with columns for the key
    field(s), the attribute fields and the two validity interval fields.
    The attribute fields are taken from the database table; the events table
    must provide fields of the same name and may carry additional fields,
    which are ignored. Only keys present in the events are touched, all
    other rows are left unchanged.

    All keys of a batch are updated within a single transaction: if applying
    the events fails for any key, the transaction is rolled back and the
    previously recorded history of all keys is preserved. Pass
    ``commit=False`` to leave transaction control to the caller, as with
    :func:`petl.io.db.todb`.

    The events table is read exactly once, so it may be a one-shot iterator.
    The `dbo` argument may be a DB-API 2.0 connection or cursor, a callable
    returning a DB-API 2.0 cursor, an SQLAlchemy engine, connection or
    session, or a string, in which case it is interpreted as the name of a
    file containing an :mod:`sqlite3` database. Connections, cursors and
    sessions supplied by the caller are left open.

    """

    needs_closing = False

    # convenience for working with sqlite3
    if isinstance(dbo, string_types):
        import sqlite3
        dbo = sqlite3.connect(dbo)
        needs_closing = True

    try:
        _historydb(table, dbo, tablename, key=key, time=time,
                   fromfield=fromfield, tofield=tofield, schema=schema,
                   commit=commit)

    finally:
        if needs_closing:
            dbo.close()


Table.historydb = historydb


def _historydb(table, dbo, tablename, key=None, time=None,
               fromfield='valid_from', tofield='valid_to', schema=None,
               commit=True):

    # need to deal with polymorphic dbo argument
    # what sort of duck is it?

    # does it quack like a standard DB-API 2.0 connection?
    if _is_dbapi_connection(dbo):
        debug('assuming %r is standard DB-API 2.0 connection', dbo)
        _historydb_dbapi_connection(table, dbo, tablename, key=key, time=time,
                                    fromfield=fromfield, tofield=tofield,
                                    schema=schema, commit=commit)

    # does it quack like a standard DB-API 2.0 cursor?
    elif _is_dbapi_cursor(dbo):
        debug('assuming %r is standard DB-API 2.0 cursor')
        _historydb_dbapi_cursor(table, dbo, tablename, key=key, time=time,
                                fromfield=fromfield, tofield=tofield,
                                schema=schema, commit=commit)

    # does it quack like an SQLAlchemy engine?
    elif _is_sqlalchemy_engine(dbo):
        debug('assuming %r instance of sqlalchemy.engine.base.Engine', dbo)
        with dbo.connect() as connection:
            _historydb_sqlalchemy_connection(table, connection, tablename,
                                             key=key, time=time,
                                             fromfield=fromfield,
                                             tofield=tofield, schema=schema,
                                             commit=commit)

    # does it quack like an SQLAlchemy session?
    elif _is_sqlalchemy_session(dbo):
        debug('assuming %r instance of sqlalchemy.orm.session.Session', dbo)
        with _sqlalchemy_transaction(dbo, commit):
            _historydb_sqlalchemy_connection(table, dbo.connection(),
                                             tablename, key=key, time=time,
                                             fromfield=fromfield,
                                             tofield=tofield, schema=schema,
                                             commit=False)

    # does it quack like an SQLAlchemy connection?
    elif _is_sqlalchemy_connection(dbo):
        debug('assuming %r instance of sqlalchemy.engine.base.Connection',
              dbo)
        _historydb_sqlalchemy_connection(table, dbo, tablename, key=key,
                                         time=time, fromfield=fromfield,
                                         tofield=tofield, schema=schema,
                                         commit=commit)

    elif callable(dbo):
        debug('assuming %r is a function returning standard DB-API 2.0 '
              'cursor objects', dbo)
        _historydb_dbapi_mkcurs(table, dbo, tablename, key=key, time=time,
                                fromfield=fromfield, tofield=tofield,
                                schema=schema, commit=commit)

    # some other sort of duck...
    else:
        raise ArgumentError('unsupported database object type: %r' % dbo)


SQL_SELECT_COLUMNS_QUERY = 'SELECT * FROM %s WHERE 1=0'
SQL_SELECT_VERSIONS_QUERY = 'SELECT %s FROM %s WHERE %s'
SQL_DELETE_VERSIONS_QUERY = 'DELETE FROM %s WHERE %s'


class _DbapiHistoryWriter(object):
    # execute the statements of a history update via DB-API 2.0

    def __init__(self, cursor):
        self.cursor = cursor

    def columns(self, tablename):
        self.cursor.execute(SQL_SELECT_COLUMNS_QUERY % tablename)
        return [d[0] for d in self.cursor.description]

    def select(self, query, params):
        self.cursor.execute(query, params)
        return [tuple(row) for row in self.cursor.fetchall()]

    def delete(self, query, params):
        self.cursor.execute(query, params)

    def insert(self, query, rows):
        self.cursor.executemany(query, rows)


class _SQLAlchemyHistoryWriter(object):
    # execute the statements of a history update via SQLAlchemy

    def __init__(self, connection):
        self.connection = connection

    def columns(self, tablename):
        result = _execute_sqlalchemy(self.connection,
                                     SQL_SELECT_COLUMNS_QUERY % tablename)
        try:
            return list(result.keys())
        finally:
            result.close()

    def select(self, query, params):
        result = _execute_sqlalchemy(self.connection, query, params)
        try:
            return [tuple(row) for row in result]
        finally:
            result.close()

    def delete(self, query, params):
        _execute_sqlalchemy(self.connection, query, params).close()

    def insert(self, query, rows):
        for row in rows:
            _execute_sqlalchemy(self.connection, query, tuple(row)).close()


def _applyhistorydb(table, writer, paramstyle_connection, tablename, key,
                    time, fromfield, tofield, schema):

    # normalise the key to a tuple of field names
    if not isinstance(key, (list, tuple)):
        key = (key,)
    key = tuple(key)

    # sanitise table name
    tablename = _quote(tablename)
    if schema is not None:
        tablename = _quote(schema) + '.' + tablename
    debug('tablename: %r', tablename)

    # discover the columns of the database table
    dbcols = list(writer.columns(tablename))
    debug('table columns: %r', dbcols)

    # determine the attribute fields from the database table
    for name in key + (fromfield, tofield):
        if name not in dbcols:
            raise ArgumentError('field %r not found in database table %r, '
                                'columns are %r' % (name, tablename, dbcols))
    attrs = [c for c in dbcols
             if c not in key and c != fromfield and c != tofield]
    debug('attribute fields: %r', attrs)

    # read the events table exactly once, grouping events per business key
    if time is None:
        time = fromfield
    it = iter(table)
    try:
        hdr = next(it)
    except StopIteration:
        debug('no events, nothing to do')
        return
    eflds = list(map(text_type, hdr))
    for name in key + (time,) + tuple(attrs):
        if name not in eflds:
            raise ArgumentError('field %r not found in the events table, '
                                'fields are %r' % (name, eflds))
    ekindices = [eflds.index(name) for name in key]
    etindex = eflds.index(time)
    eaindices = [eflds.index(name) for name in attrs]
    groups = dict()
    order = list()
    for row in it:
        k = tuple(row[i] for i in ekindices)
        if k not in groups:
            groups[k] = list()
            order.append(k)
        groups[k].append((row[etindex],
                          tuple(row[i] for i in eaindices)))

    # prepare the statements
    colnames = [_quote(c) for c in dbcols]
    insertcolnames = ', '.join(colnames)
    placeholders = _placeholders(paramstyle_connection, dbcols)
    insertquery = SQL_INSERT_QUERY % (tablename, insertcolnames, placeholders)
    keycolnames = [_quote(k) for k in key]
    keyplaceholders = _placeholders(paramstyle_connection, key).split(', ')
    whereclause = ' AND '.join(
        '%s = %s' % pair for pair in zip(keycolnames, keyplaceholders))
    selectquery = SQL_SELECT_VERSIONS_QUERY % (insertcolnames, tablename,
                                               whereclause)
    deletequery = SQL_DELETE_VERSIONS_QUERY % (tablename, whereclause)
    debug('select versions via query %r', selectquery)
    debug('delete versions via query %r', deletequery)
    debug('insert versions via query %r', insertquery)

    # column positions within a database row
    dbfrom = dbcols.index(fromfield)
    dbattrs = [dbcols.index(a) for a in attrs]
    keypos = dict((c, i) for i, c in enumerate(key))
    attrpos = dict((c, i) for i, c in enumerate(attrs))

    # apply the events of each key within the caller's transaction
    for k in order:
        existing_rows = writer.select(selectquery, k)
        existing = [(row[dbfrom], tuple(row[i] for i in dbattrs))
                    for row in existing_rows]
        merged = _mergeversions(existing, groups[k])
        # build the revised version rows in database column order,
        # recomputing the left-closed right-open validity intervals
        rows = list()
        n = len(merged)
        for i, (valid_from, avals) in enumerate(merged):
            valid_to = merged[i + 1][0] if i + 1 < n else None
            row = list()
            for c in dbcols:
                if c in keypos:
                    row.append(k[keypos[c]])
                elif c == fromfield:
                    row.append(valid_from)
                elif c == tofield:
                    row.append(valid_to)
                else:
                    row.append(avals[attrpos[c]])
            rows.append(tuple(row))
        # skip the rewrite if nothing changed, e.g., all events were
        # duplicates of the recorded history
        current = sorted(existing_rows,
                         key=lambda row: Comparable(row[dbfrom]))
        if rows == current:
            debug('history of key %r unchanged, skipping', k)
            continue
        debug('replace %r versions of key %r with %r versions',
              len(existing_rows), k, len(rows))
        writer.delete(deletequery, k)
        writer.insert(insertquery, rows)


def _historydb_dbapi_connection(table, connection, tablename, key=None,
                                time=None, fromfield='valid_from',
                                tofield='valid_to', schema=None, commit=True):

    cursor = connection.cursor()
    load_failed = True
    try:
        try:
            _applyhistorydb(table, _DbapiHistoryWriter(cursor), connection,
                            tablename, key, time, fromfield, tofield, schema)
            load_failed = False
        except Exception:
            if commit:
                # roll back so the previously recorded history is preserved
                debug('roll back transaction after failed history update')
                connection.rollback()
            raise
    finally:
        debug('close the cursor')
        _close_load_cursor(cursor, load_failed)

    if commit:
        debug('commit transaction')
        connection.commit()


def _historydb_dbapi_cursor(table, cursor, tablename, key=None, time=None,
                            fromfield='valid_from', tofield='valid_to',
                            schema=None, commit=True):

    debug('obtain connection via cursor')
    # N.B., we depend on this optional DB-API 2.0 attribute being implemented
    assert hasattr(cursor, 'connection'), \
        'could not obtain connection via cursor'
    connection = cursor.connection

    try:
        _applyhistorydb(table, _DbapiHistoryWriter(cursor), connection,
                        tablename, key, time, fromfield, tofield, schema)
    except Exception:
        if commit:
            # roll back so the previously recorded history is preserved
            debug('roll back transaction after failed history update')
            connection.rollback()
        raise

    # N.B., don't close the cursor, leave that to the application

    if commit:
        debug('commit transaction')
        connection.commit()


def _historydb_dbapi_mkcurs(table, mkcurs, tablename, key=None, time=None,
                            fromfield='valid_from', tofield='valid_to',
                            schema=None, commit=True):

    debug('obtain cursor and connection')
    cursor = mkcurs()
    # N.B., we depend on this optional DB-API 2.0 attribute being implemented
    assert hasattr(cursor, 'connection'), \
        'could not obtain connection via cursor'
    connection = cursor.connection

    load_failed = True
    try:
        try:
            _applyhistorydb(table, _DbapiHistoryWriter(cursor), connection,
                            tablename, key, time, fromfield, tofield, schema)
            load_failed = False
        except Exception:
            if commit:
                # roll back so the previously recorded history is preserved
                debug('roll back transaction after failed history update')
                connection.rollback()
            raise
    finally:
        _close_load_cursor(cursor, load_failed)

    if commit:
        debug('commit transaction')
        connection.commit()


def _historydb_sqlalchemy_connection(table, connection, tablename, key=None,
                                     time=None, fromfield='valid_from',
                                     tofield='valid_to', schema=None,
                                     commit=True):

    debug('connection: %r', connection)

    # N.B., we need to obtain a reference to the underlying DB-API connection
    # so we can import the module and determine the paramstyle
    proxied_raw_connection = connection.connection
    if hasattr(proxied_raw_connection, 'driver_connection'):
        actual_raw_connection = proxied_raw_connection.driver_connection
    else:
        actual_raw_connection = proxied_raw_connection.connection

    with _sqlalchemy_transaction(connection, commit):
        _applyhistorydb(table, _SQLAlchemyHistoryWriter(connection),
                        actual_raw_connection, tablename, key, time,
                        fromfield, tofield, schema)

    # N.B., don't close connection, leave that to the application
