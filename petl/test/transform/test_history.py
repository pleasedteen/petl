# -*- coding: utf-8 -*-
from __future__ import absolute_import, print_function, division


from petl.test.helpers import ieq, eq_
from petl.transform.history import applyhistory, _mergeversions


def test_applyhistory_backfill_out_of_order():
    # events arriving out of chronological order are backfilled into the
    # right position of the history
    history = (('id', 'name', 'valid_from', 'valid_to'),
               (1, 'foo', 1, 5),
               (1, 'baz', 5, None))
    events = (('id', 'name', 'valid_from'),
              (1, 'bar', 3))
    actual = applyhistory(history, events, key='id')
    expect = (('id', 'name', 'valid_from', 'valid_to'),
              (1, 'foo', 1, 3),
              (1, 'bar', 3, 5),
              (1, 'baz', 5, None))
    ieq(expect, actual)
    ieq(expect, actual)  # verify can iterate twice


def test_applyhistory_backfill_before_all_versions():
    # an event older than every recorded version starts the history
    history = (('id', 'name', 'valid_from', 'valid_to'),
               (1, 'foo', 5, None))
    events = (('id', 'name', 'valid_from'),
              (1, 'bar', 2))
    actual = applyhistory(history, events, key='id')
    expect = (('id', 'name', 'valid_from', 'valid_to'),
              (1, 'bar', 2, 5),
              (1, 'foo', 5, None))
    ieq(expect, actual)


def test_applyhistory_same_time_correction():
    # an event falling on the start of an existing version corrects the
    # version in place
    history = (('id', 'name', 'valid_from', 'valid_to'),
               (1, 'foo', 1, 5),
               (1, 'bar', 5, None))
    events = (('id', 'name', 'valid_from'),
              (1, 'qux', 5))
    actual = applyhistory(history, events, key='id')
    expect = (('id', 'name', 'valid_from', 'valid_to'),
              (1, 'foo', 1, 5),
              (1, 'qux', 5, None))
    ieq(expect, actual)


def test_applyhistory_correction_collapses_adjacent_versions():
    # a correction making a version identical to its predecessor collapses
    # the redundant version
    history = (('id', 'name', 'valid_from', 'valid_to'),
               (1, 'foo', 1, 5),
               (1, 'bar', 5, None))
    events = (('id', 'name', 'valid_from'),
              (1, 'foo', 5))
    actual = applyhistory(history, events, key='id')
    expect = (('id', 'name', 'valid_from', 'valid_to'),
              (1, 'foo', 1, None))
    ieq(expect, actual)


def test_applyhistory_duplicate_events_add_no_version():
    history = (('id', 'name', 'valid_from', 'valid_to'),
               (1, 'foo', 1, 5),
               (1, 'bar', 5, None))
    events = (('id', 'name', 'valid_from'),
              (1, 'foo', 1),  # restates the version starting at 1
              (1, 'bar', 5),  # restates the version starting at 5
              (1, 'foo', 3),  # restates the attributes in effect at 3
              (1, 'bar', 9))  # restates the attributes in effect at 9
    actual = applyhistory(history, events, key='id')
    expect = (('id', 'name', 'valid_from', 'valid_to'),
              (1, 'foo', 1, 5),
              (1, 'bar', 5, None))
    ieq(expect, actual)


def test_applyhistory_new_key():
    history = (('id', 'name', 'valid_from', 'valid_to'),
               (1, 'foo', 1, None))
    events = (('id', 'name', 'valid_from'),
              (2, 'bar', 3),
              (2, 'baz', 1))
    actual = applyhistory(history, events, key='id')
    expect = (('id', 'name', 'valid_from', 'valid_to'),
              (1, 'foo', 1, None),
              (2, 'baz', 1, 3),
              (2, 'bar', 3, None))
    ieq(expect, actual)


def test_applyhistory_untouched_keys_pass_through():
    history = (('id', 'name', 'valid_from', 'valid_to'),
               (1, 'foo', 1, 4),
               (1, 'bar', 4, None),
               (3, 'baz', 2, None))
    events = (('id', 'name', 'valid_from'),
              (2, 'qux', 1))
    actual = applyhistory(history, events, key='id')
    expect = (('id', 'name', 'valid_from', 'valid_to'),
              (1, 'foo', 1, 4),
              (1, 'bar', 4, None),
              (2, 'qux', 1, None),
              (3, 'baz', 2, None))
    ieq(expect, actual)


def test_applyhistory_compound_key():
    history = (('id', 'subid', 'name', 'valid_from', 'valid_to'),
               (1, 'a', 'foo', 1, None),
               (1, 'b', 'bar', 2, None))
    events = (('id', 'subid', 'name', 'valid_from'),
              (1, 'b', 'baz', 5),
              (1, 'b', 'bar', 3))  # duplicate, no new version
    actual = applyhistory(history, events, key=('id', 'subid'))
    expect = (('id', 'subid', 'name', 'valid_from', 'valid_to'),
              (1, 'a', 'foo', 1, None),
              (1, 'b', 'bar', 2, 5),
              (1, 'b', 'baz', 5, None))
    ieq(expect, actual)


def test_applyhistory_custom_fields():
    history = (('id', 'name', 'start', 'end'),
               (1, 'foo', 1, None))
    events = (('id', 'name', 'ts'),
              (1, 'bar', 3))
    actual = applyhistory(history, events, key='id', time='ts',
                          fromfield='start', tofield='end')
    expect = (('id', 'name', 'start', 'end'),
              (1, 'foo', 1, 3),
              (1, 'bar', 3, None))
    ieq(expect, actual)


def test_applyhistory_extra_event_fields_ignored():
    history = (('id', 'name', 'valid_from', 'valid_to'),
               (1, 'foo', 1, None))
    events = (('id', 'name', 'source', 'valid_from'),
              (1, 'bar', 'web', 3))
    actual = applyhistory(history, events, key='id')
    expect = (('id', 'name', 'valid_from', 'valid_to'),
              (1, 'foo', 1, 3),
              (1, 'bar', 3, None))
    ieq(expect, actual)


def test_applyhistory_intervals_recomputed():
    # the input validity intervals are ignored and recomputed so that each
    # version is valid until the next one starts (left-closed right-open)
    history = (('id', 'name', 'valid_from', 'valid_to'),
               (1, 'foo', 1, 99),
               (1, 'bar', 5, 42))
    events = (('id', 'name', 'valid_from'),
              (1, 'baz', 7))
    actual = applyhistory(history, events, key='id')
    expect = (('id', 'name', 'valid_from', 'valid_to'),
              (1, 'foo', 1, 5),
              (1, 'bar', 5, 7),
              (1, 'baz', 7, None))
    ieq(expect, actual)


def test_applyhistory_empty_events():
    history = (('id', 'name', 'valid_from', 'valid_to'),
               (1, 'foo', 1, None))
    events = (('id', 'name', 'valid_from'),)
    actual = applyhistory(history, events, key='id')
    expect = (('id', 'name', 'valid_from', 'valid_to'),
              (1, 'foo', 1, None))
    ieq(expect, actual)


def test_applyhistory_empty_history():
    history = (('id', 'name', 'valid_from', 'valid_to'),)
    events = (('id', 'name', 'valid_from'),
              (1, 'foo', 1),
              (1, 'bar', 3))
    actual = applyhistory(history, events, key='id')
    expect = (('id', 'name', 'valid_from', 'valid_to'),
              (1, 'foo', 1, 3),
              (1, 'bar', 3, None))
    ieq(expect, actual)


def test_applyhistory_presorted():
    history = (('id', 'name', 'valid_from', 'valid_to'),
               (1, 'foo', 1, None))
    events = (('id', 'name', 'valid_from'),
              (1, 'bar', 3))
    actual = applyhistory(history, events, key='id', presorted=True)
    expect = (('id', 'name', 'valid_from', 'valid_to'),
              (1, 'foo', 1, 3),
              (1, 'bar', 3, None))
    ieq(expect, actual)


def test_applyhistory_single_pass_events():
    # the events table may be a one-shot iterator
    history = (('id', 'name', 'valid_from', 'valid_to'),
               (1, 'foo', 1, None))
    events = iter([('id', 'name', 'valid_from'),
                   (1, 'bar', 3)])
    actual = applyhistory(history, events, key='id')
    expect = (('id', 'name', 'valid_from', 'valid_to'),
              (1, 'foo', 1, 3),
              (1, 'bar', 3, None))
    ieq(expect, actual)


def test_applyhistory_table_method():
    # the transformation is also available as a table method
    import petl as etl
    history = (('id', 'name', 'valid_from', 'valid_to'),
               (1, 'foo', 1, None))
    events = (('id', 'name', 'valid_from'),
              (1, 'bar', 3))
    actual = etl.wrap(history).applyhistory(events, key='id')
    expect = (('id', 'name', 'valid_from', 'valid_to'),
              (1, 'foo', 1, 3),
              (1, 'bar', 3, None))
    ieq(expect, actual)


def test_applyhistory_missing_event_field():
    history = (('id', 'name', 'valid_from', 'valid_to'),
               (1, 'foo', 1, None))
    # the events table lacks the attribute field "name"
    events = (('id', 'valid_from'),
              (1, 3))
    actual = applyhistory(history, events, key='id')
    try:
        list(actual)
    except AssertionError as e:
        assert 'name' in str(e)
    else:
        raise AssertionError('expected an assertion error')


def test_applyhistory_multiple_attributes():
    history = (('id', 'name', 'city', 'valid_from', 'valid_to'),
               (1, 'foo', 'lisbon', 1, None))
    events = (('id', 'name', 'city', 'valid_from'),
              (1, 'foo', 'porto', 3),   # only the city changed
              (1, 'foo', 'porto', 5))   # duplicate, no new version
    actual = applyhistory(history, events, key='id')
    expect = (('id', 'name', 'city', 'valid_from', 'valid_to'),
              (1, 'foo', 'lisbon', 1, 3),
              (1, 'foo', 'porto', 3, None))
    ieq(expect, actual)


def test_mergeversions():
    # events are applied in order, later events at the same time correct
    # earlier ones
    actual = _mergeversions([(1, ('a',)), (5, ('b',))],
                            [(3, ('c',)), (3, ('d',))])
    eq_([(1, ('a',)), (3, ('d',)), (5, ('b',))], actual)


def test_mergeversions_empty():
    eq_([], _mergeversions([], []))
    eq_([(1, ('a',))], _mergeversions([(1, ('a',))], []))
    eq_([(1, ('a',))], _mergeversions([], [(1, ('a',))]))
