from __future__ import absolute_import, division, print_function

import os
import shutil
import tempfile
from datetime import datetime

import pytest

import petl as etl
from petl import asofjoin
from petl.errors import ArgumentError, FieldSelectionError
from petl.test.helpers import ieq, ieq2


def _samples():
    return (('device', 'ts', 'reading'),
            ('a', '2024-01-01 09:30', 1.0),
            ('a', '2024-01-01 10:30', 2.0),
            ('a', '2024-01-01 12:30', 3.0),
            ('b', '2024-01-01 11:00', 4.0),
            ('c', '2024-01-01 10:00', 5.0))


def _configs():
    return (('device', 'ts', 'firmware', 'until'),
            ('a', '2024-01-01 09:00', 'v1', '2024-01-01 11:00'),
            ('a', '2024-01-01 11:00', 'v2', None),
            ('b', '2024-01-01 10:30', 'v3', None))


def _expect_basic():
    return (('device', 'ts', 'reading', 'cfg_ts', 'cfg_firmware',
             'cfg_until'),
            ('a', '2024-01-01 09:30', 1.0, '2024-01-01 09:00', 'v1',
             '2024-01-01 11:00'),
            ('a', '2024-01-01 10:30', 2.0, '2024-01-01 09:00', 'v1',
             '2024-01-01 11:00'),
            ('a', '2024-01-01 12:30', 3.0, '2024-01-01 11:00', 'v2', None),
            ('b', '2024-01-01 11:00', 4.0, '2024-01-01 10:30', 'v3', None),
            ('c', '2024-01-01 10:00', 5.0, None, None, None))


def _asofjoin(samples, configs, **kwargs):
    kwargs.setdefault('key', 'device')
    kwargs.setdefault('ltime', 'ts')
    kwargs.setdefault('rprefix', 'cfg_')
    return asofjoin(samples, configs, **kwargs)


def test_basic():
    result = _asofjoin(_samples(), _configs(), rend='until')
    ieq2(_expect_basic(), result)


def test_basic_presorted():
    # input already sorted by (key, time) can be joined in a single
    # streaming pass
    result = _asofjoin(_samples(), _configs(), rend='until',
                       presorted=True)
    ieq2(_expect_basic(), result)


def test_unordered_input():
    # the same data, shuffled: results must be identical and in the
    # original order of the samples
    samples = (('device', 'ts', 'reading'),
               ('c', '2024-01-01 10:00', 5.0),
               ('a', '2024-01-01 12:30', 3.0),
               ('b', '2024-01-01 11:00', 4.0),
               ('a', '2024-01-01 09:30', 1.0),
               ('a', '2024-01-01 10:30', 2.0))
    configs = (('device', 'ts', 'firmware', 'until'),
               ('b', '2024-01-01 10:30', 'v3', None),
               ('a', '2024-01-01 11:00', 'v2', None),
               ('a', '2024-01-01 09:00', 'v1', '2024-01-01 11:00'))
    expect = (('device', 'ts', 'reading', 'cfg_ts', 'cfg_firmware',
               'cfg_until'),
              ('c', '2024-01-01 10:00', 5.0, None, None, None),
              ('a', '2024-01-01 12:30', 3.0, '2024-01-01 11:00', 'v2',
               None),
              ('b', '2024-01-01 11:00', 4.0, '2024-01-01 10:30', 'v3',
               None),
              ('a', '2024-01-01 09:30', 1.0, '2024-01-01 09:00', 'v1',
               '2024-01-01 11:00'),
              ('a', '2024-01-01 10:30', 2.0, '2024-01-01 09:00', 'v1',
               '2024-01-01 11:00'))
    result = _asofjoin(samples, configs, rend='until')
    ieq2(expect, result)


def test_tie_last_in_input_wins():
    # several config rows with the same key and time: the last row of
    # the original input wins, however the input is ordered
    samples = (('device', 'ts'), ('a', '2024-01-01 10:00'))
    configs = (('device', 'ts', 'firmware'),
               ('a', '2024-01-01 09:00', 'v1'),
               ('a', '2024-01-01 09:00', 'v2'),
               ('a', '2024-01-01 08:00', 'v0'))
    expect = (('device', 'ts', 'cfg_ts', 'cfg_firmware'),
              ('a', '2024-01-01 10:00', '2024-01-01 09:00', 'v2'))
    ieq2(expect, _asofjoin(samples, configs))
    # shuffled config input: v1 is now the last of the tied rows in the
    # original input
    shuffled = (configs[0],
                configs[3],
                configs[2],
                configs[1])
    expect_shuffled = (('device', 'ts', 'cfg_ts', 'cfg_firmware'),
                       ('a', '2024-01-01 10:00', '2024-01-01 09:00', 'v1'))
    ieq2(expect_shuffled, _asofjoin(samples, shuffled))
    # also in streaming mode
    sorted_configs = (configs[0], configs[3], configs[1], configs[2])
    ieq2(expect, _asofjoin(samples, sorted_configs, presorted=True))


def test_composite_key():
    samples = (('site', 'device', 'ts'),
               ('s1', 'a', '2024-01-01 10:00'),
               ('s2', 'a', '2024-01-01 10:00'))
    configs = (('site', 'device', 'ts', 'fw'),
               ('s1', 'a', '2024-01-01 09:00', 'v1'),
               ('s2', 'a', '2024-01-01 09:00', 'v2'))
    result = asofjoin(samples, configs, key=['site', 'device'],
                      ltime='ts', rprefix='cfg_')
    expect = (('site', 'device', 'ts', 'cfg_ts', 'cfg_fw'),
              ('s1', 'a', '2024-01-01 10:00', '2024-01-01 09:00', 'v1'),
              ('s2', 'a', '2024-01-01 10:00', '2024-01-01 09:00', 'v2'))
    ieq2(expect, result)


def test_lkey_rkey():
    samples = (('dev', 'ts'), ('a', '2024-01-01 10:00'))
    configs = (('device', 'ts', 'fw'), ('a', '2024-01-01 09:00', 'v1'))
    result = asofjoin(samples, configs, lkey='dev', rkey='device',
                      ltime='ts', rprefix='cfg_')
    expect = (('dev', 'ts', 'cfg_ts', 'cfg_fw'),
              ('a', '2024-01-01 10:00', '2024-01-01 09:00', 'v1'))
    ieq2(expect, result)


def test_rtime_different_name():
    samples = (('device', 'ts'), ('a', '2024-01-01 10:00'))
    configs = (('device', 'effective_from', 'fw'),
               ('a', '2024-01-01 09:00', 'v1'))
    result = asofjoin(samples, configs, key='device', ltime='ts',
                      rtime='effective_from', rprefix='cfg_')
    expect = (('device', 'ts', 'cfg_effective_from', 'cfg_fw'),
              ('a', '2024-01-01 10:00', '2024-01-01 09:00', 'v1'))
    ieq2(expect, result)


def test_validity_period():
    samples = (('device', 'ts'),
               ('a', '2024-01-01 09:00'),   # exactly at rtime: match
               ('a', '2024-01-01 10:59'),   # within validity: match
               ('a', '2024-01-01 11:00'),   # exactly at rend: no match
               ('a', '2024-01-01 12:00'))   # after rend: no match
    configs = (('device', 'ts', 'fw', 'until'),
               ('a', '2024-01-01 09:00', 'v1', '2024-01-01 11:00'))
    result = _asofjoin(samples, configs, rend='until')
    expect = (('device', 'ts', 'cfg_ts', 'cfg_fw', 'cfg_until'),
              ('a', '2024-01-01 09:00', '2024-01-01 09:00', 'v1',
               '2024-01-01 11:00'),
              ('a', '2024-01-01 10:59', '2024-01-01 09:00', 'v1',
               '2024-01-01 11:00'),
              ('a', '2024-01-01 11:00', None, None, None),
              ('a', '2024-01-01 12:00', None, None, None))
    ieq2(expect, result)


def test_validity_period_expired_latest_wins():
    # only the latest configuration is considered: if it has expired,
    # the result is no match, even if an earlier configuration would
    # still be valid
    samples = (('device', 'ts'), ('a', '2024-01-01 12:00'))
    configs = (('device', 'ts', 'fw', 'until'),
               ('a', '2024-01-01 09:00', 'v1', None),
               ('a', '2024-01-01 10:00', 'v2', '2024-01-01 11:00'))
    result = _asofjoin(samples, configs, rend='until')
    expect = (('device', 'ts', 'cfg_ts', 'cfg_fw', 'cfg_until'),
              ('a', '2024-01-01 12:00', None, None, None))
    ieq2(expect, result)


def test_order_preserved():
    # output rows appear in the original order of the left table
    samples = (('id', 'device', 'ts'),
               (1, 'a', '2024-01-01 12:30'),
               (2, 'b', '2024-01-01 11:00'),
               (3, 'a', '2024-01-01 09:30'),
               (4, 'c', '2024-01-01 10:00'),
               (5, 'a', '2024-01-01 10:30'))
    result = _asofjoin(samples, _configs(), rend='until')
    actual_ids = [row[0] for row in etl.data(result)]
    assert actual_ids == [1, 2, 3, 4, 5]


def test_presorted_streaming_readonce():
    # streaming mode works with iterators that can only be read once,
    # and does not create any temporary directory
    def gen(table):
        for row in table:
            yield row
    result = _asofjoin(gen(_samples()), gen(_configs()), rend='until',
                       presorted=True)
    assert result._managed_tempdir is None
    ieq(_expect_basic(), result)


def test_readonce_unsorted():
    # read-once iterators are also supported on the sorting path, with
    # memory bounded by buffersize (chunks spilled to temporary files)
    def gen(table):
        for row in table:
            yield row
    result = _asofjoin(gen(_samples()), gen(_configs()), rend='until',
                       buffersize=2)
    assert result._managed_tempdir is not None
    try:
        ieq2(_expect_basic(), result)
    finally:
        result.close()


def test_managed_tempdir_cleanup():
    result = _asofjoin(_samples(), _configs(), rend='until', buffersize=2)
    tmpdir = result._managed_tempdir
    assert tmpdir is not None and os.path.isdir(tmpdir)
    ieq(_expect_basic(), result)
    # with a small buffersize, sort chunks are spilled to the managed
    # temporary directory
    assert os.listdir(tmpdir) != []
    result.close()
    assert not os.path.exists(tmpdir)
    # closing twice is fine
    result.close()


def test_tempdir_context_manager():
    with _asofjoin(_samples(), _configs(), rend='until',
                   buffersize=2) as result:
        tmpdir = result._managed_tempdir
        assert os.path.isdir(tmpdir)
        ieq(_expect_basic(), result)
    assert not os.path.exists(tmpdir)


def test_user_tempdir_not_removed():
    tmpdir = tempfile.mkdtemp()
    try:
        result = _asofjoin(_samples(), _configs(), rend='until',
                           buffersize=2, tempdir=tmpdir)
        assert result._managed_tempdir is None
        ieq(_expect_basic(), result)
        result.close()
        # a caller-provided tempdir is never removed
        assert os.path.isdir(tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_presorted_no_tempdir():
    tmpdir = tempfile.mkdtemp()
    try:
        result = _asofjoin(_samples(), _configs(), rend='until',
                           presorted=True, tempdir=tmpdir)
        ieq(_expect_basic(), result)
        # nothing was written in streaming mode
        assert os.listdir(tmpdir) == []
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_fluent_chaining():
    result = (etl.wrap(_samples())
              .asofjoin(_configs(), key='device', ltime='ts', rend='until',
                        rprefix='cfg_')
              .selecteq('cfg_firmware', 'v1')
              .cut('device', 'ts', 'reading', 'cfg_firmware'))
    expect = (('device', 'ts', 'reading', 'cfg_firmware'),
              ('a', '2024-01-01 09:30', 1.0, 'v1'),
              ('a', '2024-01-01 10:30', 2.0, 'v1'))
    ieq(expect, result)
    assert etl.Table.asofjoin is asofjoin


def test_empty():
    # empty right table: all rows unmatched
    result = _asofjoin(_samples(), (('device', 'ts', 'firmware', 'until'),),
                       rend='until')
    expect = (('device', 'ts', 'reading', 'cfg_ts', 'cfg_firmware',
               'cfg_until'),
              ('a', '2024-01-01 09:30', 1.0, None, None, None),
              ('a', '2024-01-01 10:30', 2.0, None, None, None),
              ('a', '2024-01-01 12:30', 3.0, None, None, None),
              ('b', '2024-01-01 11:00', 4.0, None, None, None),
              ('c', '2024-01-01 10:00', 5.0, None, None, None))
    ieq(expect, result)
    # empty left table: header only
    result = _asofjoin((('device', 'ts', 'reading'),), _configs(),
                       rend='until')
    ieq((('device', 'ts', 'reading', 'cfg_ts', 'cfg_firmware',
          'cfg_until'),), result)


def test_none_times():
    samples = (('device', 'ts'),
               ('a', None),                 # no sample time: no match
               ('a', '2024-01-01 10:00'))
    configs = (('device', 'ts', 'fw'),
               ('a', None, 'v0'),           # no effective time: ignored
               ('a', '2024-01-01 09:00', 'v1'))
    result = _asofjoin(samples, configs)
    expect = (('device', 'ts', 'cfg_ts', 'cfg_fw'),
              ('a', None, None, None),
              ('a', '2024-01-01 10:00', '2024-01-01 09:00', 'v1'))
    ieq2(expect, result)


def test_missing_value():
    samples = (('device', 'ts'), ('c', '2024-01-01 10:00'))
    configs = (('device', 'ts', 'fw'), ('a', '2024-01-01 09:00', 'v1'))
    result = _asofjoin(samples, configs, missing='N/A')
    expect = (('device', 'ts', 'cfg_ts', 'cfg_fw'),
              ('c', '2024-01-01 10:00', 'N/A', 'N/A'))
    ieq(expect, result)


def test_no_rprefix():
    # without rprefix, right fields are appended as-is, possibly
    # duplicating field names
    samples = (('device', 'ts'), ('a', '2024-01-01 10:00'))
    configs = (('device', 'ts', 'fw'), ('a', '2024-01-01 09:00', 'v1'))
    result = asofjoin(samples, configs, key='device', ltime='ts')
    expect = (('device', 'ts', 'ts', 'fw'),
              ('a', '2024-01-01 10:00', '2024-01-01 09:00', 'v1'))
    ieq(expect, result)


def test_datetime_objects():
    samples = (('device', 'ts'),
               ('a', datetime(2024, 1, 1, 10, 30)))
    configs = (('device', 'ts', 'fw'),
               ('a', datetime(2024, 1, 1, 9, 0), 'v1'),
               ('a', datetime(2024, 1, 1, 11, 0), 'v2'))
    result = _asofjoin(samples, configs)
    expect = (('device', 'ts', 'cfg_ts', 'cfg_fw'),
              ('a', datetime(2024, 1, 1, 10, 30),
               datetime(2024, 1, 1, 9, 0), 'v1'))
    ieq(expect, result)


def test_natural_key():
    # with no key arguments, common fields other than the time fields
    # are used
    samples = (('device', 'ts', 'reading'), ('a', '2024-01-01 10:00', 1.0))
    configs = (('device', 'ts', 'fw'), ('a', '2024-01-01 09:00', 'v1'))
    result = asofjoin(samples, configs, ltime='ts', rprefix='cfg_')
    expect = (('device', 'ts', 'reading', 'cfg_ts', 'cfg_fw'),
              ('a', '2024-01-01 10:00', 1.0, '2024-01-01 09:00', 'v1'))
    ieq(expect, result)


def test_errors():
    samples = _samples()
    configs = _configs()
    # ltime is required
    with pytest.raises(ArgumentError):
        asofjoin(samples, configs, key='device')
    # bad combination of key arguments
    with pytest.raises(ArgumentError):
        asofjoin(samples, configs, key='device', lkey='device',
                 rkey='device', ltime='ts')
    with pytest.raises(ArgumentError):
        asofjoin(samples, configs, key='device', lkey='device', ltime='ts')
    # unknown fields
    with pytest.raises(FieldSelectionError):
        asofjoin(samples, configs, key='nope', ltime='ts')
    with pytest.raises(FieldSelectionError):
        asofjoin(samples, configs, key='device', ltime='nope')
    with pytest.raises(FieldSelectionError):
        asofjoin(samples, configs, key='device', ltime='ts', rend='nope')
    # empty tables have no header
    with pytest.raises(ArgumentError):
        asofjoin((), configs, key='device', ltime='ts')
    with pytest.raises(ArgumentError):
        asofjoin(samples, (), key='device', ltime='ts')


def test_small_buffersize():
    # force the external sort to spill every row to disk
    result = _asofjoin(_samples(), _configs(), rend='until', buffersize=1)
    try:
        ieq2(_expect_basic(), result)
    finally:
        result.close()


def _brute_force(samples, configs, rend):
    """Reference implementation of the documented semantics."""
    expect = [samples[0] + tuple(f for i, f in enumerate(configs[0])
                                 if i != 0)]
    for s in samples[1:]:
        stime = s[1]
        best = None
        best_pos = None
        if stime is not None:
            for i, c in enumerate(configs[1:]):
                ctime = c[1]
                if ctime is None or c[0] != s[0] or ctime > stime:
                    continue
                if best is None or (ctime, i) > (best[1], best_pos):
                    best = c
                    best_pos = i
        if best is not None and rend is not None \
                and best[2] is not None and not stime < best[2]:
            best = None
        expect.append(s + (best[1:] if best is not None
                           else (None,) * (len(configs[0]) - 1)))
    return tuple(expect)


def test_against_brute_force():
    import random
    rnd = random.Random(20240101)
    devices = ['a', 'b', 'c']
    configs = [('device', 'ts', 'until')]
    for _ in range(40):
        ts = rnd.choice([None] + list(range(15)))
        until = None if ts is None or rnd.random() < 0.5 \
            else ts + rnd.randint(0, 4)
        configs.append((rnd.choice(devices), ts, until))
    samples = [('device', 'ts')]
    for _ in range(60):
        samples.append((rnd.choice(devices),
                        rnd.choice([None] + list(range(20)))))
    expect = _brute_force(samples, configs, rend='until')
    # default path, with sorting
    result = asofjoin(samples, configs, key='device', ltime='ts',
                      rend='until', buffersize=7)
    try:
        ieq(expect, result)
    finally:
        result.close()
    # streaming path, with both tables pre-sorted by (key, time)
    result = asofjoin(etl.sort(samples, key=['device', 'ts']),
                      etl.sort(configs, key=['device', 'ts']),
                      key='device', ltime='ts', rend='until',
                      presorted=True)
    ieq(_brute_force(tuple(etl.sort(samples, key=['device', 'ts'])),
                     tuple(etl.sort(configs, key=['device', 'ts'])),
                     rend='until'),
        result)
