# -*- coding: utf-8 -*-
"""Partitioned CSV directory export.

Write a table out as one CSV file per distinct key value, together with a
manifest file describing the partition layout, while reading the input table
exactly once and bounding both memory usage and the number of simultaneously
open files.

"""
from __future__ import absolute_import, print_function, division


# standard library dependencies
import csv
import errno
import hashlib
import io
import json
import os
import shutil
from collections import OrderedDict

# internal dependencies
from petl.compat import PY2, text_type, binary_type, string_types, long
from petl.errors import ArgumentError
from petl.util.base import Table, asindices
from petl.io.sources import FileSource
if PY2:
    from petl.io.csv_py2 import open_csv_writer, UnicodeWriter as _CsvUnicodeWriter
    from petl.io.base import getcodec
else:
    from petl.io.csv_py3 import open_csv_writer


# name of the manifest file written inside every published partition directory
MANIFEST_FILENAME = 'manifest.csv'

# characters allowed inside partition file names (path separators and other
# potentially dangerous characters are never present)
_SAFE_CHARS = ('abcdefghijklmnopqrstuvwxyz'
               'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
               '0123456789._-')

# keep generated names comfortably below common file system limits
_MAX_BASENAME_LENGTH = 100


def tocsvpartitioned(table, directory, key, encoding='utf-8', errors='strict',
                     manifest_name=MANIFEST_FILENAME, max_cache_rows=1000,
                     max_open_files=10, **csvargs):
    """
    Write the table to a directory of CSV files, one file per distinct value of
    the composite `key`, plus a manifest recording each key, its file name and
    the number of rows it contains.

    The input table is iterated over exactly once. Rows belonging to each
    partition are buffered in memory; when more than `max_cache_rows` rows are
    buffered, a buffer is flushed to disk. At most `max_open_files` partition
    files are kept open at any time (the least recently used file is closed
    and reopened in append mode later), so writing a table with many
    partitions never exhausts file descriptors.

    The output directory is built in a temporary staging directory and only
    renamed into place once every row has been written successfully. If
    anything goes wrong the staging directory is removed and nothing is left
    behind. An exception is raised if `directory` already exists.

    Key values are rendered in partition file names with explicit type
    markers, so that ``None``, the empty string and numeric keys are never
    confused with string keys, and path separators or other unsafe characters
    in key values cannot escape the output directory. The original key values
    are preserved verbatim, as JSON, in the manifest.

    Parameters
    ----------

    table : Table
        The table to export. All columns are written in their original order.
    directory : str
        Path of the directory to create. It must not already exist.
    key : str or int or sequence
        A field name, field index, or list of field names/indices forming the
        composite partition key.
    encoding : str
        Encoding used for the partition files and the manifest.
    errors : str
        Encoding error handling, passed through to the underlying files.
    manifest_name : str
        File name of the manifest inside the output directory.
    max_cache_rows : int
        Maximum number of data rows held in memory across all partition
        buffers before buffers start being flushed to disk.
    max_open_files : int
        Maximum number of partition files kept open simultaneously.
    **csvargs
        Extra keyword arguments are passed to :func:`csv.writer` for each
        partition file, e.g. ``delimiter='\\t'`` or ``quoting=csv.QUOTE_ALL``.

    The manifest is a CSV file whose header lists the key field names followed
    by ``filename`` and ``nrows``. Each key value is stored as a JSON literal
    so that its type (null, string, number, boolean) is unambiguous.

    """

    # validate arguments up front
    if not isinstance(directory, string_types):
        raise ArgumentError(
            'directory must be a file system path, found %r' % (directory,))
    if max_cache_rows < 1:
        raise ArgumentError(
            'max_cache_rows must be at least 1, found %r' % (max_cache_rows,))
    if max_open_files < 1:
        raise ArgumentError(
            'max_open_files must be at least 1, found %r' % (max_open_files,))
    if not isinstance(manifest_name, string_types) or not manifest_name:
        raise ArgumentError(
            'manifest_name must be a non-empty string, found %r'
            % (manifest_name,))
    if os.sep in manifest_name or (os.altsep and os.altsep in manifest_name):
        raise ArgumentError(
            'manifest_name must not contain path separators, found %r'
            % (manifest_name,))

    # refuse to touch an existing target (files, symlinks and empty
    # directories are all rejected)
    if os.path.lexists(directory):
        raise OSError(errno.EEXIST,
                      'target directory already exists: %s' % directory)

    csvargs.setdefault('dialect', 'excel')

    parent = os.path.dirname(os.path.abspath(directory))
    if not os.path.isdir(parent):
        raise OSError(errno.ENOENT,
                      'parent directory does not exist: %s' % parent)

    # build everything in a sibling staging directory, then atomically rename
    staging = _make_staging(parent)
    try:
        _write_partitions(table=table, staging=staging, key=key,
                          encoding=encoding, errors=errors,
                          manifest_name=manifest_name,
                          max_cache_rows=max_cache_rows,
                          max_open_files=max_open_files, csvargs=csvargs)
        os.rename(staging, directory)
    except BaseException:
        # clean up partial output, including any open files
        shutil.rmtree(staging, ignore_errors=True)
        raise


Table.tocsvpartitioned = tocsvpartitioned


def _make_staging(parent):
    # isolated, unpredictable name so concurrent exports never collide
    random_suffix = hashlib.sha1(os.urandom(16)).hexdigest()[:16]
    staging = os.path.join(parent, '.csvpartition-%s' % random_suffix)
    os.mkdir(staging)
    return staging


class _PartitionState(object):
    """Bookkeeping for a single partition file."""

    __slots__ = ('key', 'filename', 'path', 'buffer', 'nrows')

    def __init__(self, key, filename, path):
        self.key = key
        self.filename = filename
        self.path = path
        self.buffer = []
        self.nrows = 0


def _write_partitions(table, staging, key, encoding, errors, manifest_name,
                      max_cache_rows, max_open_files, csvargs):

    it = iter(table)
    try:
        hdr = tuple(next(it))
    except StopIteration:
        raise ArgumentError('table has no header row')

    indices = asindices(hdr, key)
    key_names = [text_type(hdr[i]) for i in indices]
    if 'filename' in key_names or 'nrows' in key_names:
        raise ArgumentError(
            "key field names must not include 'filename' or 'nrows', "
            'found %r' % (key_names,))

    # partitions keyed by the tuple of raw key values; insertion order is the
    # order in which partitions are first seen, which gives the manifest a
    # stable, deterministic layout
    states = OrderedDict()
    # reserved and already-used file names, guaranteeing one-to-one mapping
    # between key values and files even on case-insensitive file systems
    used_names = set([manifest_name.lower()])

    # ordered dict used as an LRU registry of open writers:
    # path -> (csv writer, close function)
    open_writers = OrderedDict()
    cached_rows = [0]  # total rows currently held in memory

    def get_state(row_key):
        try:
            state = states.get(row_key)
        except TypeError:
            # unhashable key values (e.g. dicts or lists) cannot partition
            raise ArgumentError(
                'partition key values must be hashable, found %r'
                % (row_key,))
        if state is None:
            # verify the values can be recorded in the manifest before any
            # data are written
            for value in row_key:
                _encode_manifest_value(value)
            filename = _partition_filename(row_key, used_names)
            state = _PartitionState(row_key, filename,
                                    os.path.join(staging, filename))
            states[row_key] = state
        return state

    def flush(state):
        """Write buffered rows of one partition to its file."""
        if not state.buffer:
            return
        rows = state.buffer
        state.buffer = []
        cached_rows[0] -= len(rows)
        writer = _get_writer(state, open_writers, max_open_files,
                             encoding, errors, csvargs, hdr)
        try:
            for row in rows:
                writer.writerow(row)
        except Exception:
            # close and drop the possibly broken writer; staging cleanup
            # below removes any partial file
            _, close = open_writers.pop(state.path, (None, None))
            if close is not None:
                close()
            raise

    def evict_buffer():
        """Flush one non-empty buffer to bring memory back under the bound."""
        # states stay in first-seen order (that order is used for the
        # manifest); the open-file LRU is maintained separately inside
        # _get_writer, so any buffer choice here keeps the file bound intact
        for candidate in states.values():
            if candidate.buffer:
                flush(candidate)
                return

    for row in it:
        row_key = tuple(row[i] for i in indices)
        state = get_state(row_key)
        state.buffer.append(tuple(row))
        state.nrows += 1
        cached_rows[0] += 1
        if cached_rows[0] > max_cache_rows:
            evict_buffer()

    # flush everything still buffered
    for state in list(states.values()):
        flush(state)

    # close all open partition files before writing the manifest
    close_error = None
    while open_writers:
        _, (_, close) = open_writers.popitem()
        try:
            close()
        except Exception as e:  # pragma: no cover - defensive
            close_error = e
    if close_error is not None:
        raise close_error

    _write_manifest(staging=staging, manifest_name=manifest_name,
                    key_names=key_names, states=states,
                    encoding=encoding, errors=errors)


def _get_writer(state, open_writers, max_open_files, encoding, errors,
                csvargs, hdr):
    """Return an open csv writer for the partition, evicting LRU files.

    `open_writers` maps partition paths to ``(writer, close)`` tuples and
    doubles as the LRU registry: the most recently used entry lives at the
    end and is evicted from the front.
    """
    existing = open_writers.get(state.path)
    if existing is not None:
        open_writers.move_to_end(state.path)
        return existing[0]

    while len(open_writers) >= max_open_files:
        # close the least recently used partition file; it will be reopened
        # in append mode if more rows for it arrive later
        _, old_entry = open_writers.popitem(last=False)
        old_entry[1]()

    first_time = not os.path.exists(state.path)
    mode = 'wb' if first_time else 'ab'
    source = FileSource(state.path)
    context = open_csv_writer(source, mode=mode, encoding=encoding,
                              errors=errors, **csvargs)
    writer = context.__enter__()

    def close():
        context.__exit__(None, None, None)

    entry = (writer, close)
    open_writers[state.path] = entry
    if first_time:
        # every partition file keeps all columns, in original order
        writer.writerow(hdr)
    return writer


def _write_manifest(staging, manifest_name, key_names, states, encoding,
                    errors):
    path = os.path.join(staging, manifest_name)
    with open(path, 'wb') as buf:
        if PY2:
            codec = getcodec(encoding)
            if codec.name == 'ascii':
                writer = csv.writer(buf)
            else:
                writer = _ManifestUnicodeWriter(buf, encoding=encoding,
                                                errors=errors)
            writer.writerow(tuple(key_names) + ('filename', 'nrows'))
            for state in states.values():
                encoded_key = [_encode_manifest_value(value)
                               for value in state.key]
                writer.writerow(encoded_key + [state.filename,
                                               text_type(state.nrows)])
        else:
            text_buf = io.TextIOWrapper(buf, encoding=encoding,
                                        errors=errors, newline='')
            try:
                writer = csv.writer(text_buf)
                writer.writerow(tuple(key_names) + ('filename', 'nrows'))
                for state in states.values():
                    encoded_key = [_encode_manifest_value(value)
                                   for value in state.key]
                    writer.writerow(encoded_key + [state.filename,
                                                   text_type(state.nrows)])
                text_buf.flush()
            finally:
                text_buf.detach()


if PY2:
    class _ManifestUnicodeWriter(_CsvUnicodeWriter):
        # the base class converts None values back to None, whereas the
        # manifest always contains fully rendered strings
        def writerow(self, row):
            self.writer.writerow(
                [unicode(s).encode('utf-8') for s in row])


def _partition_filename(row_key, used_names):
    """Build a unique, file-system-safe name for a raw key tuple."""
    base = '#'.join(_safe_token(value) for value in row_key)
    if not base:
        # defensive fallback; an empty composite key cannot normally occur
        base = 'partition'
    if len(base) > _MAX_BASENAME_LENGTH:
        digest = hashlib.sha1(base.encode('utf-8')).hexdigest()
        base = base[:_MAX_BASENAME_LENGTH - 41] + '_' + digest
    candidate = base + '.csv'
    unique = candidate
    counter = 1
    while unique.lower() in used_names:
        suffix = '_%d' % counter
        trimmed = base[:_MAX_BASENAME_LENGTH - len(suffix) - 4]
        unique = trimmed + suffix + '.csv'
        counter += 1
    used_names.add(unique.lower())
    return unique


def _encode_manifest_value(value):
    """Encode a key value for the manifest, preserving its JSON type."""
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        raise ArgumentError(
            'partition key values must be JSON serialisable so their type '
            'can be recorded in the manifest; value %r of type %s is not'
            % (value, type(value).__name__))


def _safe_token(value):
    """Render one key value as an unambiguous, path-safe file name token."""
    if value is None:
        return 'null'
    if value is True:
        return 'bool_true'
    if value is False:
        return 'bool_false'
    if isinstance(value, binary_type):
        # on PY2 this also catches plain byte strings; latin-1 is lossless
        # for every byte
        rendered = value.decode('latin-1')
        prefix = 'bytes_'
    elif isinstance(value, bool):
        # kept for safety; True/False are handled above
        rendered = text_type(value)
        prefix = 'bool_'
    elif isinstance(value, long):
        # covers int and, on PY2, long (bool is already handled)
        rendered = text_type(value)
        prefix = 'int_'
    elif isinstance(value, float):
        rendered = text_type(value)
        prefix = 'float_'
    else:
        # plain strings and any other type rendered via text_type; note
        # the empty string becomes 'str_' which can never equal another
        # token (numbers carry a digit, null is spelled differently)
        rendered = text_type(value)
        prefix = 'str_'

    chars = []
    for char in rendered:
        if char in _SAFE_CHARS:
            chars.append(char)
        else:
            chars.append('_%04x' % ord(char))
    return prefix + ''.join(chars)
