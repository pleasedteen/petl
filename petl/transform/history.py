# -*- coding: utf-8 -*-
from __future__ import absolute_import, print_function, division


import operator


from petl.compat import text_type
from petl.comparison import Comparable
from petl.util.base import Table, asindices, rowgetter
from petl.transform.sorts import sort


def applyhistory(table, events, key, time=None, fromfield='valid_from',
                 tofield='valid_to', presorted=False, buffersize=None,
                 tempdir=None, cache=True):
    """
    Apply a table of change events to a dimension history table and return
    the revised history, e.g.::

        >>> import petl as etl
        >>> history = [['id', 'name', 'valid_from', 'valid_to'],
        ...            [1, 'foo', 1, 4],
        ...            [1, 'bar', 4, None],
        ...            [2, 'baz', 2, None]]
        >>> events = [['id', 'name', 'valid_from'],
        ...           [1, 'qux', 3],
        ...           [1, 'quux', 4],
        ...           [2, 'baz', 5]]
        >>> table1 = etl.applyhistory(history, events, key='id')
        >>> table1.lookall()
        +----+--------+------------+----------+
        | id | name   | valid_from | valid_to |
        +====+========+============+==========+
        |  1 | 'foo'  |          1 |        3 |
        +----+--------+------------+----------+
        |  1 | 'qux'  |          3 |        4 |
        +----+--------+------------+----------+
        |  1 | 'quux' |          4 | None     |
        +----+--------+------------+----------+
        |  2 | 'baz'  |          2 | None     |
        +----+--------+------------+----------+

    The first event is backfilled out of order between the existing versions
    at times 1 and 4. The second event falls on an existing version and
    corrects it in place. The third event restates the attributes already in
    effect for key 2, so no new version is added.

    The `table` argument is the existing history, with one row per version of
    a business key. The `events` argument is a table of change events, with
    one row per observed attribute set. Both tables must contain the key
    field(s) given by `key`, the attribute fields, and a field holding the
    time each version or event takes effect. The history table must also
    contain the `tofield` field; its values are recomputed, so the field
    content on input is ignored.

    The validity interval of each version is left-closed and right-open,
    i.e., a version is in effect from `fromfield` (inclusive) until
    `tofield` (exclusive). Intervals are recomputed after merging so that
    each version of a key is valid until the next version of the same key
    starts, and the last version of each key is open-ended (`tofield` is
    `None`).

    Events are applied per business key as follows:

    * An event whose time matches the start of an existing version corrects
      that version in place ("same-time correction").
    * An event whose time falls between existing versions is backfilled as a
      new version, regardless of whether it arrives out of chronological
      order.
    * An event that restates the attributes already recorded as in effect at
      its time is a duplicate and does not create a new version.
    * After merging, adjacent versions of a key with identical attributes
      are collapsed into the earliest one.

    Events for keys not present in the history table start a new history.
    Keys of the history table without events are passed through unchanged.

    The `time` argument names the field of the `events` table holding the
    effective time of each event; it defaults to the value of `fromfield`.
    The `fromfield` and `tofield` arguments name the validity interval
    fields of the history table, and default to ``'valid_from'`` and
    ``'valid_to'``.

    Compound keys are supported by passing a list or tuple of field names as
    `key`. Attribute fields are taken from the history table; the events
    table must provide fields of the same name and may carry additional
    fields, which are ignored.

    If `presorted` is True, it is assumed that the data of both tables are
    already sorted by the given key, and the `buffersize`, `tempdir` and
    `cache` arguments are ignored. Otherwise, the data are sorted, see also
    the discussion of the `buffersize`, `tempdir` and `cache` arguments
    under the :func:`petl.transform.sorts.sort` function.

    See also :func:`petl.io.db.historydb` for maintaining a dimension
    history directly in a database table.

    """

    return ApplyHistoryView(table, events, key=key, time=time,
                            fromfield=fromfield, tofield=tofield,
                            presorted=presorted, buffersize=buffersize,
                            tempdir=tempdir, cache=cache)


Table.applyhistory = applyhistory


class ApplyHistoryView(Table):

    def __init__(self, table, events, key=None, time=None,
                 fromfield='valid_from', tofield='valid_to',
                 presorted=False, buffersize=None, tempdir=None, cache=True):
        self.key = key
        self.time = time
        self.fromfield = fromfield
        self.tofield = tofield
        if presorted:
            self.table = table
            self.events = events
        else:
            self.table = sort(table, key, buffersize=buffersize,
                              tempdir=tempdir, cache=cache)
            self.events = sort(events, key, buffersize=buffersize,
                               tempdir=tempdir, cache=cache)

    def __iter__(self):
        return iterapplyhistory(self.table, self.events, self.key, self.time,
                                self.fromfield, self.tofield)


def _getter(indices):
    # build a function extracting the given indices from a row as a tuple
    return lambda row: tuple(row[i] for i in indices)


def _itergroups(it, getkey):
    # group an iterator of rows sorted by key, yielding (key, rows) pairs
    group = list()
    current = None
    started = False
    for row in it:
        k = getkey(row)
        if started and k != current:
            yield current, group
            group = list()
        started = True
        current = k
        group.append(row)
    if started:
        yield current, group


def _lt(a, b):
    return Comparable(a) < Comparable(b)


def _mergeversions(existing, events):
    """
    Merge change events into existing versions of a single business key.

    The `existing` argument is an iterable of (valid_from, attrs) pairs
    describing the recorded versions, and `events` is an iterable of
    (time, attrs) pairs of change events, where attrs is a tuple of
    attribute values. Returns a list of (valid_from, attrs) pairs sorted
    by valid_from, where adjacent versions with identical attributes have
    been collapsed.

    """
    versions = dict()
    for valid_from, attrs in existing:
        versions[valid_from] = tuple(attrs)
    for time, attrs in events:
        attrs = tuple(attrs)
        if time in versions:
            # event falls on an existing version, correct it in place; if
            # the attributes are equal this is a duplicate event and the
            # assignment is a no-op
            versions[time] = attrs
        else:
            # find the attributes recorded as in effect at the event time,
            # i.e., the version with the latest valid_from not after time
            current = None
            for valid_from, vattrs in versions.items():
                if not _lt(time, valid_from):
                    if current is None or _lt(current[0], valid_from):
                        current = (valid_from, vattrs)
            if current is None or current[1] != attrs:
                # backfill a new version at the event time
                versions[time] = attrs
            # else the event restates known attributes, do not add a version
    merged = list()
    for valid_from, attrs in sorted(versions.items(),
                                    key=lambda item: Comparable(item[0])):
        if merged and merged[-1][1] == attrs:
            # same attributes as the previous version, collapse into it
            continue
        merged.append((valid_from, attrs))
    return merged


def _buildrows(template, keyvals, versions):
    # build output rows following the template of the history table,
    # recomputing the left-closed right-open validity intervals
    n = len(versions)
    for i, (valid_from, attrs) in enumerate(versions):
        valid_to = versions[i + 1][0] if i + 1 < n else None
        row = list()
        for kind, pos in template:
            if kind == 'key':
                row.append(keyvals[pos])
            elif kind == 'from':
                row.append(valid_from)
            elif kind == 'to':
                row.append(valid_to)
            else:
                row.append(attrs[pos])
        yield tuple(row)


def iterapplyhistory(table, events, key, time, fromfield, tofield):
    # assumes the rows of both tables are sorted by the given key

    hit = iter(table)
    try:
        hdr = next(hit)
    except StopIteration:
        return
    hdr = tuple(hdr)
    yield hdr

    flds = list(map(text_type, hdr))
    if time is None:
        time = fromfield

    # resolve fields of the history table
    kindices = asindices(hdr, key)
    findex = asindices(hdr, fromfield)[0]
    tindex = asindices(hdr, tofield)[0]
    kset = set(kindices)
    aindices = tuple(i for i in range(len(hdr))
                     if i not in kset and i != findex and i != tindex)

    # build a template mapping output columns to key, attribute and
    # validity interval values
    template = list()
    for i in range(len(hdr)):
        if i in kset:
            template.append(('key', kindices.index(i)))
        elif i == findex:
            template.append(('from', None))
        elif i == tindex:
            template.append(('to', None))
        else:
            template.append(('attr', aindices.index(i)))

    gethkey = rowgetter(*kindices)
    gethfrom = operator.itemgetter(findex)
    gethattrs = _getter(aindices)

    eit = iter(events)
    try:
        ehdr = next(eit)
    except StopIteration:
        # no events, pass the remaining history through unchanged
        for row in hit:
            yield tuple(row)
        return

    # resolve fields of the events table
    eflds = list(map(text_type, ehdr))
    ekindices = asindices(ehdr, key)
    etindex = asindices(ehdr, time)[0]
    eaindices = list()
    for i in aindices:
        assert flds[i] in eflds, \
            'attribute field %r not found in the events table' % flds[i]
        eaindices.append(eflds.index(flds[i]))
    getekey = rowgetter(*ekindices)
    getetime = operator.itemgetter(etindex)
    geteattrs = _getter(tuple(eaindices))

    # merge the two sorted streams of (key, rows) groups
    hg = _itergroups(hit, gethkey)
    eg = _itergroups(eit, getekey)
    h = next(hg, None)
    e = next(eg, None)
    while h is not None or e is not None:
        if e is None or (h is not None and _lt(h[0], e[0])):
            # key only present in the history, pass through unchanged
            for row in h[1]:
                yield tuple(row)
            h = next(hg, None)
        elif h is None or _lt(e[0], h[0]):
            # key only present in the events, start a new history
            versions = _mergeversions(
                (), ((getetime(row), geteattrs(row)) for row in e[1]))
            for row in _buildrows(template, e[0], versions):
                yield row
            e = next(eg, None)
        else:
            # key present on both sides, apply the events
            versions = _mergeversions(
                ((gethfrom(row), gethattrs(row)) for row in h[1]),
                ((getetime(row), geteattrs(row)) for row in e[1]))
            for row in _buildrows(template, h[0], versions):
                yield row
            h = next(hg, None)
            e = next(eg, None)
