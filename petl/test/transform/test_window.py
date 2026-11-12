from __future__ import absolute_import, print_function, division


import os

import pytest

from petl.errors import FieldSelectionError
from petl.test.helpers import ieq
from petl.transform.window import groupwindow


def test_groupwindow():

    table = (('region', 'store', 'day', 'sales'),
             ('north', 'a', 2, 20),
             ('north', 'a', 1, 10),
             ('north', 'a', 3, None),
             ('north', 'b', 1, 5),
             ('north', 'b', 1, 7),
             ('south', 'c', 1, 100),
             ('south', 'c', 2, 50))

    actual = groupwindow(table, key=['region', 'store'], value='sales',
                         sort='day', preceding=1, following=1)
    expect = (('region', 'store', 'day', 'sales', 'rank', 'lag',
               'window_sum'),
              ('north', 'a', 2, 20, 2, 10, 30),
              ('north', 'a', 1, 10, 1, None, 30),
              ('north', 'a', 3, None, 3, 20, 20),
              ('north', 'b', 1, 5, 1, None, 12),
              ('north', 'b', 1, 7, 1, 5, 12),
              ('south', 'c', 1, 100, 1, None, 150),
              ('south', 'c', 2, 50, 2, 100, 150))
    ieq(expect, actual)
    ieq(expect, actual)  # check can iterate twice


def test_groupwindow_simple_key():

    table = (('foo', 'bar'),
             ('a', 3),
             ('a', 1),
             ('b', 2),
             ('a', 2),
             ('b', 1))

    actual = groupwindow(table, key='foo', value='bar', sort='bar')
    expect = (('foo', 'bar', 'rank', 'lag', 'window_sum'),
              ('a', 3, 3, 2, 3),
              ('a', 1, 1, None, 1),
              ('b', 2, 2, 1, 2),
              ('a', 2, 2, 1, 2),
              ('b', 1, 1, None, 1))
    ieq(expect, actual)
    ieq(expect, actual)


def test_groupwindow_rank_gaps():
    # rows tied on the sort field share a rank, following ranks are skipped
    table = (('foo', 'bar', 'baz'),
             ('a', 5, 'x'),
             ('a', 1, 'y'),
             ('a', 5, 'z'),
             ('a', 9, 'w'))

    actual = groupwindow(table, key='foo', value='baz', sort='bar')
    expect = (('foo', 'bar', 'baz', 'rank', 'lag', 'window_sum'),
              ('a', 5, 'x', 2, 'y', 'x'),
              ('a', 1, 'y', 1, None, 'y'),
              ('a', 5, 'z', 2, 'x', 'z'),
              ('a', 9, 'w', 4, 'z', 'w'))
    ieq(expect, actual)
    ieq(expect, actual)


def test_groupwindow_stable():
    # rows with equal sort values keep their original relative order
    table = (('foo', 'bar', 'baz'),
             ('a', 1, 'first'),
             ('a', 1, 'second'),
             ('a', 1, 'third'))

    actual = groupwindow(table, key='foo', value='baz', sort='bar')
    expect = (('foo', 'bar', 'baz', 'rank', 'lag', 'window_sum'),
              ('a', 1, 'first', 1, None, 'first'),
              ('a', 1, 'second', 1, 'first', 'second'),
              ('a', 1, 'third', 1, 'second', 'third'))
    ieq(expect, actual)
    ieq(expect, actual)


def test_groupwindow_none_values():
    # None values are ignored by the window sum; if all values in the
    # window are None, the sum is None
    table = (('foo', 'bar'),
             ('a', None),
             ('a', None),
             ('a', 5),
             ('b', None),
             ('b', None))

    actual = groupwindow(table, key='foo', value='bar',
                         preceding=1, following=1)
    expect = (('foo', 'bar', 'rank', 'lag', 'window_sum'),
              ('a', None, 1, None, None),
              ('a', None, 2, None, 5),
              ('a', 5, 3, None, 5),
              ('b', None, 1, None, None),
              ('b', None, 2, None, None))
    ieq(expect, actual)
    ieq(expect, actual)


def test_groupwindow_window_edges():
    # windows are clipped at the group boundaries
    table = (('foo', 'bar'),
             ('a', 1),
             ('a', 2),
             ('a', 3))

    actual = groupwindow(table, key='foo', value='bar',
                         preceding=10, following=10)
    expect = (('foo', 'bar', 'rank', 'lag', 'window_sum'),
              ('a', 1, 1, None, 6),
              ('a', 2, 2, 1, 6),
              ('a', 3, 3, 2, 6))
    ieq(expect, actual)
    ieq(expect, actual)

    # window covering the current row only
    actual = groupwindow(table, key='foo', value='bar',
                         preceding=0, following=0)
    expect = (('foo', 'bar', 'rank', 'lag', 'window_sum'),
              ('a', 1, 1, None, 1),
              ('a', 2, 2, 1, 2),
              ('a', 3, 3, 2, 3))
    ieq(expect, actual)
    ieq(expect, actual)


def test_groupwindow_sort_none():
    # no sort field: rows keep their input order within each group and
    # rank is the row number within the group
    table = (('foo', 'bar'),
             ('a', 1),
             ('a', 2),
             ('b', 5),
             ('a', 9))

    actual = groupwindow(table, key='foo', value='bar')
    expect = (('foo', 'bar', 'rank', 'lag', 'window_sum'),
              ('a', 1, 1, None, 1),
              ('a', 2, 2, 1, 2),
              ('b', 5, 1, None, 5),
              ('a', 9, 3, 2, 9))
    ieq(expect, actual)
    ieq(expect, actual)


def test_groupwindow_key_none():
    # no key: the whole table is a single group
    table = (('bar',),
             (3,),
             (1,),
             (2,))

    actual = groupwindow(table, key=None, value='bar', sort='bar',
                         preceding=1, following=1)
    expect = (('bar', 'rank', 'lag', 'window_sum'),
              (3, 3, 2, 5),
              (1, 1, None, 3),
              (2, 2, 1, 6))
    ieq(expect, actual)
    ieq(expect, actual)


def test_groupwindow_compound_sort():
    # multiple sort fields are supported
    table = (('foo', 'bar', 'baz', 'qux'),
             ('a', 1, 2, 'x'),
             ('a', 1, 1, 'y'),
             ('a', 0, 5, 'z'))

    actual = groupwindow(table, key='foo', value='qux', sort=['bar', 'baz'])
    expect = (('foo', 'bar', 'baz', 'qux', 'rank', 'lag', 'window_sum'),
              ('a', 1, 2, 'x', 3, 'y', 'x'),
              ('a', 1, 1, 'y', 2, 'z', 'y'),
              ('a', 0, 5, 'z', 1, None, 'z'))
    ieq(expect, actual)
    ieq(expect, actual)


def test_groupwindow_field_names():
    # the names of the output fields can be changed, and fields can be
    # omitted by passing None
    table = (('foo', 'bar'),
             ('a', 1),
             ('a', 2))

    actual = groupwindow(table, key='foo', value='bar', rank='r',
                         lag='previous', windowsum='total')
    expect = (('foo', 'bar', 'r', 'previous', 'total'),
              ('a', 1, 1, None, 1),
              ('a', 2, 2, 1, 2))
    ieq(expect, actual)
    ieq(expect, actual)

    actual = groupwindow(table, key='foo', value='bar', rank=None, lag=None)
    expect = (('foo', 'bar', 'window_sum'),
              ('a', 1, 1),
              ('a', 2, 2))
    ieq(expect, actual)
    ieq(expect, actual)

    actual = groupwindow(table, key='foo', value='bar', windowsum=None)
    expect = (('foo', 'bar', 'rank', 'lag'),
              ('a', 1, 1, None),
              ('a', 2, 2, 1))
    ieq(expect, actual)
    ieq(expect, actual)


def test_groupwindow_empty():

    table = (('foo', 'bar'),)
    actual = groupwindow(table, key='foo', value='bar')
    expect = (('foo', 'bar', 'rank', 'lag', 'window_sum'),)
    ieq(expect, actual)
    ieq(expect, actual)


def test_groupwindow_headerless():

    table = []
    actual = groupwindow(table, key='foo', value='bar')
    expect = []
    ieq(expect, actual)
    ieq(expect, actual)


def test_groupwindow_buffered():
    # a small buffersize forces sorting in chunks via temporary files;
    # results must be the same as sorting in memory
    table = (('foo', 'bar'),) \
        + tuple(('a', i % 10) for i in range(100)) \
        + tuple(('b', i % 7) for i in range(100))

    reference = groupwindow(table, key='foo', value='bar', sort='bar',
                            preceding=2, following=2)
    actual = groupwindow(table, key='foo', value='bar', sort='bar',
                         preceding=2, following=2, buffersize=5)
    ieq(reference, actual)
    ieq(reference, actual)


def test_groupwindow_buffered_tempdir():

    table = (('foo', 'bar'),
             ('C', 2),
             ('A', 9),
             ('A', 6),
             ('F', 1),
             ('D', 10))

    expectation = (('foo', 'bar', 'rank', 'lag', 'window_sum'),
                   ('C', 2, 1, None, 2),
                   ('A', 9, 2, 6, 9),
                   ('A', 6, 1, None, 6),
                   ('F', 1, 1, None, 1),
                   ('D', 10, 1, None, 10))
    tempdir = 'tmp'
    if not os.path.exists(tempdir):
        os.mkdir(tempdir)
    actual = groupwindow(table, key='foo', value='bar', sort='bar',
                         buffersize=2, tempdir=tempdir)
    ieq(expectation, actual)
    ieq(expectation, actual)


def test_groupwindow_large_group():
    # a single group larger than the sort buffer is processed in a
    # streaming fashion
    n = 1000
    table = (('foo', 'bar'),) + tuple(('a', i) for i in range(n))

    actual = groupwindow(table, key='foo', value='bar',
                         preceding=1, following=1, buffersize=17)
    expect = (('foo', 'bar', 'rank', 'lag', 'window_sum'),) \
        + tuple(('a', i, i + 1, i - 1 if i > 0 else None,
                 (i - 1 if i > 0 else 0) + i + (i + 1 if i < n - 1 else 0))
                for i in range(n))
    ieq(expect, actual)
    ieq(expect, actual)


def test_groupwindow_single_pass():
    # the input table is read exactly once per iteration, so one-shot
    # generators are supported
    def source():
        yield ('foo', 'bar')
        yield ('a', 2)
        yield ('a', 1)
        yield ('b', 3)

    actual = groupwindow(source(), key='foo', value='bar', sort='bar',
                         preceding=1, following=0)
    expect = (('foo', 'bar', 'rank', 'lag', 'window_sum'),
              ('a', 2, 2, 1, 3),
              ('a', 1, 1, None, 1),
              ('b', 3, 1, None, 3))
    ieq(expect, actual)


def test_groupwindow_chained():
    # the function is also available as a method on table objects, so it
    # can be used in a fluent (chained) style
    import petl as etl

    table = (('foo', 'bar'),
             ('a', 2),
             ('a', 1),
             ('b', 3))

    actual = etl.wrap(table).groupwindow('foo', 'bar', sort='bar')
    expect = (('foo', 'bar', 'rank', 'lag', 'window_sum'),
              ('a', 2, 2, 1, 2),
              ('a', 1, 1, None, 1),
              ('b', 3, 1, None, 3))
    ieq(expect, actual)
    ieq(expect, actual)


def test_groupwindow_indices():
    # fields can also be selected by index
    table = (('foo', 'bar'),
             ('a', 2),
             ('a', 1))

    actual = groupwindow(table, key=0, value=1, sort=1)
    expect = (('foo', 'bar', 'rank', 'lag', 'window_sum'),
              ('a', 2, 2, 1, 2),
              ('a', 1, 1, None, 1))
    ieq(expect, actual)
    ieq(expect, actual)


def test_groupwindow_errors():

    table = (('foo', 'bar'),
             ('a', 1))

    # window offsets must be non-negative integers
    with pytest.raises(ValueError):
        groupwindow(table, key='foo', value='bar', preceding=-1)
    with pytest.raises(ValueError):
        groupwindow(table, key='foo', value='bar', following=-1)
    with pytest.raises(ValueError):
        groupwindow(table, key='foo', value='bar', preceding='1')

    # output field names must be distinct
    with pytest.raises(ValueError):
        groupwindow(table, key='foo', value='bar', rank='x', lag='x')

    # output field names must not conflict with existing fields
    with pytest.raises(ValueError):
        list(groupwindow(table, key='foo', value='bar', lag='foo'))

    # key, value and sort fields must exist
    with pytest.raises(FieldSelectionError):
        list(groupwindow(table, key='baz', value='bar'))
    with pytest.raises(FieldSelectionError):
        list(groupwindow(table, key='foo', value='baz'))
    with pytest.raises(FieldSelectionError):
        list(groupwindow(table, key='foo', value='bar', sort='baz'))
