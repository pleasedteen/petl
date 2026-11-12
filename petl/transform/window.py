from __future__ import absolute_import, print_function, division


import logging
from collections import deque
from itertools import groupby

from petl.compat import next, text_type, integer_types
from petl.transform.sorts import sort as _sort
from petl.util.base import Table, asindices


logger = logging.getLogger(__name__)
warning = logger.warning
info = logger.info
debug = logger.debug


def groupwindow(table, key, value, sort=None, preceding=0, following=0,
                rank='rank', lag='lag', windowsum='window_sum',
                buffersize=None, tempdir=None):
    """
    Compute window functions over groups of rows and append the results as
    new fields. E.g.::

        >>> import petl as etl
        >>> table1 = [['region', 'store', 'day', 'sales'],
        ...           ['north', 'a', 2, 20],
        ...           ['north', 'a', 1, 10],
        ...           ['north', 'a', 3, None],
        ...           ['north', 'b', 1, 5],
        ...           ['north', 'b', 1, 7],
        ...           ['south', 'c', 1, 100],
        ...           ['south', 'c', 2, 50]]
        >>> table2 = etl.groupwindow(table1, key=['region', 'store'],
        ...                          value='sales', sort='day',
        ...                          preceding=1, following=1)
        >>> table2.lookall()
        +---------+-------+-----+-------+------+------+------------+
        | region  | store | day | sales | rank | lag  | window_sum |
        +=========+=======+=====+=======+======+======+============+
        | 'north' | 'a'   |   2 |    20 |    2 |   10 |         30 |
        +---------+-------+-----+-------+------+------+------------+
        | 'north' | 'a'   |   1 |    10 |    1 | None |         30 |
        +---------+-------+-----+-------+------+------+------------+
        | 'north' | 'a'   |   3 | None  |    3 |   20 |         20 |
        +---------+-------+-----+-------+------+------+------------+
        | 'north' | 'b'   |   1 |     5 |    1 | None |         12 |
        +---------+-------+-----+-------+------+------+------------+
        | 'north' | 'b'   |   1 |     7 |    1 |    5 |         12 |
        +---------+-------+-----+-------+------+------+------------+
        | 'south' | 'c'   |   1 |   100 |    1 | None |        150 |
        +---------+-------+-----+-------+------+------+------------+
        | 'south' | 'c'   |   2 |    50 |    2 |  100 |        150 |
        +---------+-------+-----+-------+------+------+------------+

    Rows are grouped by the `key` field(s) (a single field or a list of
    fields for a compound key). If `key` is `None`, the whole table is
    treated as a single group. Within each group, rows are ordered by the
    `sort` field, ascending. The sort is stable, i.e., rows with equal
    `sort` values keep their original relative order. If `sort` is `None`,
    rows keep their original order within each group.

    Up to three new fields are appended to each row:

    - `rank` - the rank of the row within its group, starting from 1.
      Rows with equal `sort` values are tied: they share the same rank and
      the following rank(s) are skipped, so ranks go 1, 2, 2, 4, ...
    - `lag` - the `value` of the previous row within the same group, or
      `None` for the first row of each group.
    - `window_sum` - the sum of `value` over the window of `preceding`
      rows before and `following` rows after the current row, including
      the current row itself. The window is clipped at the group
      boundaries. `None` values are ignored; if every value in the window
      is `None`, the result is `None`.

    The `rank`, `lag` and `windowsum` arguments set the names of the new
    fields. Pass `None` for any of them to omit that field from the
    output.

    The rows in the output table are restored to the original input order.
    The input table is read exactly once per iteration, and rows are
    processed one group at a time, so groups larger than memory are
    supported: at most `preceding + following + 1` rows per group are held
    in memory, in addition to the buffers used for sorting. The
    `buffersize` argument limits the number of rows sorted in memory; if
    there are more rows, sorting is done in chunks written to temporary
    files (see :func:`petl.transform.sorts.sort` for details, and the
    `tempdir` argument to control where temporary files are created).

    This function can also be used in a fluent or "chained" style, e.g.::

        >>> table2 = (etl.wrap(table1)
        ...           .groupwindow(['region', 'store'], 'sales', sort='day',
        ...                        preceding=1, following=1))

    """

    return GroupWindowView(table, key, value, sort=sort, preceding=preceding,
                           following=following, rank=rank, lag=lag,
                           windowsum=windowsum, buffersize=buffersize,
                           tempdir=tempdir)


Table.groupwindow = groupwindow


class GroupWindowView(Table):

    def __init__(self, table, key, value, sort=None, preceding=0,
                 following=0, rank='rank', lag='lag', windowsum='window_sum',
                 buffersize=None, tempdir=None):
        self.table = table
        self.key = key
        self.value = value
        self.sort = sort
        self.preceding = _checkoffset('preceding', preceding)
        self.following = _checkoffset('following', following)
        self.rank = rank
        self.lag = lag
        self.windowsum = windowsum
        self.buffersize = buffersize
        self.tempdir = tempdir
        # check for duplicate output field names
        names = [text_type(f) for f in (rank, lag, windowsum)
                 if f is not None]
        if len(set(names)) != len(names):
            raise ValueError('duplicate output field names: %r'
                             % (names,))

    def __iter__(self):
        return itergroupwindow(self.table, self.key, self.value, self.sort,
                               self.preceding, self.following, self.rank,
                               self.lag, self.windowsum, self.buffersize,
                               self.tempdir)


def _checkoffset(name, offset):
    if not isinstance(offset, integer_types) or offset < 0:
        raise ValueError('%s must be a non-negative integer, found %r'
                         % (name, offset))
    return offset


def itergroupwindow(table, key, value, sort, preceding, following,
                    rank, lag, windowsum, buffersize, tempdir):
    it = iter(table)
    try:
        hdr = tuple(next(it))
    except StopIteration:
        return
    flds = list(map(text_type, hdr))

    # resolve field selections to field indices
    keyindices = asindices(flds, key) if key is not None else []
    valueindex = asindices(flds, value)[0]
    sortindices = asindices(flds, sort) if sort is not None else []

    # determine the output fields
    want = (rank is not None, lag is not None, windowsum is not None)
    newfields = [f for f, w in zip((rank, lag, windowsum), want) if w]
    conflicts = [f for f in newfields if text_type(f) in flds]
    if conflicts:
        raise ValueError('output field name conflicts with an existing '
                         'field: %r' % (conflicts,))
    yield tuple(hdr) + tuple(newfields)

    nfields = len(hdr)

    # choose a scratch field name to tag rows with their original position
    indexfield = '_index'
    taken = set(flds) | set(map(text_type, newfields))
    while text_type(indexfield) in taken:
        indexfield = '_' + indexfield

    # tag rows with their original position, then sort by group key, sort
    # field and original position (sorting by the original position last
    # makes the ordering of rows with equal sort values deterministic,
    # i.e., the sort is stable)
    tagged = _itertagged(hdr, it, indexfield)
    sortkey = tuple(keyindices) + tuple(sortindices) + (nfields,)
    sortedrows = _sort(tagged, key=sortkey, buffersize=buffersize,
                       tempdir=tempdir, cache=False)
    # compute the window fields, one group at a time
    computed = _iterwindow(sortedrows, keyindices, sortindices, valueindex,
                           preceding, following, newfields, want)
    # restore the original row order, then strip the scratch field
    restored = _sort(computed, key=nfields, buffersize=buffersize,
                     tempdir=tempdir, cache=False)
    rit = iter(restored)
    try:
        next(rit)  # discard the header, already yielded above
    except StopIteration:
        return
    for row in rit:
        yield row[:nfields] + row[nfields + 1:]


def _itertagged(hdr, rows, indexfield):
    yield tuple(hdr) + (indexfield,)
    for i, row in enumerate(rows):
        yield tuple(row) + (i,)


def _iterwindow(table, keyindices, sortindices, valueindex, preceding,
                following, newfields, want):
    it = iter(table)
    try:
        hdr = next(it)
    except StopIteration:
        return
    yield tuple(hdr) + tuple(newfields)

    def getgroupkey(row):
        return tuple(row[i] for i in keyindices)

    # N.B., rows are sorted by the group key, so rows belonging to the same
    # group are consecutive and can be processed one group at a time
    for _, grouprows in groupby(it, getgroupkey):
        for outrow in _itergroup(grouprows, sortindices, valueindex,
                                 preceding, following, want):
            yield outrow


def _itergroup(rows, sortindices, valueindex, preceding, following, want):
    # buffer of rows pending output, together with up to `preceding` rows
    # before them that are still needed to compute their window sums; each
    # item is a tuple (row, rank, lag, value)
    buffer = deque()
    # state = [position of next row to emit, window sum, window count]
    # where the window sum and count cover the non-None values of all rows
    # currently in the buffer
    state = [0, None, 0]
    pos = 0  # 1-based position of the current row within the group
    currentrank = 0
    lastsortkey = None
    prevvalue = None
    for row in rows:
        pos += 1
        # rank, leaving gaps after ties
        if sortindices:
            sortkey = tuple(row[i] for i in sortindices)
            if pos == 1 or sortkey != lastsortkey:
                currentrank = pos
            lastsortkey = sortkey
        else:
            currentrank = pos
        # lag, i.e., value of the previous row within the group
        lagvalue = prevvalue if pos > 1 else None
        value = row[valueindex]
        prevvalue = value
        buffer.append((row, currentrank, lagvalue, value))
        if value is not None:
            state[1] = value if state[2] == 0 else state[1] + value
            state[2] += 1
        # emit rows whose window is now complete
        for outrow in _emitupto(pos - following, buffer, state,
                                preceding, want):
            yield outrow
    # emit the remaining rows of the group
    for outrow in _emitupto(pos, buffer, state, preceding, want):
        yield outrow


def _emitupto(stop, buffer, state, preceding, want):
    # emit all buffered rows up to (but not including) position `stop`;
    # the window of the row being emitted always spans the whole buffer
    while state[0] < stop:
        pos = state[0]
        row, rankvalue, lagvalue, _ = buffer[min(preceding, pos)]
        sumvalue = state[1] if state[2] > 0 else None
        yield _formatrow(row, rankvalue, lagvalue, sumvalue, want)
        state[0] = pos + 1
        if state[0] > preceding:
            # the leftmost row is no longer needed for any window
            oldvalue = buffer.popleft()[3]
            if oldvalue is not None:
                state[2] -= 1
                state[1] = state[1] - oldvalue if state[2] > 0 else None


def _formatrow(row, rankvalue, lagvalue, sumvalue, want):
    outrow = list(row)
    if want[0]:
        outrow.append(rankvalue)
    if want[1]:
        outrow.append(lagvalue)
    if want[2]:
        outrow.append(sumvalue)
    return tuple(outrow)
