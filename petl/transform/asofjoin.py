from __future__ import absolute_import, print_function, division

import itertools
import logging
import shutil
import tempfile

from petl.compat import next, text_type
from petl.comparison import comparable_itemgetter
from petl.errors import ArgumentError
from petl.transform.basics import addrownumbers, cutout
from petl.transform.joins import _nonkey_indices
from petl.transform.selects import selectnotnone
from petl.transform.sorts import sort
from petl.util.base import Table, asindices, rowgetter


logger = logging.getLogger(__name__)
debug = logger.debug


def asofjoin(left, right, key=None, lkey=None, rkey=None, ltime=None,
             rtime=None, rend=None, presorted=False, buffersize=None,
             tempdir=None, cache=True, rprefix=None, missing=None):
    """
    Perform an "as-of" join of a table of samples (or events) against a
    table of versioned configurations. For each row of the `left` table,
    the matching row of the `right` table is the row with the same key
    and the latest `rtime` that is not later than the left row's `ltime`
    (i.e., a backward as-of join, similar to ``pandas.merge_asof``).
    E.g.::

        >>> import petl as etl
        >>> samples = [['device', 'ts', 'reading'],
        ...            ['a', '2024-01-01 09:30', 1.0],
        ...            ['a', '2024-01-01 10:30', 2.0],
        ...            ['a', '2024-01-01 12:30', 3.0],
        ...            ['b', '2024-01-01 11:00', 4.0],
        ...            ['c', '2024-01-01 10:00', 5.0]]
        >>> configs = [['device', 'ts', 'firmware', 'until'],
        ...            ['a', '2024-01-01 09:00', 'v1', '2024-01-01 11:00'],
        ...            ['a', '2024-01-01 11:00', 'v2', None],
        ...            ['b', '2024-01-01 10:30', 'v3', None]]
        >>> table = etl.asofjoin(samples, configs, key='device', ltime='ts',
        ...                      rend='until', rprefix='cfg_')
        >>> table
        +--------+--------------------+---------+--------------------+--------------+--------------------+
        | device | ts                 | reading | cfg_ts             | cfg_firmware | cfg_until          |
        +========+====================+=========+====================+==============+====================+
        | 'a'    | '2024-01-01 09:30' |     1.0 | '2024-01-01 09:00' | 'v1'         | '2024-01-01 11:00' |
        +--------+--------------------+---------+--------------------+--------------+--------------------+
        | 'a'    | '2024-01-01 10:30' |     2.0 | '2024-01-01 09:00' | 'v1'         | '2024-01-01 11:00' |
        +--------+--------------------+---------+--------------------+--------------+--------------------+
        | 'a'    | '2024-01-01 12:30' |     3.0 | '2024-01-01 11:00' | 'v2'         | None               |
        +--------+--------------------+---------+--------------------+--------------+--------------------+
        | 'b'    | '2024-01-01 11:00' |     4.0 | '2024-01-01 10:30' | 'v3'         | None               |
        +--------+--------------------+---------+--------------------+--------------+--------------------+
        | 'c'    | '2024-01-01 10:00' |     5.0 | None               | None         | None               |
        +--------+--------------------+---------+--------------------+--------------+--------------------+

    If several rows of the right table share the same key and `rtime`,
    the row that comes last in the original input of the right table is
    used, e.g.::

        >>> configs = [['device', 'ts', 'firmware'],
        ...            ['a', '2024-01-01 09:00', 'v1'],
        ...            ['a', '2024-01-01 09:00', 'v2']]
        >>> samples = [['device', 'ts'],
        ...            ['a', '2024-01-01 10:00']]
        >>> etl.asofjoin(samples, configs, key='device', ltime='ts')
        +--------+--------------------+--------------------+----------+
        | device | ts                 | ts                 | firmware |
        +========+====================+====================+==========+
        | 'a'    | '2024-01-01 10:00' | '2024-01-01 09:00' | 'v2'     |
        +--------+--------------------+--------------------+----------+

    Every row of the left table appears exactly once in the output, in
    the original order of the left table, with the non-key fields of the
    matching right row appended (or `missing` values if there is no
    match). The key fields of the right table are not repeated in the
    output; use the `rprefix` argument to disambiguate the remaining
    right fields if their names clash with left fields.

    The `key` argument may be a single field or a list of fields for a
    composite key. If the key fields are named differently in the two
    tables, use `lkey` and `rkey` instead of `key`. If no key is given,
    a natural join is tried over the fields the two tables have in
    common, excluding the time fields.

    The `ltime` argument is required and gives the field of the left
    table holding the time of each sample. The `rtime` argument gives
    the field of the right table holding the time from which each
    configuration is effective; it defaults to `ltime`. Rows of the
    right table where `rtime` is `None` are ignored, and rows of the
    left table where `ltime` is `None` never match. Time values must be
    mutually comparable (e.g., all `datetime` objects, or all ISO 8601
    strings of the same format).

    The optional `rend` argument gives the field of the right table
    holding the end of the validity period of each configuration. A
    configuration matches a sample only while the sample time lies in
    the half-open interval `[rtime, rend)`, i.e., the match is dropped
    if the sample time is greater than or equal to `rend`. A `rend`
    value of `None` means the configuration never expires. Note that
    only the latest configuration not later than the sample time is
    considered: if that configuration has expired, the result is no
    match, even if an earlier configuration would still be valid.

    If `presorted` is `True`, both tables are assumed to be already
    sorted by `(key, time)` in ascending order and the join is performed
    in a single streaming pass with constant memory, which also works
    for data sources that can only be read once. Otherwise both tables
    are sorted first, see the discussion of the `buffersize`, `tempdir`
    and `cache` arguments under :func:`petl.transform.sorts.sort`; the
    memory used is bounded by `buffersize`, and larger tables are
    processed via temporary files, so unordered data and data sources
    that can only be read once are handled within limited memory. In
    this case the original order of the left table is restored by an
    additional sort of the results.

    If `tempdir` is not given and sorting is required, a temporary
    directory is created for the sort chunk files, and it is removed
    when the returned table is closed - either explicitly via its
    ``close()`` method, implicitly by using the table as a context
    manager, or when the table is garbage collected. If `tempdir` is
    given, the directory itself is never removed by this function, but
    closing the table still clears the sort caches, deleting any chunk
    files written to it, e.g.::

        >>> with etl.asofjoin(samples, configs, key='device',
        ...                   ltime='ts') as table:
        ...     # temporary files are removed on leaving the block
        ...     print(table.lookall())  # doctest: +SKIP

    .. versionadded:: 1.7.33

    """

    if ltime is None:
        raise ArgumentError(
            'the ltime argument is required: provide the field of the '
            'left table holding the time of each sample')
    if rtime is None:
        rtime = ltime
    return AsofJoinView(left, right, key=key, lkey=lkey, rkey=rkey,
                        ltime=ltime, rtime=rtime, rend=rend,
                        presorted=presorted, buffersize=buffersize,
                        tempdir=tempdir, cache=cache, rprefix=rprefix,
                        missing=missing)


Table.asofjoin = asofjoin


class AsofJoinView(Table):

    def __init__(self, left, right, key=None, lkey=None, rkey=None,
                 ltime=None, rtime=None, rend=None, presorted=False,
                 buffersize=None, tempdir=None, cache=True, rprefix=None,
                 missing=None):
        self._managed_tempdir = None
        self._sortviews = []

        # peek at the headers without consuming read-once sources, so
        # fields can be validated and the pipeline built up front
        lhdr, left = _peek_header(left, 'left')
        rhdr, right = _peek_header(right, 'right')

        # resolve the time fields and the join keys, eagerly validating
        # them against the headers
        ltind = asindices(lhdr, ltime)[0]
        rtind = asindices(rhdr, rtime)[0]
        lkey, rkey = _resolve_keys(lhdr, rhdr, key, lkey, rkey, ltind)
        lkind = asindices(lhdr, lkey)
        rkind = asindices(rhdr, rkey)
        reind = asindices(rhdr, rend)[0] if rend is not None else None
        rvalind = _nonkey_indices(rhdr, rkind)

        # configuration rows without an effective time can never match
        right = selectnotnone(right, rtime)

        if presorted:
            # assume both tables are already sorted by (key, time) and
            # merge in a single streaming pass with constant memory
            self._result = _AsofMergeView(
                left, right, lkind, ltind, rkind, rtind, reind, rvalind,
                missing, rprefix)
        else:
            if tempdir is None:
                self._managed_tempdir = tempfile.mkdtemp(
                    prefix='petl-asofjoin-')
                tempdir = self._managed_tempdir
            # add row numbers, so the original order of the left table
            # can be restored after the join, and so that ties on
            # (key, time) in the right table are deterministically
            # resolved to the last row of the original input
            left = addrownumbers(left, field=_unique_field(
                lhdr, 'asof_rownum'))
            right = addrownumbers(right, field=_unique_field(
                rhdr, 'asof_rownum'))
            # the row number field is prepended, so all original field
            # indices shift by one
            shift = 1
            lsortkey = [i + shift for i in lkind] + [ltind + shift, 0]
            rsortkey = [i + shift for i in rkind] + [rtind + shift, 0]
            left = sort(left, key=lsortkey, buffersize=buffersize,
                        tempdir=tempdir, cache=cache)
            right = sort(right, key=rsortkey, buffersize=buffersize,
                         tempdir=tempdir, cache=cache)
            self._sortviews.extend([left, right])
            merged = _AsofMergeView(
                left, right,
                [i + shift for i in lkind], ltind + shift,
                [i + shift for i in rkind], rtind + shift,
                reind + shift if reind is not None else None,
                [i + shift for i in rvalind],
                missing, rprefix)
            # restore the original order of the left table, then drop
            # the row number field (at position 0)
            restored = sort(merged, key=0, buffersize=buffersize,
                            tempdir=tempdir, cache=cache)
            self._sortviews.append(restored)
            self._result = cutout(restored, 0)

    def __iter__(self):
        return iter(self._result)

    def close(self):
        """Release temporary resources held by this table.

        Clears the caches of the underlying sorts, which deletes any
        temporary chunk files, and removes the managed temporary
        directory, if one was created. A `tempdir` provided by the
        caller is never removed. The table should not be iterated after
        it has been closed if its sources cannot be read again.
        """
        for view in getattr(self, '_sortviews', ()):
            try:
                view.clearcache()
            except Exception:
                pass
        tmpdir = getattr(self, '_managed_tempdir', None)
        if tmpdir is not None:
            self._managed_tempdir = None
            # the managed directory is removed below, so any subsequent
            # re-sort must fall back to the default temporary directory
            for view in self._sortviews:
                view.tempdir = None
            debug('removing temporary directory %s', tmpdir)
            shutil.rmtree(tmpdir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class _AsofMergeView(Table):
    """Streaming merge of two tables already sorted by (key, time)."""

    def __init__(self, left, right, lkind, ltind, rkind, rtind, reind,
                 rvalind, missing, rprefix):
        self.left = left
        self.right = right
        self.lkind = lkind
        self.ltind = ltind
        self.rkind = rkind
        self.rtind = rtind
        self.reind = reind
        self.rvalind = rvalind
        self.missing = missing
        self.rprefix = rprefix

    def __iter__(self):
        return _iterasofmerge(
            self.left, self.right, self.lkind, self.ltind, self.rkind,
            self.rtind, self.reind, self.rvalind, self.missing,
            self.rprefix)


def _iterasofmerge(left, right, lkind, ltind, rkind, rtind, reind, rvalind,
                   missing, rprefix):
    lit = iter(left)
    rit = iter(right)
    lhdr = tuple(next(lit))
    rhdr = tuple(next(rit))

    # functions to extract comparable keys and times from rows
    lgetk = comparable_itemgetter(*lkind)
    rgetk = comparable_itemgetter(*rkind)
    lgett = comparable_itemgetter(ltind)
    rgett = comparable_itemgetter(rtind)
    rgete = comparable_itemgetter(reind) if reind is not None else None
    rgetv = rowgetter(*rvalind)

    # determine the output fields
    if rprefix is None:
        outhdr = list(lhdr) + [rhdr[i] for i in rvalind]
    else:
        outhdr = list(lhdr) + [text_type(rprefix) + text_type(rhdr[i])
                               for i in rvalind]
    yield tuple(outhdr)

    nmissing = len(rvalind)
    candidate = None
    candidate_key = None
    rrow = next(rit, None)
    rk = rt = None
    if rrow is not None:
        rk, rt = rgetk(rrow), rgett(rrow)

    for lrow in lit:
        lk = lgetk(lrow)
        lt = lgett(lrow)
        if lt.inner is None:
            # samples without a time never match
            yield tuple(lrow) + (missing,) * nmissing
            continue
        # the left table is sorted by key, so a candidate from a
        # different key group can never match again
        if candidate_key is not None and not candidate_key == lk:
            candidate = None
            candidate_key = None
        # discard right rows sorted before the current left key
        while rrow is not None and rk < lk:
            rrow = next(rit, None)
            rk, rt = (rgetk(rrow), rgett(rrow)) if rrow is not None \
                else (None, None)
        # advance the candidate to the latest right row with the same
        # key and a time not later than the sample time; because the
        # right table is sorted by (key, time, original row number),
        # the last row advanced to is the latest configuration, with
        # ties on (key, time) resolved to the last row of the input
        while rrow is not None and rk == lk and rt <= lt:
            candidate = rrow
            candidate_key = rk
            rrow = next(rit, None)
            rk, rt = (rgetk(rrow), rgett(rrow)) if rrow is not None \
                else (None, None)
        # emit the joined row if the candidate is still valid at the
        # sample time
        if candidate is not None and _isvalid(candidate, rgete, lt):
            yield tuple(lrow) + rgetv(candidate)
        else:
            yield tuple(lrow) + (missing,) * nmissing


def _isvalid(candidate, rgete, lt):
    # a candidate without a validity end field never expires
    if rgete is None:
        return True
    end = rgete(candidate)
    # a None validity end means the configuration never expires;
    # otherwise the sample time must lie before the end (the validity
    # interval is half-open)
    return end.inner is None or lt < end


def _peek_header(table, name):
    """Return a (header, table) pair, where the header has been read
    from the table without losing it, even if the table is an iterator
    that can only be read once."""
    it = iter(table)
    try:
        hdr = tuple(next(it))
    except StopIteration:
        raise ArgumentError('%s table is empty, no header row' % name)
    if it is table:
        # read-once iterator, re-attach the header row
        return hdr, itertools.chain([hdr], it)
    return hdr, table


def _resolve_keys(lhdr, rhdr, key, lkey, rkey, ltind):
    if key is lkey is rkey is None:
        # no keys specified, attempt a natural join over the fields the
        # two tables have in common, excluding the left time field
        rflds = set(map(text_type, rhdr))
        common = [f for i, f in enumerate(map(text_type, lhdr))
                  if f in rflds and i != ltind]
        if not common:
            raise ArgumentError('no fields in common')
        lkey = rkey = common[0] if len(common) == 1 else common
    elif key is not None and lkey is rkey is None:
        # common key specified
        lkey = rkey = key
    elif key is None and lkey is not None and rkey is not None:
        # left and right keys specified
        pass
    else:
        raise ArgumentError(
            'bad key arguments: either specify key, or specify both lkey '
            'and rkey, or provide no key/lkey/rkey arguments at all '
            '(natural join)')
    return lkey, rkey


def _unique_field(hdr, base):
    flds = set(map(text_type, hdr))
    field = base
    while field in flds:
        field = '_' + field
    return field
