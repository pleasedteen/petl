# -*- coding: utf-8 -*-
from __future__ import absolute_import, print_function, division


import os
import sqlite3
from tempfile import NamedTemporaryFile

import pytest

from petl.errors import ArgumentError
from petl.io.db import historydb
from petl.test.helpers import eq_


# N.B., this file only tests historydb() using sqlite3, as anything else
# requires database connection configuration.


def _tmpfile():
    f = NamedTemporaryFile(delete=False)
    f.close()
    return f.name


def _rows(connection, tablename):
    return list(connection.execute(
        'select * from %s order by 1, 3' % tablename))


def test_historydb_basic():
    connection = sqlite3.connect(':memory:')
    connection.execute('create table dim (id, name, valid_from, valid_to)')

    # initial load starts one open-ended version per key
    events = (('id', 'name', 'valid_from'),
              (1, 'foo', 1),
              (2, 'baz', 2))
    historydb(events, connection, 'dim', key='id')
    expect = [(1, 'foo', 1, None),
              (2, 'baz', 2, None)]
    eq_(expect, _rows(connection, 'dim'))

    # apply a batch with an out-of-order backfill, a same-time correction
    # and a duplicate event
    historydb((('id', 'name', 'valid_from'),
               (1, 'bar', 3),
               (1, 'qux', 1),
               (2, 'baz', 5)),
              connection, 'dim', key='id')
    expect = [(1, 'qux', 1, 3),
              (1, 'bar', 3, None),
              (2, 'baz', 2, None)]
    eq_(expect, _rows(connection, 'dim'))

    # a batch of duplicate events changes nothing
    historydb((('id', 'name', 'valid_from'),
               (1, 'qux', 1),
               (1, 'bar', 4),
               (2, 'baz', 2)),
              connection, 'dim', key='id')
    eq_(expect, _rows(connection, 'dim'))

    connection.close()


def test_historydb_intervals_recomputed():
    connection = sqlite3.connect(':memory:')
    connection.execute('create table dim (id, name, valid_from, valid_to)')
    historydb((('id', 'name', 'valid_from'),
               (1, 'foo', 1),
               (1, 'bar', 5)),
              connection, 'dim', key='id')
    # backfill between the two versions, intervals are recomputed so that
    # each version is valid until the next one starts
    historydb((('id', 'name', 'valid_from'),
               (1, 'baz', 3)),
              connection, 'dim', key='id')
    expect = [(1, 'foo', 1, 3),
              (1, 'baz', 3, 5),
              (1, 'bar', 5, None)]
    eq_(expect, _rows(connection, 'dim'))
    connection.close()


def test_historydb_atomic_batch_rollback():
    connection = sqlite3.connect(':memory:')
    connection.execute(
        'create table dim (id, name not null, valid_from, valid_to)')
    historydb((('id', 'name', 'valid_from'),
               (1, 'foo', 1),
               (2, 'baz', 2)),
              connection, 'dim', key='id')
    before = _rows(connection, 'dim')

    # the event for key 2 violates the not null constraint, so the whole
    # batch must be rolled back, preserving the history of all keys
    events = (('id', 'name', 'valid_from'),
              (1, 'bar', 3),
              (2, None, 4))
    with pytest.raises(sqlite3.IntegrityError):
        historydb(events, connection, 'dim', key='id')
    eq_(before, _rows(connection, 'dim'))

    # the connection is still usable after the failed batch
    historydb((('id', 'name', 'valid_from'),
               (1, 'bar', 3)),
              connection, 'dim', key='id')
    eq_([(1, 'foo', 1, 3),
         (1, 'bar', 3, None),
         (2, 'baz', 2, None)],
        _rows(connection, 'dim'))
    connection.close()


def test_historydb_connection_remains_usable():
    connection = sqlite3.connect(':memory:')
    connection.execute('create table dim (id, name, valid_from, valid_to)')
    historydb((('id', 'name', 'valid_from'),
               (1, 'foo', 1)),
              connection, 'dim', key='id')
    # the externally supplied connection is left open and usable
    eq_([(1, 'foo', 1, None)], _rows(connection, 'dim'))
    connection.execute('create table other (foo)')
    connection.execute('insert into other values (1)')
    connection.commit()
    eq_([(1,)], list(connection.execute('select * from other')))
    connection.close()


def test_historydb_cursor():
    connection = sqlite3.connect(':memory:')
    connection.execute('create table dim (id, name, valid_from, valid_to)')
    cursor = connection.cursor()
    historydb((('id', 'name', 'valid_from'),
               (1, 'foo', 1),
               (1, 'bar', 3)),
              cursor, 'dim', key='id')
    # the supplied cursor is left open
    cursor.execute('select * from dim order by valid_from')
    eq_([(1, 'foo', 1, 3), (1, 'bar', 3, None)], cursor.fetchall())
    cursor.close()
    connection.close()


def test_historydb_mkcurs():
    connection = sqlite3.connect(':memory:')
    connection.execute('create table dim (id, name, valid_from, valid_to)')

    def mkcurs():
        return connection.cursor()

    historydb((('id', 'name', 'valid_from'),
               (1, 'foo', 1),
               (1, 'bar', 3)),
              mkcurs, 'dim', key='id')
    eq_([(1, 'foo', 1, 3), (1, 'bar', 3, None)],
        _rows(connection, 'dim'))
    connection.close()


def test_historydb_filename():
    f = _tmpfile()
    try:
        connection = sqlite3.connect(f)
        connection.execute('create table dim (id, name, valid_from, '
                           'valid_to)')
        connection.commit()
        connection.close()

        # a string is interpreted as the name of a sqlite3 database file
        historydb((('id', 'name', 'valid_from'),
                   (1, 'foo', 1)),
                  f, 'dim', key='id')

        connection = sqlite3.connect(f)
        eq_([(1, 'foo', 1, None)], _rows(connection, 'dim'))
        connection.close()
    finally:
        os.unlink(f)


def test_historydb_commit_false():
    connection = sqlite3.connect(':memory:')
    connection.execute('create table dim (id, name, valid_from, valid_to)')
    historydb((('id', 'name', 'valid_from'),
               (1, 'foo', 1)),
              connection, 'dim', key='id', commit=False)
    connection.rollback()
    eq_([], _rows(connection, 'dim'))
    historydb((('id', 'name', 'valid_from'),
               (1, 'foo', 1)),
              connection, 'dim', key='id', commit=False)
    connection.commit()
    eq_([(1, 'foo', 1, None)], _rows(connection, 'dim'))
    connection.close()


def test_historydb_single_pass_events():
    # the events table is read exactly once, so it may be a one-shot
    # iterator
    connection = sqlite3.connect(':memory:')
    connection.execute('create table dim (id, name, valid_from, valid_to)')
    events = iter([('id', 'name', 'valid_from'),
                   (1, 'foo', 1),
                   (1, 'bar', 3),
                   (2, 'baz', 2)])
    historydb(events, connection, 'dim', key='id')
    eq_([(1, 'foo', 1, 3),
         (1, 'bar', 3, None),
         (2, 'baz', 2, None)],
        _rows(connection, 'dim'))
    connection.close()


def test_historydb_compound_key():
    connection = sqlite3.connect(':memory:')
    connection.execute(
        'create table dim (id, subid, name, valid_from, valid_to)')
    historydb((('id', 'subid', 'name', 'valid_from'),
               (1, 'a', 'foo', 1),
               (1, 'b', 'bar', 2)),
              connection, 'dim', key=('id', 'subid'))
    historydb((('id', 'subid', 'name', 'valid_from'),
               (1, 'b', 'baz', 5)),
              connection, 'dim', key=('id', 'subid'))
    actual = list(connection.execute(
        'select * from dim order by id, subid, valid_from'))
    expect = [(1, 'a', 'foo', 1, None),
              (1, 'b', 'bar', 2, 5),
              (1, 'b', 'baz', 5, None)]
    eq_(expect, actual)
    connection.close()


def test_historydb_custom_fields():
    connection = sqlite3.connect(':memory:')
    connection.execute('create table dim (id, name, start, end)')
    historydb((('id', 'name', 'ts'),
               (1, 'foo', 1),
               (1, 'bar', 3)),
              connection, 'dim', key='id', time='ts', fromfield='start',
              tofield='end')
    eq_([(1, 'foo', 1, 3), (1, 'bar', 3, None)],
        list(connection.execute('select * from dim order by start')))
    connection.close()


def test_historydb_extra_event_fields_ignored():
    connection = sqlite3.connect(':memory:')
    connection.execute('create table dim (id, name, valid_from, valid_to)')
    historydb((('id', 'name', 'source', 'valid_from'),
               (1, 'foo', 'web', 1)),
              connection, 'dim', key='id')
    eq_([(1, 'foo', 1, None)], _rows(connection, 'dim'))
    connection.close()


def test_historydb_untouched_keys_preserved():
    connection = sqlite3.connect(':memory:')
    connection.execute('create table dim (id, name, valid_from, valid_to)')
    historydb((('id', 'name', 'valid_from'),
               (1, 'foo', 1),
               (2, 'bar', 2),
               (3, 'baz', 3)),
              connection, 'dim', key='id')
    # only key 2 is touched by the second batch
    historydb((('id', 'name', 'valid_from'),
               (2, 'qux', 5)),
              connection, 'dim', key='id')
    eq_([(1, 'foo', 1, None),
         (2, 'bar', 2, 5),
         (2, 'qux', 5, None),
         (3, 'baz', 3, None)],
        _rows(connection, 'dim'))
    connection.close()


def test_historydb_empty_events():
    connection = sqlite3.connect(':memory:')
    connection.execute('create table dim (id, name, valid_from, valid_to)')
    historydb((('id', 'name', 'valid_from'),),
              connection, 'dim', key='id')
    eq_([], _rows(connection, 'dim'))
    connection.close()


def test_historydb_missing_event_field():
    connection = sqlite3.connect(':memory:')
    connection.execute('create table dim (id, name, valid_from, valid_to)')
    # the events table lacks the attribute field "name"
    with pytest.raises(ArgumentError):
        historydb((('id', 'valid_from'),
                   (1, 1)),
                  connection, 'dim', key='id')
    # the events table lacks the time field
    with pytest.raises(ArgumentError):
        historydb((('id', 'name', 'ts'),
                   (1, 'foo', 1)),
                  connection, 'dim', key='id')
    # the key field is not in the database table
    with pytest.raises(ArgumentError):
        historydb((('id', 'name', 'valid_from'),
                   (1, 'foo', 1)),
                  connection, 'dim', key='subid')
    connection.close()


def test_historydb_unsupported_dbo():
    with pytest.raises(ArgumentError):
        historydb((('id', 'name', 'valid_from'),
                   (1, 'foo', 1)),
                  object(), 'dim', key='id')
