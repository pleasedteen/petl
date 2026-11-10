from __future__ import absolute_import, print_function, division


from collections import namedtuple

from petl.compat import next, text_type
from petl.comparison import Comparable
from petl.errors import ArgumentError
from petl.transform.sorts import sort
from petl.util.base import Table, asindices


# Change type constants. These are the values found in the ``change``
# column of the change detail table returned by a three-way merge.
ADDED = 'added'
DELETED = 'deleted'
MODIFIED = 'modified'
CONFLICT = 'conflict'
DUPLICATE = 'duplicate'

# Side tag constants. ``BASE`` identifies the common ancestor table;
# ``LEFT`` and ``RIGHT`` identify the two independently edited tables.
BASE = 'base'
LEFT = 'left'
RIGHT = 'right'
# ``BOTH`` is used in the change detail ``side`` column when the two
# edited tables make an identical change.
BOTH = 'both'

# Column names of the fixed (non-key) part of the change detail table.
# The first columns of the detail table are the key columns, named after
# the actual key fields.
CHANGE_CHANGE = 'change'
CHANGE_SIDE = 'side'
CHANGE_FIELD = 'field'
CHANGE_BASE_VALUE = 'base_value'
CHANGE_LEFT_VALUE = 'left_value'
CHANGE_RIGHT_VALUE = 'right_value'
CHANGE_HEADER = (CHANGE_CHANGE, CHANGE_SIDE, CHANGE_FIELD,
                 CHANGE_BASE_VALUE, CHANGE_LEFT_VALUE,
                 CHANGE_RIGHT_VALUE)


class MergeConflict(tuple):
    """Marker placed in an otherwise auto-merged row where the left and
    right tables made incompatible changes that could not be merged
    automatically.

    A :class:`MergeConflict` is an immutable three-tuple of
    ``(base_value, left_value, right_value)`` and renders readably in a
    ``look()`` table, e.g.::

        MergeConflict(base=1, left=2, right=3)

    The marker keeps the three competing values available for downstream
    handling instead of silently discarding either side. See
    :func:`petl.transform.threemerge.threemerge` and
    :func:`petl.transform.threemerge.resolve`.

    """

    __slots__ = ()

    def __new__(cls, base_value, left_value, right_value):
        return super(MergeConflict, cls).__new__(
            cls, (base_value, left_value, right_value))

    @property
    def base_value(self):
        return self[0]

    @property
    def left_value(self):
        return self[1]

    @property
    def right_value(self):
        return self[2]

    def __repr__(self):
        return ('MergeConflict(base=%r, left=%r, right=%r)'
                % (self[0], self[1], self[2]))


# A group collects every row sharing one key from each of the three
# sorted input streams. A missing side is represented by ``None``; extra
# rows beyond the first for a side are kept in the ``*_duplicates``
# lists. ``key`` is the tuple of key values and ``key_fields`` the names
# of the key columns.
_MergeGroup = namedtuple('_MergeGroup',
                         ['key', 'key_fields', 'base', 'left', 'right',
                          'base_duplicates', 'left_duplicates',
                          'right_duplicates'])


def threemerge(base, left, right, key, presorted=False, buffersize=None,
               tempdir=None, cache=True):
    """
    Perform a three-way merge of a common ancestor table (`base`) and two
    independently edited tables (`left` and `right`, e.g. the purchasing
    and operations copies of the same product master data).

    Returns a :class:`ThreeWayMerge` object giving access to two lazy
    tables (see :class:`ThreeWayMerge` for the full interface):

    * :attr:`ThreeWayMerge.merged` - the merged result. Non-conflicting
      field edits are combined automatically. Rows added on one side are
      included, rows deleted on one side are dropped, and fields where
      both sides made incompatible edits are marked with a
      :class:`MergeConflict` marker carrying all three competing values.
    * :attr:`ThreeWayMerge.changes` - one detail row per detected
      addition, deletion, field modification, conflict or duplicate key,
      suitable for exporting (e.g. to CSV) for manual review.

    E.g.::

        >>> import petl as etl
        >>> base = [['id', 'name', 'price', 'supplier'],
        ...         [1, 'widget', 10, 'acme'],
        ...         [2, 'gadget', 20, 'acme'],
        ...         [3, 'gizmo', 30, 'globex']]
        >>> # purchasing renames product 1 and changes the supplier of 3
        ... left = [['id', 'name', 'price', 'supplier'],
        ...         [1, 'widget v2', 11, 'acme'],
        ...         [2, 'gadget', 20, 'acme'],
        ...         [3, 'gizmo', 30, 'initech']]
        >>> # operations also changes the price of 1 and deletes product 2
        ... right = [['id', 'name', 'price', 'supplier'],
        ...          [1, 'widget', 12, 'acme'],
        ...          [3, 'gizmo', 30, 'globex']]
        >>> tm = etl.threemerge(base, left, right, key='id')
        >>> tm.merged
        +----+-------------+-------------------------------------------+-----------+
        | id | name        | price                                     | supplier  |
        +====+=============+===========================================+===========+
        |  1 | 'widget v2' | MergeConflict(base=10, left=11, right=12) | 'acme'    |
        +----+-------------+-------------------------------------------+-----------+
        |  3 | 'gizmo'     |                                        30 | 'initech' |
        +----+-------------+-------------------------------------------+-----------+

    The non-conflicting edits (the rename on the left and the supplier
    change on the left) are applied automatically; product 2 was deleted
    on the right and disappears, while the conflicting price edit is
    flagged rather than guessed.

    The change details make every detected change explicit. The detail
    table begins with the key column(s), followed by the fixed columns
    ``change``, ``side``, ``field``, ``base_value``, ``left_value`` and
    ``right_value``::

        >>> tm.changes
        +----+------------+---------+------------+------------+-------------+-------------+
        | id | change     | side    | field      | base_value | left_value  | right_value |
        +====+============+=========+============+============+=============+=============+
        |  1 | 'modified' | 'left'  | 'name'     | 'widget'   | 'widget v2' | 'widget'    |
        +----+------------+---------+------------+------------+-------------+-------------+
        |  1 | 'conflict' | ''      | 'price'    |         10 |          11 |          12 |
        +----+------------+---------+------------+------------+-------------+-------------+
        |  2 | 'deleted'  | 'right' | 'name'     | 'gadget'   | None        | None        |
        +----+------------+---------+------------+------------+-------------+-------------+
        |  2 | 'deleted'  | 'right' | 'price'    |         20 | None        | None        |
        +----+------------+---------+------------+------------+-------------+-------------+
        |  2 | 'deleted'  | 'right' | 'supplier' | 'acme'     | None        | None        |
        +----+------------+---------+------------+------------+-------------+-------------+
        ...

    The change type constants are available as
    :data:`petl.transform.threemerge.ADDED`, :data:`DELETED`,
    :data:`MODIFIED`, :data:`CONFLICT` and :data:`DUPLICATE`, and the
    side constants as :data:`BASE`, :data:`LEFT` and :data:`RIGHT`.

    Merge semantics, per key:

    * **added** - the key exists on one side only (and not in the base):
      the row is included and one ``added`` detail per non-key field is
      reported.
    * **deleted** - the key is in the base and missing from one side and
      unchanged on the other: the row is dropped and a ``deleted``
      detail is reported. A row deleted identically on both sides is also
      dropped cleanly.
    * **modified** - only one side changed a field relative to the base:
      that value is used and a ``modified`` detail is reported. If both
      sides made the identical change it is applied cleanly without a
      conflict, but still reported with ``side`` set to ``'both'``.
    * **conflict** - both sides changed the same field to different
      values, or one side deleted a row that the other modified: a
      :class:`MergeConflict` is placed in the merged row and a
      ``conflict`` detail is reported.
    * **duplicate** - a side contains more than one row for a key: the
      first row is used for merging and a ``duplicate`` detail is reported
      for every extra row (one per non-key field), with ``side`` naming
      the offending table.

    Once the conflicts in the change detail table have been reviewed,
    feed the reviewed decisions to :func:`resolve` to produce the final,
    conflict-free table, or call :meth:`ThreeWayMerge.resolve` directly.

    The three input tables do not have to fit in memory and each is read
    through the standard external-memory :func:`petl.transform.sorts.sort`
    machinery (respecting `buffersize`, `tempdir` and `cache`), after
    which the merge itself streams the three sorted tables in a single
    pass. If the inputs are already sorted by `key`, pass
    ``presorted=True`` to skip sorting entirely. The returned object owns
    the sorting spool resources; call :meth:`ThreeWayMerge.close` (or use
    it as a context manager) to release them deterministically.

    """

    return ThreeWayMerge(base, left, right, key, presorted=presorted,
                         buffersize=buffersize, tempdir=tempdir, cache=cache)


Table.threemerge = threemerge


class ThreeWayMerge(Table):
    """Result of a three-way merge.

    Not normally instantiated directly - use :func:`threemerge`. The
    object is itself a lazy table equal to :attr:`merged`, and also
    exposes :attr:`changes` and :meth:`resolve`.

    """

    def __init__(self, base, left, right, key, presorted=False,
                 buffersize=None, tempdir=None, cache=True):
        if presorted:
            self._base = base
            self._left = left
            self._right = right
        else:
            # sorting spools one-shot sources so later passes read the
            # spool rather than re-reading the source; it also validates
            # the key on the first pass
            self._base = sort(base, key, buffersize=buffersize,
                              tempdir=tempdir, cache=cache)
            self._left = sort(left, key, buffersize=buffersize,
                              tempdir=tempdir, cache=cache)
            self._right = sort(right, key, buffersize=buffersize,
                               tempdir=tempdir, cache=cache)
        self.key = key
        self.presorted = presorted
        self.buffersize = buffersize
        self.tempdir = tempdir
        # header metadata is computed lazily and cached so repeatedly
        # iterating the merged/changes views never re-opens the sources
        self._header_meta = None

    # -- headers ---------------------------------------------------------

    def _headers_and_indices(self):
        if self._header_meta is None:
            bhdr = tuple(next(iter(self._base)))
            lhdr = tuple(next(iter(self._left)))
            rhdr = tuple(next(iter(self._right)))
            # output fields: base fields first, then anything extra from
            # the edited tables, preserving first-seen order
            outhdr = list()
            for hdr in (bhdr, lhdr, rhdr):
                for f in hdr:
                    f = text_type(f)
                    if f not in outhdr:
                        outhdr.append(f)
            # validating here (rather than in the constructor) means
            # presorted one-shot sources are not read before the first
            # real iteration
            key_indices = asindices(outhdr, self.key)
            key_fields = tuple(outhdr[i] for i in key_indices)
            self._header_meta = (tuple(outhdr), tuple(key_indices),
                                 key_fields, bhdr, lhdr, rhdr)
        return self._header_meta

    def _groups(self):
        """Yield a :class:`_MergeGroup` for every distinct key, streaming
        the three sorted input tables in one pass."""

        (outhdr, key_indices, key_fields,
         bhdr, lhdr, rhdr) = self._headers_and_indices()
        bget = _keygetter(bhdr, key_fields)
        lget = _keygetter(lhdr, key_fields)
        rget = _keygetter(rhdr, key_fields)

        bit = _standardised(self._base, bhdr, outhdr)
        lit = _standardised(self._left, lhdr, outhdr)
        rit = _standardised(self._right, rhdr, outhdr)

        # discard the standardised headers
        next(bit)
        next(lit)
        next(rit)

        b = _next_or_none(bit)
        l = _next_or_none(lit)
        r = _next_or_none(rit)
        bkey = bget(b) if b is not None else None
        lkey = lget(l) if l is not None else None
        rkey = rget(r) if r is not None else None

        while b is not None or l is not None or r is not None:
            candidates = [k for k in (bkey, lkey, rkey)
                          if k is not None]
            current = min(candidates, key=Comparable)

            brows, lrows, rrows = [], [], []
            while b is not None and bkey == current:
                brows.append(b)
                b = _next_or_none(bit)
                bkey = bget(b) if b is not None else None
            while l is not None and lkey == current:
                lrows.append(l)
                l = _next_or_none(lit)
                lkey = lget(l) if l is not None else None
            while r is not None and rkey == current:
                rrows.append(r)
                r = _next_or_none(rit)
                rkey = rget(r) if r is not None else None

            yield _MergeGroup(
                key=tuple(current),
                key_fields=key_fields,
                base=brows[0] if brows else None,
                left=lrows[0] if lrows else None,
                right=rrows[0] if rrows else None,
                base_duplicates=brows[1:],
                left_duplicates=lrows[1:],
                right_duplicates=rrows[1:])

    def itermerged(self):
        """Yield the header and merged data rows."""
        outhdr, key_indices, key_fields, _, _, _ = self._headers_and_indices()
        yield outhdr
        for group in self._groups():
            merged, _ = _merge_group(group, outhdr, key_indices)
            if merged is not None:
                yield merged

    def iterchanges(self):
        """Yield the change-detail header and detail rows."""
        outhdr, key_indices, key_fields, _, _, _ = self._headers_and_indices()
        header = tuple(key_fields) + CHANGE_HEADER
        yield header
        for group in self._groups():
            for change in _change_rows(group, outhdr, key_indices):
                yield change

    # -- public views ----------------------------------------------------

    @property
    def merged(self):
        """Lazy table of merged rows, with :class:`MergeConflict` markers
        for unresolved field conflicts."""
        return _MergedView(self)

    @property
    def changes(self):
        """Lazy table describing every addition, deletion, modification,
        conflict and duplicate key encountered during the merge."""
        return _ChangesView(self)

    def resolve(self, resolutions):
        """Apply reviewed conflict `resolutions` and return a lazy,
        conflict-free merged table.

        See :func:`resolve` for the accepted resolution formats and
        semantics.

        """
        return resolve(self, resolutions)

    # -- resource management --------------------------------------------

    def close(self):
        """Release any spool resources held by the internal sorted
        tables. Safe to call more than once."""
        for table in (self._base, self._left, self._right):
            clear = getattr(table, 'clearcache', None)
            if clear is not None:
                clear()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # the object itself behaves as the merged table
    def __iter__(self):
        return self.itermerged()


class _MergedView(Table):

    def __init__(self, merge_result):
        # N.B., the attribute is not called ``merge`` because that name is
        # already a fluent Table method (see test_method_shadow)
        self.merge_result = merge_result

    def __iter__(self):
        return self.merge_result.itermerged()


class _ChangesView(Table):

    def __init__(self, merge_result):
        self.merge_result = merge_result

    def __iter__(self):
        return self.merge_result.iterchanges()


def _next_or_none(it):
    try:
        return next(it)
    except StopIteration:
        return None


def _keygetter(hdr, key_fields):
    indices = asindices(hdr, key_fields)
    if len(indices) == 1:
        idx = indices[0]
        return lambda row: (row[idx],)
    return lambda row: tuple(row[i] for i in indices)


def _standardised(table, hdr, outhdr):
    """Yield the table header followed by each row padded/reordered to
    match `outhdr`; fields absent from this table are filled with
    ``None`` and extra fields are dropped."""
    indices = [hdr.index(f) if f in hdr else None for f in outhdr]
    yield tuple(outhdr)
    it = iter(table)
    next(it)  # discard source header
    for row in it:
        out = []
        for idx in indices:
            if idx is None or idx >= len(row):
                out.append(None)
            else:
                out.append(row[idx])
        yield tuple(out)


def _merge_group(group, outhdr, key_indices):
    """Compute the merged row and the list of change detail tuples for a
    single key group. Returns ``(merged_row_or_None, change_rows)``; the
    merged row is ``None`` when the row was deleted."""

    b, l, r = group.base, group.left, group.right
    changes = []

    # duplicate keys are always reported, whichever side they come from;
    # one detail per non-key field for each extra row
    for side, duplicates in ((BASE, group.base_duplicates),
                             (LEFT, group.left_duplicates),
                             (RIGHT, group.right_duplicates)):
        for duplicate in duplicates:
            changes.extend(
                _duplicate_detail(group, side, duplicate, outhdr,
                                  key_indices, b, l, r))

    if b is None:
        # row did not exist in the base
        if l is not None and r is None:
            return l, changes + _row_level(group, ADDED, LEFT,
                                           l, outhdr, key_indices)
        if r is not None and l is None:
            return r, changes + _row_level(group, ADDED, RIGHT,
                                           r, outhdr, key_indices)
        if l is not None and r is not None:
            # independently added by both sides: per-field merge
            return _fieldwise_merge(group, outhdr, key_indices,
                                    added=True, prior=changes)
        # nothing on either side - cannot happen for a real group
        return None, changes

    # row exists in the base
    if l is None and r is None:
        # deleted on both sides - a clean, agreed deletion
        return None, changes + _row_level(group, DELETED, None,
                                          b, outhdr, key_indices)
    if l is None:
        if r == b:
            # deleted on the left, untouched on the right
            return None, changes + _row_level(group, DELETED, LEFT,
                                              b, outhdr, key_indices)
        # deleted on the left but modified on the right
        return _modify_delete(group, outhdr, key_indices,
                              deleting=LEFT, surviving=r, prior=changes)
    if r is None:
        if l == b:
            # deleted on the right, untouched on the left
            return None, changes + _row_level(group, DELETED, RIGHT,
                                              b, outhdr, key_indices)
        # deleted on the right but modified on the left
        return _modify_delete(group, outhdr, key_indices,
                              deleting=RIGHT, surviving=l, prior=changes)

    # present on all three sides - field-level three-way merge
    return _fieldwise_merge(group, outhdr, key_indices,
                            added=False, prior=changes)


def _duplicate_detail(group, side, duplicate, outhdr, key_indices, b, l, r):
    """Build the detail rows for an extra (duplicate key) row. The
    duplicate's values are shown in the value columns of the offending
    side; the chosen rows appear on the other sides. One row-level detail
    (``field=None``) is emitted for key-only tables, otherwise one detail
    per non-key field."""
    value_indices = [i for i in range(len(outhdr))
                     if i not in key_indices]
    if side == BASE:
        db, dl, dr = duplicate, l, r
    elif side == LEFT:
        db, dl, dr = b, duplicate, r
    else:
        db, dl, dr = b, l, duplicate
    if not value_indices:
        return [_detail(group, DUPLICATE, side, None, None, None, None)]
    details = []
    for idx in value_indices:
        field = outhdr[idx]
        details.append(_detail(group, DUPLICATE, side, field,
                               _cell(db, idx), _cell(dl, idx),
                               _cell(dr, idx)))
    return details


def _fieldwise_merge(group, outhdr, key_indices, added, prior):
    b, l, r = group.base, group.left, group.right
    changes = list(prior)
    merged = []
    for i, field in enumerate(outhdr):
        if i in key_indices:
            # key fields are never in conflict; take the available value
            merged.append(l[i] if l is not None else
                          (r[i] if r is not None else b[i]))
            continue
        bv = b[i] if b is not None else None
        lv = l[i] if l is not None else None
        rv = r[i] if r is not None else None
        if lv == rv:
            # both sides agree (including both unchanged)
            merged.append(lv)
            if b is not None and bv != lv:
                # both sides made the identical edit: apply it cleanly but
                # keep the audit trail
                changes.append(_detail(group, MODIFIED, BOTH, field,
                                       bv, lv, rv))
            elif added and lv is not None:
                changes.append(_detail(group, ADDED,
                                       _side_for_add(l, r), field,
                                       bv, lv, rv))
            continue
        if b is None:
            # both sides added the key but give this field different values
            merged.append(MergeConflict(bv, lv, rv))
            changes.append(_detail(group, CONFLICT, '', field,
                                   bv, lv, rv))
        elif lv == bv:
            # only the right side changed
            merged.append(rv)
            changes.append(_detail(group, MODIFIED, RIGHT, field,
                                   bv, lv, rv))
        elif rv == bv:
            # only the left side changed
            merged.append(lv)
            changes.append(_detail(group, MODIFIED, LEFT, field,
                                   bv, lv, rv))
        else:
            # both changed, to different values
            merged.append(MergeConflict(bv, lv, rv))
            changes.append(_detail(group, CONFLICT, '', field,
                                   bv, lv, rv))
    return tuple(merged), changes


def _side_for_add(l, r):
    if l is not None:
        return LEFT
    return RIGHT


def _modify_delete(group, outhdr, key_indices, deleting, surviving, prior):
    """One side deleted the base row while the other side edited it.
    The surviving row is kept, but every edited field is flagged as a
    conflict so the deletion is not silently overridden."""
    b = group.base
    changes = list(prior)
    merged = list(surviving)
    for i, field in enumerate(outhdr):
        if i in key_indices:
            continue
        bv = b[i]
        sv = surviving[i]
        if bv != sv:
            if deleting == LEFT:
                lv, rv = None, sv
            else:
                lv, rv = sv, None
            merged[i] = MergeConflict(bv, lv, rv)
            changes.append(_detail(group, CONFLICT, deleting, field,
                                   bv, lv, rv))
    return tuple(merged), changes


def _row_level(group, change, side, row, outhdr, key_indices):
    """Build the detail rows for an addition or deletion, one per
    non-key field (a single row-level detail with ``field=None`` when the
    table has no non-key fields)."""
    value_fields = [f for i, f in enumerate(outhdr)
                    if i not in key_indices]
    b, l, r = _side_rows(group, row, change, side)
    if not value_fields:
        # key-only table: emit a single row-level detail showing the
        # key value carried by the row responsible for the event
        if change == DELETED:
            bv, lv, rv = group.key[0], None, None
        elif side == LEFT:
            bv, lv, rv = None, group.key[0], None
        else:
            bv, lv, rv = None, None, group.key[0]
        return [_detail(group, change, side, None, bv, lv, rv)]
    details = []
    for field in value_fields:
        idx = outhdr.index(field)
        details.append(_detail(group, change, side, field,
                               _cell(b, idx), _cell(l, idx),
                               _cell(r, idx)))
    return details


def _side_rows(group, row, change, side):
    """For add/delete details, populate the value columns from the row
    responsible for the event on its side, leaving the other sides
    empty."""
    b = l = r = None
    if change == ADDED:
        if side == LEFT:
            l = row
        else:
            r = row
    else:
        # deletion: show the base values that were removed
        b = group.base
    return b, l, r


def _cell(row, idx):
    if row is None or idx >= len(row):
        return None
    return row[idx]


def _change_rows(group, outhdr, key_indices):
    _, changes = _merge_group(group, outhdr, key_indices)
    return changes


def _detail(group, change, side, field, bv, lv, rv):
    return tuple(group.key) + (change, side, field, bv, lv, rv)


def resolve(merged, resolutions):
    """Apply reviewed conflict decisions to a three-way merge result and
    return a lazy, conflict-free table.

    `merged` is either a :class:`ThreeWayMerge` (or its :attr:`merged`
    table) and `resolutions` is one of:

    * a petl table (or any iterable of row tuples, including a filtered
      copy of the :attr:`ThreeWayMerge.changes` table augmented with a
      ``resolution`` column) whose header contains the key field(s), a
      ``field`` column and a ``resolution`` column; or
    * a mapping of ``(key, field)`` to a decision, where ``key`` is the
      tuple of key values (or a single value for a single-field key) and
      ``field`` is the field name. A field of ``None`` applies the
      decision to every unresolved field of that row. A nested mapping of
      ``key`` to ``{field: decision}`` is also accepted;

    Each decision is one of:

    * ``'base'``, ``'left'`` or ``'right'`` - take the corresponding
      value from the :class:`MergeConflict` marker;
    * any other value - use that literal value directly.

    E.g.::

        >>> import petl as etl
        >>> base = [['id', 'price'], [1, 10]]
        >>> left = [['id', 'price'], [1, 11]]
        >>> right = [['id', 'price'], [1, 12]]
        >>> tm = etl.threemerge(base, left, right, key='id')
        >>> decisions = [['id', 'field', 'resolution'],
        ...              [1, 'price', 'right']]
        >>> tm.resolve(decisions).lookall()
        +----+-------+
        | id | price |
        +====+=======+
        |  1 |    12 |
        +----+-------+

    Conflicts without a matching decision keep their
    :class:`MergeConflict` marker, so the output can be re-checked for
    completeness.

    """

    return _ResolvedView(merged, resolutions)


Table.resolve = resolve


class _ResolvedView(Table):

    def __init__(self, merged, resolutions):
        # unwrap a ThreeWayMerge to its merged table; keep a reference to
        # the merge object as well for its key metadata
        if isinstance(merged, ThreeWayMerge):
            self._merge = merged
            self._source = merged.merged
        elif isinstance(merged, _MergedView):
            self._merge = merged.merge_result
            self._source = merged
        else:
            self._merge = None
            self._source = merged
        self._resolutions = resolutions

    def _decision_map(self, key_fields=None):
        resolutions = self._resolutions
        if hasattr(resolutions, 'items'):
            return _normalise_mapping(resolutions)
        return _parse_resolution_table(resolutions, key_fields)

    def __iter__(self):
        it = iter(self._source)
        hdr = tuple(next(it))
        if self._merge is not None:
            _, key_indices, key_fields, _, _, _ = \
                self._merge._headers_and_indices()
        else:
            # standalone table: assume the first column is the key
            key_fields = (hdr[0],)
            key_indices = (0,)
        decisions = self._decision_map(key_fields)
        yield hdr
        for row in it:
            yield tuple(self._resolve_row(row, hdr, key_indices,
                                          decisions))

    def _resolve_row(self, row, hdr, key_indices, decisions):
        key = tuple(row[i] for i in key_indices)
        row_decisions = decisions.get(key, {})
        row_wide = row_decisions.get(None)
        out = list(row)
        for i, value in enumerate(row):
            if not isinstance(value, MergeConflict):
                continue
            field = hdr[i]
            decision = row_decisions.get(field, row_wide)
            if decision is None:
                continue
            out[i] = _apply_decision(value, decision)
        return out


def _normalise_mapping(mapping):
    normalised = {}
    for key, value in mapping.items():
        # accept both a flat {(key, field): decision} mapping and a nested
        # {key: {field: decision}} mapping
        if hasattr(value, 'items'):
            row_decisions = value
        else:
            if not isinstance(key, tuple):
                raise ArgumentError(
                    'mapping resolutions must be keyed by (key, field) '
                    'pairs or by key to a field/decision mapping')
            key, field = key
            row_decisions = {field: value}
        if not isinstance(key, tuple):
            key = (key,)
        bucket = normalised.setdefault(key, {})
        for field, decision in row_decisions.items():
            bucket[field] = decision
    return normalised


def _parse_resolution_table(table, key_fields=None):
    it = iter(table)
    hdr = [text_type(f) for f in next(it)]
    if CHANGE_FIELD not in hdr:
        raise ArgumentError(
            'resolution table must contain a "%s" column' % CHANGE_FIELD)
    resolution_col = 'resolution'
    if resolution_col not in hdr:
        raise ArgumentError(
            'resolution table must contain a "%s" column' % resolution_col)
    field_idx = hdr.index(CHANGE_FIELD)
    decision_idx = hdr.index(resolution_col)
    if key_fields is not None:
        # use the merge's own key fields when known, so extra context
        # columns in an exported review file are simply ignored; fall back
        # if the review file does not carry every key column
        names = [text_type(f) for f in key_fields]
        if all(name in hdr for name in names):
            key_indices = [hdr.index(name) for name in names]
        else:
            key_indices = _default_key_indices(hdr, field_idx, decision_idx)
    else:
        key_indices = _default_key_indices(hdr, field_idx, decision_idx)
    decisions = {}
    for row in it:
        key = tuple(row[i] for i in key_indices)
        field = row[field_idx]
        if field == '':
            field = None
        decisions.setdefault(key, {})[field] = row[decision_idx]
    return decisions


def _default_key_indices(hdr, field_idx, decision_idx):
    # without merge metadata, treat every column other than field and
    # resolution as a key column
    return [i for i, name in enumerate(hdr)
            if i not in (field_idx, decision_idx)]


def _apply_decision(conflict, decision):
    if decision == BASE:
        return conflict.base_value
    if decision == LEFT:
        return conflict.left_value
    if decision == RIGHT:
        return conflict.right_value
    return decision
