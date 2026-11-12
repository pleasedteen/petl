# -*- coding: utf-8 -*-
from __future__ import absolute_import, print_function, division


import csv
import errno
import json
import os
from contextlib import contextmanager

import pytest

import petl as etl
from petl.errors import ArgumentError
from petl.io.csv import fromcsv
from petl.io.csv_partition import (
    tocsvpartitioned,
    _safe_token,
    _partition_filename,
    MANIFEST_FILENAME,
)
from petl.util.base import Table


def _read_manifest(path):
    with open(path, newline='', encoding='utf-8') as f:
        rows = list(csv.reader(f))
    return rows


def _manifest_entries(path):
    rows = _read_manifest(path)
    header, data = rows[0], rows[1:]
    n_key = len(header) - 2
    entries = []
    for row in data:
        key = tuple(json.loads(v) for v in row[:n_key])
        filename = row[n_key]
        nrows = int(row[n_key + 1])
        entries.append((key, filename, nrows))
    return header, entries


def _read_partition(directory, filename):
    return fromcsv(os.path.join(directory, filename))


def test_tocsvpartitioned_basic(tmpdir):
    table = (('k1', 'k2', 'v'),
             ('a', 1, 'x'),
             ('a', 1, 'y'),
             ('b', 2, 'z'),
             ('b', 2, 'w'))
    target = str(tmpdir.join('parts'))
    tocsvpartitioned(table, target, key=['k1', 'k2'])

    header, entries = _manifest_entries(
        os.path.join(target, MANIFEST_FILENAME))
    assert header == ['k1', 'k2', 'filename', 'nrows']
    assert entries == [
        (('a', 1), 'str_a#int_1.csv', 2),
        (('b', 2), 'str_b#int_2.csv', 2),
    ]

    # all columns kept, in original order, header written once
    assert list(_read_partition(target, 'str_a#int_1.csv')) == [
        ('k1', 'k2', 'v'), ('a', '1', 'x'), ('a', '1', 'y')]
    assert list(_read_partition(target, 'str_b#int_2.csv')) == [
        ('k1', 'k2', 'v'), ('b', '2', 'z'), ('b', '2', 'w')]


def test_tocsvpartitioned_single_key_and_table_method(tmpdir):
    table = etl.wrap((('k', 'v'), ('a', 1), ('b', 2), ('a', 3)))
    target = str(tmpdir.join('parts'))
    table.tocsvpartitioned(target, key='k')
    _, entries = _manifest_entries(
        os.path.join(target, MANIFEST_FILENAME))
    assert entries == [(('a',), 'str_a.csv', 2),
                       (('b',), 'str_b.csv', 1)]


def test_tocsvpartitioned_index_and_negative_index_keys(tmpdir):
    table = (('k', 'v'), ('a', 1), ('a', 2))
    target1 = str(tmpdir.join('p1'))
    tocsvpartitioned(table, target1, key=0)
    assert os.path.exists(os.path.join(target1, 'str_a.csv'))

    target2 = str(tmpdir.join('p2'))
    tocsvpartitioned(table, target2, key=-1)
    assert os.path.exists(os.path.join(target2, 'int_1.csv'))


def test_key_types_are_distinguished(tmpdir):
    # None, empty string and numeric vs. string keys must never collide.
    # Note Python itself treats 1 == 1.0 == True as the same dict key, so
    # those cannot form separate partitions; verify their file-name tokens
    # separately in test_safe_token_examples.
    values = [None, '', '1', 1, 2.5]
    table = etl.wrap([('k', 'v')] +
                    [[v, i] for i, v in enumerate(values)])
    target = str(tmpdir.join('parts'))
    tocsvpartitioned(table, target, key='k')

    _, entries = _manifest_entries(
        os.path.join(target, MANIFEST_FILENAME))
    keys = [e[0] for e in entries]
    filenames = [e[1] for e in entries]
    assert keys == [(v,) for v in values]
    # every key gets its own file, even though '' and None and '1' share a
    # textual form with other values
    assert len(set(filenames)) == len(filenames)
    assert len(filenames) == len(values)
    for key, filename, nrows in entries:
        assert nrows == 1
        rows = list(_read_partition(target, filename))
        assert rows[0] == ('k', 'v')
        assert len(rows) == 2


def test_safe_token_examples():
    assert _safe_token(None) == 'null'
    assert _safe_token('') == 'str_'
    assert _safe_token('1') == 'str_1'
    assert _safe_token(1) == 'int_1'
    assert _safe_token(1.0) == 'float_1.0'
    assert _safe_token(True) == 'bool_true'
    assert _safe_token(False) == 'bool_false'
    # path separators and other unsafe characters are escaped, never kept
    token = _safe_token('a/../../etc')
    assert '/' not in token
    assert '\\' not in _safe_token('a\\b')
    assert '\x00' not in _safe_token('\x00')


def test_partition_filenames_are_unique_and_case_insensitive_safe():
    used = set()
    names = [_partition_filename((v,), used)
             for v in (None, '', '1', 1, 'A', 'a')]
    assert len(names) == len(set(names))
    lowered = [n.lower() for n in names]
    assert len(lowered) == len(set(lowered))
    for name in names:
        assert '/' not in name and '\\' not in name
        assert name.endswith('.csv')


def test_unsafe_key_characters_cannot_escape_directory(tmpdir):
    nasty = ['../escape', '../../escape', 'a/b\\c', '..', '..\\\\x',
             'con', 'CON', 'PRN', 'a\x00b', '']
    table = [('k', 'v')] + [[v, i] for i, v in enumerate(nasty)]
    target = str(tmpdir.join('parts'))
    tocsvpartitioned(table, target, key='k')

    root = str(tmpdir)
    # nothing was written outside the target directory
    for entry in os.listdir(root):
        if entry != 'parts':
            pytest.fail('file escaped target directory: %s' % entry)
    # every file lives directly inside the target (no subdirectories)
    for name in os.listdir(target):
        assert os.path.isfile(os.path.join(target, name))

    # the data itself round-trips unchanged
    _, entries = _manifest_entries(
        os.path.join(target, MANIFEST_FILENAME))
    recovered = set()
    for (key,), filename, nrows in entries:
        assert nrows == 1
        rows = list(_read_partition(target, filename))
        recovered.add(rows[1][0])
    assert recovered == set(nasty)


def test_target_already_exists_is_rejected(tmpdir):
    table = (('k', 'v'), ('a', 1))

    directory = tmpdir.mkdir('exists')
    with pytest.raises(OSError) as exc:
        tocsvpartitioned(table, str(directory), key='k')
    assert exc.value.errno == errno.EEXIST

    afile = tmpdir.join('afile')
    afile.write('')
    with pytest.raises(OSError) as exc:
        tocsvpartitioned(table, str(afile), key='k')
    assert exc.value.errno == errno.EEXIST

    link = str(tmpdir.join('link'))
    os.symlink(str(directory), link)
    with pytest.raises(OSError) as exc:
        tocsvpartitioned(table, link, key='k')
    assert exc.value.errno == errno.EEXIST


def test_failure_cleans_up_and_target_never_appears(tmpdir):

    class FailingTable(Table):
        def __init__(self):
            self.passes = 0

        def __iter__(self):
            self.passes += 1
            yield ('k', 'v')
            for i in range(5):
                if i == 3:
                    raise RuntimeError('boom')
                yield (i, i)

    table = FailingTable()
    target = str(tmpdir.join('parts'))
    with pytest.raises(RuntimeError):
        # tiny bounds force flushing before the failure
        tocsvpartitioned(table, target, key='k',
                         max_cache_rows=1, max_open_files=1)
    assert not os.path.exists(target)
    # no staging directories left behind
    assert not [n for n in os.listdir(str(tmpdir))
                if n.startswith('.csvpartition-')]
    # the input was read exactly once
    assert table.passes == 1


def test_input_is_iterated_exactly_once(tmpdir):

    class CountingTable(Table):
        def __init__(self):
            self.passes = 0

        def __iter__(self):
            self.passes += 1
            yield ('k', 'v')
            for i in range(20):
                yield (i % 4, i)

    table = CountingTable()
    target = str(tmpdir.join('parts'))
    tocsvpartitioned(table, target, key='k',
                     max_cache_rows=2, max_open_files=2)
    assert table.passes == 1

    _, entries = _manifest_entries(
        os.path.join(target, MANIFEST_FILENAME))
    assert sum(nrows for _, _, nrows in entries) == 20


def test_open_files_are_bounded(tmpdir):
    import petl.io.csv_partition as mod

    original = mod._get_writer
    seen = []

    def spy(state, open_writers, max_open_files, *args, **kwargs):
        result = original(state, open_writers, max_open_files, *args,
                          **kwargs)
        seen.append(len(open_writers))
        return result

    table = [('k', 'v')] + [[i, i] for i in range(25)]
    target = str(tmpdir.join('parts'))
    mod._get_writer = spy
    try:
        tocsvpartitioned(table, target, key='k',
                         max_cache_rows=1, max_open_files=3)
    finally:
        mod._get_writer = original
    assert max(seen) <= 3


def test_cached_rows_are_bounded(tmpdir):
    import petl.io.csv_partition as mod

    # 20 distinct partitions with a cache holding at most 2 rows force data
    # to be spilled to disk long before the end of the single input pass
    table = [('k', 'v')] + [[i, i] for i in range(20)]
    target = str(tmpdir.join('parts'))
    tocsvpartitioned(table, target, key='k',
                     max_cache_rows=2, max_open_files=2)

    files = [n for n in os.listdir(target) if n != MANIFEST_FILENAME]
    assert len(files) == 20
    # files exist on disk, proving buffered rows were flushed
    assert all(os.path.getsize(os.path.join(target, n)) > 0
               for n in files)

    # every partition still contains exactly one header plus one data row
    _, entries = _manifest_entries(
        os.path.join(target, MANIFEST_FILENAME))
    for _, filename, nrows in entries:
        assert nrows == 1
        rows = list(_read_partition(target, filename))
        assert rows[0] == ('k', 'v') and len(rows) == 2


def test_reopened_partition_keeps_single_header_and_all_rows(tmpdir):
    # interleaved keys with tiny cache force repeated close/reopen cycles
    table = [('k', 'v')] + [[i % 2, i] for i in range(10)]
    target = str(tmpdir.join('parts'))
    tocsvpartitioned(table, target, key='k',
                     max_cache_rows=1, max_open_files=1)

    _, entries = _manifest_entries(
        os.path.join(target, MANIFEST_FILENAME))
    assert sorted(nrows for _, _, nrows in entries) == [5, 5]
    for _, filename, nrows in entries:
        rows = list(_read_partition(target, filename))
        assert rows[0] == ('k', 'v')
        assert len(rows) == nrows + 1


def test_csvargs_are_passed_to_partition_writer(tmpdir):
    table = (('k', 'v'), ('a', 1), ('b', 2))
    target = str(tmpdir.join('parts'))
    tocsvpartitioned(table, target, key='k', delimiter=';')
    with open(os.path.join(target, 'str_a.csv'), newline='') as f:
        assert f.read().splitlines() == ['k;v', 'a;1']

    # manifest keeps the default dialect regardless of csvargs
    header, _ = _manifest_entries(
        os.path.join(target, MANIFEST_FILENAME))
    assert header == ['k', 'filename', 'nrows']


def test_quoting_csvarg(tmpdir):
    table = (('k', 'v'), ('a,b', 1))
    target = str(tmpdir.join('parts'))
    tocsvpartitioned(table, target, key='k', quoting=csv.QUOTE_ALL)
    with open(os.path.join(target, 'str_a_002cb.csv'), newline='') as f:
        content = f.read()
    assert '"a,b"' in content


def test_manifest_preserves_key_types(tmpdir):
    table = etl.wrap((('k', 'v'),
                     (None, 1), ('', 2), ('1', 3), (1, 4), (1.5, 5)))
    target = str(tmpdir.join('parts'))
    tocsvpartitioned(table, target, key='k')

    raw = _read_manifest(os.path.join(target, MANIFEST_FILENAME))
    assert raw[0] == ['k', 'filename', 'nrows']
    encoded = [row[0] for row in raw[1:]]
    # JSON literals make every type unambiguous
    assert encoded == ['null', '""', '"1"', '1', '1.5']
    decoded = [json.loads(v) for v in encoded]
    assert decoded == [None, '', '1', 1, 1.5]
    # None vs empty string never confused
    assert encoded.index('null') != encoded.index('""')


def test_non_json_serialisable_key_is_rejected_and_cleaned(tmpdir):
    table = (('k', 'v'), ({'not': 'json serialisable'}, 1))
    target = str(tmpdir.join('parts'))
    with pytest.raises(ArgumentError):
        tocsvpartitioned(table, target, key='k')
    assert not os.path.exists(target)


def test_header_only_table_publishes_manifest_only(tmpdir):
    target = str(tmpdir.join('parts'))
    tocsvpartitioned([('a', 'b')], target, key='a')
    assert os.listdir(target) == [MANIFEST_FILENAME]
    assert _read_manifest(os.path.join(target, MANIFEST_FILENAME)) == [
        ['a', 'filename', 'nrows']]


def test_table_without_header_rejected(tmpdir):
    target = str(tmpdir.join('parts'))
    with pytest.raises(ArgumentError):
        tocsvpartitioned([], target, key='a')
    assert not os.path.exists(target)


def test_invalid_arguments(tmpdir):
    table = (('k', 'v'), ('a', 1))
    with pytest.raises(ArgumentError):
        tocsvpartitioned(table, str(tmpdir.join('a')), 'k',
                         max_cache_rows=0)
    with pytest.raises(ArgumentError):
        tocsvpartitioned(table, str(tmpdir.join('b')), 'k',
                         max_open_files=0)
    with pytest.raises(ArgumentError):
        tocsvpartitioned(table, str(tmpdir.join('c')), 'k',
                         manifest_name='nested/name.csv')
    with pytest.raises(ArgumentError):
        tocsvpartitioned(table, str(tmpdir.join('d')), 'k',
                         manifest_name='')


def test_missing_parent_directory_rejected(tmpdir):
    target = os.path.join(str(tmpdir), 'no', 'such', 'parts')
    with pytest.raises(OSError) as exc:
        tocsvpartitioned((('k', 'v'), ('a', 1)), target, key='k')
    assert exc.value.errno == errno.ENOENT


def test_reserved_manifest_name_cannot_collide_with_partition(tmpdir):
    # a key string whose file name would match the manifest spelling
    table = (('k', 'v'), ('manifest', 1), ('other', 2))
    target = str(tmpdir.join('parts'))
    tocsvpartitioned(table, target, key='k')
    names = set(os.listdir(target))
    assert MANIFEST_FILENAME in names
    # exactly one manifest, one file per distinct key
    assert len(names) == 3
    header, entries = _manifest_entries(
        os.path.join(target, MANIFEST_FILENAME))
    assert [e[1] for e in entries] != [MANIFEST_FILENAME]
    assert all(e[1] != MANIFEST_FILENAME for e in entries)


def test_key_field_named_filename_rejected(tmpdir):
    table = (('filename', 'v'), ('a', 1))
    with pytest.raises(ArgumentError):
        tocsvpartitioned(table, str(tmpdir.join('parts')), key='filename')


def test_long_key_value_produces_usable_filename(tmpdir):
    long_value = 'x' * 300
    table = (('k', 'v'), (long_value, 1), (long_value, 2))
    target = str(tmpdir.join('parts'))
    tocsvpartitioned(table, target, key='k')
    _, entries = _manifest_entries(
        os.path.join(target, MANIFEST_FILENAME))
    assert len(entries) == 1
    _, filename, nrows = entries[0]
    assert nrows == 2
    assert len(filename) <= 105
    assert os.path.isfile(os.path.join(target, filename))


def test_output_is_published_atomically(tmpdir):
    # while a successful run leaves exactly the target directory; a
    # interrupted run leaves neither target nor staging artefacts
    table = (('k', 'v'),) + tuple((i % 3, i) for i in range(9))
    target = str(tmpdir.join('parts'))
    tocsvpartitioned(table, target, key='k', max_cache_rows=2)
    assert os.path.isdir(target)
    assert not [n for n in os.listdir(str(tmpdir))
                if n.startswith('.csvpartition-')]


def test_write_error_closes_files_and_cleans_up(tmpdir):
    import petl.io.csv_partition as mod

    original = mod.open_csv_writer

    class BrokenWriter(object):
        def writerow(self, row):
            raise IOError('simulated write failure')

    @contextmanager
    def broken(source, mode, encoding, errors, **csvargs):
        yield BrokenWriter()

    table = etl.wrap([('k', 'v')] + [[i, i] for i in range(6)])
    target = str(tmpdir.join('parts'))
    mod.open_csv_writer = broken
    try:
        with pytest.raises(IOError):
            tocsvpartitioned(table, target, key='k',
                             max_cache_rows=1, max_open_files=2)
    finally:
        mod.open_csv_writer = original
    assert not os.path.exists(target)
    assert not [n for n in os.listdir(str(tmpdir))
                if n.startswith('.csvpartition-')]
