from __future__ import absolute_import, print_function, division


import pytest

import petl as etl
from petl.errors import ArgumentError, FieldSelectionError
from petl.test.helpers import eq_, ieq, ieq2
from petl.transform.sorts import SortView
from petl.transform.threemerge import (
    ADDED, BASE, BOTH, CONFLICT, DELETED, DUPLICATE, LEFT, MODIFIED, RIGHT,
    MergeConflict, ThreeWayMerge, resolve, threemerge)


# --------------------------------------------------------------------------
# Test fixtures
# --------------------------------------------------------------------------

def _base():
    return [['id', 'name', 'price'],
            [1, 'widget', 10],
            [2, 'gadget', 20],
            [3, 'gizmo', 30]]


def _left():
    # id 1: price edited; id 2: untouched; id 3: untouched
    return [['id', 'name', 'price'],
            [1, 'widget', 11],
            [2, 'gadget', 20],
            [3, 'gizmo', 30]]


def _right():
    # id 1: name edited; id 2: deleted; id 3: price edited
    return [['id', 'name', 'price'],
            [1, 'widget v2', 10],
            [3, 'gizmo', 33]]


# --------------------------------------------------------------------------
# Basic merge semantics
# --------------------------------------------------------------------------

def test_threemerge_non_conflicting_fields_combine():
    # both sides edit different fields of the same row - clean merge
    tm = threemerge(_base(), _left(), _right(), key='id')
    rows = list(tm.merged)
    eq_(('id', 'name', 'price'), rows[0])
    # id 1: left changed price to 11, right changed name to 'widget v2'
    eq_((1, 'widget v2', 11), rows[1])
    # id 3: only right changed the price
    eq_((3, 'gizmo', 33), rows[2])
    # id 2 was deleted on the right
    eq_(2, len(rows) - 1)


def test_threemerge_is_lazy_table():
    tm = threemerge(_base(), _left(), _right(), key='id')
    assert isinstance(tm, ThreeWayMerge)
    assert isinstance(tm, etl.Table)
    # the merge object itself iterates like the merged table
    ieq(tm.merged, tm)
    # views are re-iterable
    ieq2(tm.merged, tm.merged)
    ieq2(tm.changes, tm.changes)


def test_threemerge_identical_change_is_not_conflict():
    base = [['id', 'v'], [1, 'a']]
    left = [['id', 'v'], [1, 'b']]
    right = [['id', 'v'], [1, 'b']]
    tm = threemerge(base, left, right, key='id')
    ieq((('id', 'v'), (1, 'b')), tm.merged)
    changes = list(tm.changes)
    # no conflict rows at all ...
    assert all(row[1] != CONFLICT for row in changes[1:])
    # ... but the identical edit is still recorded for the audit trail
    eq_((1, MODIFIED, BOTH, 'v', 'a', 'b', 'b'), changes[1])


def test_threemerge_identical_change_to_none_is_reported():
    base = [['id', 'v'], [1, 9]]
    side = [['id', 'v'], [1, None]]
    tm = threemerge(base, side, side, key='id')
    ieq((('id', 'v'), (1, None)), tm.merged)
    eq_((1, MODIFIED, BOTH, 'v', 9, None, None), list(tm.changes)[1])


def test_threemerge_unchanged_rows_passthrough():
    base = [['id', 'v'], [1, 'a'], [2, 'b']]
    tm = threemerge(base,
                    [['id', 'v'], [1, 'a'], [2, 'b']],
                    [['id', 'v'], [1, 'a'], [2, 'b']],
                    key='id')
    ieq((('id', 'v'), (1, 'a'), (2, 'b')), tm.merged)
    eq_(1, len(list(tm.changes)))  # header only


def test_threemerge_field_conflict_marker_carries_all_values():
    base = [['id', 'v'], [1, 'a']]
    left = [['id', 'v'], [1, 'b']]
    right = [['id', 'v'], [1, 'c']]
    tm = threemerge(base, left, right, key='id')
    rows = list(tm.merged)
    marker = rows[1][1]
    assert isinstance(marker, MergeConflict)
    eq_(('a', 'b', 'c'), tuple(marker))
    eq_('a', marker.base_value)
    eq_('b', marker.left_value)
    eq_('c', marker.right_value)
    eq_('MergeConflict(base=\'a\', left=\'b\', right=\'c\')', repr(marker))


# --------------------------------------------------------------------------
# Additions and deletions
# --------------------------------------------------------------------------

def test_threemerge_added_on_one_side():
    base = [['id', 'v'], [1, 'a']]
    left = [['id', 'v'], [1, 'a'], [2, 'x']]
    right = [['id', 'v'], [1, 'a']]
    tm = threemerge(base, left, right, key='id')
    ieq((('id', 'v'), (1, 'a'), (2, 'x')), tm.merged)
    detail = [r for r in list(tm.changes) if r[0] == 2]
    eq_(1, len(detail))
    eq_((2, ADDED, LEFT, 'v', None, 'x', None), detail[0])


def test_threemerge_added_identically_on_both_sides():
    base = [['id', 'v']]
    side = [['id', 'v'], [9, 'z']]
    tm = threemerge(base, side, side, key='id')
    ieq((('id', 'v'), (9, 'z')), tm.merged)
    detail = [r for r in list(tm.changes) if r[0] == 9]
    # reported as an addition once, not as a conflict
    assert all(r[1] == ADDED for r in detail)


def test_threemerge_added_on_both_sides_with_conflict():
    base = [['id', 'v']]
    left = [['id', 'v'], [9, 'p']]
    right = [['id', 'v'], [9, 'q']]
    tm = threemerge(base, left, right, key='id')
    rows = list(tm.merged)
    assert isinstance(rows[1][1], MergeConflict)
    detail = list(tm.changes)[1]
    eq_((9, CONFLICT, '', 'v', None, 'p', 'q'), detail)


def test_threemerge_deleted_on_one_side():
    base = [['id', 'v'], [1, 'a'], [2, 'b']]
    left = [['id', 'v'], [1, 'a'], [2, 'b']]
    right = [['id', 'v'], [1, 'a']]  # deletes id 2
    tm = threemerge(base, left, right, key='id')
    ieq((('id', 'v'), (1, 'a')), tm.merged)
    detail = [r for r in list(tm.changes) if r[0] == 2]
    eq_(1, len(detail))
    eq_((2, DELETED, RIGHT, 'v', 'b', None, None), detail[0])


def test_threemerge_deleted_on_both_sides_is_clean():
    base = [['id', 'v'], [1, 'a'], [2, 'b']]
    side = [['id', 'v'], [1, 'a']]
    tm = threemerge(base, side, side, key='id')
    ieq((('id', 'v'), (1, 'a')), tm.merged)
    detail = [r for r in list(tm.changes) if r[0] == 2]
    eq_(1, len(detail))
    eq_(DELETED, detail[0][1])
    assert detail[0][2] is None  # no single side responsible


def test_threemerge_modify_delete_conflict():
    base = [['id', 'v'], [1, 'a'], [2, 'b']]
    left = [['id', 'v'], [1, 'a'], [2, 'B']]   # modifies 2
    right = [['id', 'v'], [1, 'a']]            # deletes 2
    tm = threemerge(base, left, right, key='id')
    rows = {r[0]: r for r in list(tm.merged)[1:]}
    # surviving row is kept but the edited field is flagged
    marker = rows[2][1]
    assert isinstance(marker, MergeConflict)
    eq_(('b', 'B', None), tuple(marker))
    detail = [r for r in list(tm.changes) if r[0] == 2]
    eq_(1, len(detail))
    eq_((2, CONFLICT, RIGHT, 'v', 'b', 'B', None), detail[0])

# --------------------------------------------------------------------------
# Duplicate keys
# --------------------------------------------------------------------------

def test_threemerge_duplicate_keys_are_reported():
    base = [['id', 'v'], [1, 'a']]
    left = [['id', 'v'], [1, 'x'], [1, 'y'], [3, 'z']]
    right = [['id', 'v'], [1, 'a']]
    tm = threemerge(base, left, right, key='id')
    duplicates = [r for r in list(tm.changes) if r[1] == DUPLICATE]
    eq_(1, len(duplicates))
    eq_((1, DUPLICATE, LEFT, 'v', 'a', 'y', 'a'), duplicates[0])
    # first row for the key is used for merging
    rows = list(tm.merged)
    eq_((1, 'x'), rows[1])
    # the extra, single-occurrence addition still goes through
    eq_((3, 'z'), rows[2])


def test_threemerge_duplicate_on_each_side():
    base = [['id', 'v'], [1, 'a'], [1, 'a2']]
    left = [['id', 'v'], [1, 'b'], [1, 'b2']]
    right = [['id', 'v'], [1, 'c'], [1, 'c2']]
    tm = threemerge(base, left, right, key='id')
    dupes = [r for r in list(tm.changes) if r[1] == DUPLICATE]
    sides = sorted(r[2] for r in dupes)
    eq_([BASE, LEFT, RIGHT], sides)


def test_threemerge_duplicate_reports_each_non_key_field():
    base = [['id', 'a', 'b'], [1, 1, 2]]
    left = [['id', 'a', 'b'], [1, 1, 2], [1, 9, 8]]
    right = [['id', 'a', 'b'], [1, 1, 2]]
    tm = threemerge(base, left, right, key='id')
    dupes = [r for r in list(tm.changes) if r[1] == DUPLICATE]
    # one detail per non-key field for the single extra row
    eq_(2, len(dupes))
    eq_([(1, DUPLICATE, LEFT, 'a', 1, 9, 1),
         (1, DUPLICATE, LEFT, 'b', 2, 8, 2)], dupes)


# --------------------------------------------------------------------------
# Change detail table
# --------------------------------------------------------------------------

def test_changes_header_and_modification_detail():
    tm = threemerge(_base(), _left(), _right(), key='id')
    changes = list(tm.changes)
    eq_(('id', 'change', 'side', 'field', 'base_value',
         'left_value', 'right_value'), changes[0])
    by_key = {}
    for row in changes[1:]:
        by_key.setdefault(row[0], []).append(row)
    # id 1: left modified price, right modified name
    id1 = {(r[2], r[3]): r for r in by_key[1]}
    eq_((1, MODIFIED, LEFT, 'price', 10, 11, 10), id1[(LEFT, 'price')])
    eq_((1, MODIFIED, RIGHT, 'name', 'widget', 'widget', 'widget v2'),
        id1[(RIGHT, 'name')])
    # id 2: deleted on the right, one detail per non-key field
    id2 = by_key[2]
    eq_([DELETED, DELETED], [r[1] for r in id2])
    assert all(r[2] == RIGHT for r in id2)
    # id 3: right modified the price
    eq_((3, MODIFIED, RIGHT, 'price', 30, 30, 33), by_key[3][0])


def test_changes_can_be_exported_and_reimported(tmp_path):
    tm = threemerge(_base(), _left(), _right(), key='id')
    path = str(tmp_path / 'changes.csv')
    etl.tocsv(tm.changes, path)
    reloaded = etl.fromcsv(path)
    eq_(tuple(tm.changes.header()), tuple(reloaded.header()))
    # every detail row survives the round trip
    eq_(len(list(tm.changes)), len(list(reloaded)))


def test_changes_for_key_only_table():
    base = [['id'], [1], [2]]
    left = [['id'], [1], [3]]
    right = [['id'], [1]]
    tm = threemerge(base, left, right, key='id')
    changes = list(tm.changes)
    # one row-level detail each for the deletion and the addition
    eq_((2, DELETED, None, None, 2, None, None), changes[1])
    eq_((3, ADDED, LEFT, None, None, 3, None), changes[2])


# --------------------------------------------------------------------------
# Compound keys and differing schemas
# --------------------------------------------------------------------------

def test_threemerge_compound_key():
    base = [['k1', 'k2', 'v'], ['a', 'x', 1], ['b', 'y', 2]]
    left = [['k1', 'k2', 'v'], ['a', 'x', 9], ['b', 'y', 2]]
    right = [['k1', 'k2', 'v'], ['a', 'x', 1], ['b', 'y', 8]]
    tm = threemerge(base, left, right, key=['k1', 'k2'])
    rows = list(tm.merged)
    # distinct keys, each with a clean single-side modification
    values = {(r[0], r[1]): r[2] for r in rows[1:]}
    eq_(9, values[('a', 'x')])
    eq_(8, values[('b', 'y')])
    header = list(tm.changes)[0]
    eq_(('k1', 'k2', 'change', 'side', 'field',
         'base_value', 'left_value', 'right_value'), header)


def test_threemerge_different_field_sets_are_unioned():
    base = [['id', 'a'], [1, 1]]
    left = [['id', 'a', 'b'], [1, 1, 2]]
    right = [['id', 'a', 'c'], [1, 1, 3]]
    tm = threemerge(base, left, right, key='id')
    eq_(('id', 'a', 'b', 'c'), tuple(tm.merged.header()))
    row = list(tm.merged)[1]
    eq_((1, 1, 2, 3), row)


def test_threemerge_short_rows_are_padded():
    base = [['id', 'a', 'b'], [1, 1, 1]]
    left = [['id', 'a', 'b'], [1, 2]]          # short row
    right = [['id', 'a', 'b'], [1, 1, 1]]
    tm = threemerge(base, left, right, key='id')
    eq_((1, 2, None), tuple(list(tm.merged)[1]))


def test_threemerge_missing_key_field_raises():
    # a bad key is reported when the merge is first evaluated
    with pytest.raises(FieldSelectionError):
        list(threemerge([['id']], [['id']], [['id']], key='nope').merged)
    with pytest.raises(FieldSelectionError):
        list(threemerge([['id']], [['id']], [['id']], key='nope',
                        presorted=True).merged)


# --------------------------------------------------------------------------
# Conflict resolution
# --------------------------------------------------------------------------

def _conflict_merge():
    base = [['id', 'price', 'qty'], [1, 10, 5], [2, 20, 7]]
    left = [['id', 'price', 'qty'], [1, 11, 5], [2, 21, 7]]
    right = [['id', 'price', 'qty'], [1, 12, 6], [2, 22, 7]]
    return threemerge(base, left, right, key='id')


def test_resolve_with_table_decisions():
    tm = _conflict_merge()
    decisions = [['id', 'field', 'resolution'],
                 [1, 'price', 'right'],
                 [2, 'price', 'base']]
    resolved = list(tm.resolve(decisions))
    eq_((1, 12, 6), resolved[1])       # right price; qty 6 was a clean edit
    eq_((2, 20, 7), resolved[2])       # base price
    # no markers remain for resolved fields
    assert not any(isinstance(v, MergeConflict) for row in resolved for v in row)


def test_resolve_with_flat_mapping():
    tm = _conflict_merge()
    decisions = {(1, 'price'): 'left',
                 (2, 'price'): 'right'}
    resolved = list(tm.resolve(decisions))
    eq_((1, 11, 6), resolved[1])
    eq_((2, 22, 7), resolved[2])


def test_resolve_with_nested_mapping():
    tm = _conflict_merge()
    decisions = {1: {'price': 'base'},
                 2: {'price': 'left'}}
    resolved = list(tm.resolve(decisions))
    eq_((1, 10, 6), resolved[1])
    eq_((2, 21, 7), resolved[2])


def test_resolve_row_wide_decision_with_none_field():
    tm = _conflict_merge()
    decisions = {2: {None: 'right'}}
    resolved = list(tm.resolve(decisions))
    eq_((2, 22, 7), resolved[2])
    # row 1 was not mentioned and keeps its marker
    assert isinstance(resolved[1][1], MergeConflict)


def test_resolve_literal_value():
    tm = _conflict_merge()
    decisions = {(1, 'price'): 99}
    resolved = list(tm.resolve(decisions))
    eq_((1, 99, 6), resolved[1])


def test_resolve_unresolved_conflicts_keep_marker():
    tm = _conflict_merge()
    resolved = list(tm.resolve([['id', 'field', 'resolution']]))
    for row in resolved[1:]:
        assert isinstance(row[1], MergeConflict)


def test_resolve_standalone_table():
    table = etl.wrap([['id', 'v'],
                      [1, MergeConflict('a', 'b', 'c')]])
    resolved = list(resolve(table, {(1, 'v'): 'left'}))
    eq_((1, 'b'), resolved[1])


def test_resolve_table_requires_field_and_resolution_columns():
    tm = _conflict_merge()
    with pytest.raises(ArgumentError):
        list(tm.resolve([['id', 'oops'], [1, 'x']]))
    with pytest.raises(ArgumentError):
        list(tm.resolve([['id', 'field'], [1, 'price']]))


def test_resolve_table_ignores_extra_context_columns():
    tm = _conflict_merge()
    # a review file exported from the change table may carry extra
    # value/context columns alongside the required field/resolution ones
    decisions = [['id', 'field', 'base_value', 'resolution', 'reviewer'],
                 [1, 'price', 10, 'right', 'alice'],
                 [2, 'price', 20, 'left', 'bob']]
    resolved = list(tm.resolve(decisions))
    eq_((1, 12, 6), resolved[1])
    eq_((2, 21, 7), resolved[2])


def test_resolve_bad_mapping_key_raises():
    tm = _conflict_merge()
    with pytest.raises(ArgumentError):
        list(tm.resolve({1: 'left'}))


def test_resolve_roundtrip_through_changes_table(tmp_path):
    # simulate the full review workflow: export changes, annotate, resolve
    tm = _conflict_merge()
    path = str(tmp_path / 'review.csv')
    conflicts = etl.select(tm.changes, lambda r: r.change == CONFLICT)
    reviewed = etl.addfield(conflicts, 'resolution', 'right')
    etl.tocsv(reviewed.cut('id', 'field', 'resolution'), path)
    decisions = etl.fromcsv(path)
    # CSV loads keys as strings, so build a string-keyed merge for the check
    base = [['id', 'price'], ['1', 10]]
    left = [['id', 'price'], ['1', 11]]
    right = [['id', 'price'], ['1', 12]]
    tm2 = threemerge(base, left, right, key='id')
    resolved = list(tm2.resolve(decisions))
    # the directive is text from CSV, but the resolved value comes from
    # the merge conflict marker itself
    eq_(('1', 12), resolved[1])


# --------------------------------------------------------------------------
# Streaming, spooling and resource management
# --------------------------------------------------------------------------

class _OneShot(etl.Table):
    """Table that can only be iterated a fixed number of times, modelling
    a source (file, generator-backed view) that cannot be re-read on
    demand. External sorting is allowed its single read-and-spool pass;
    the merge itself must then run from the spool, not the source."""

    def __init__(self, rows, passes=2):
        self._rows = [tuple(r) for r in rows]
        self._remaining = passes

    def __iter__(self):
        if self._remaining <= 0:
            raise AssertionError('source re-read beyond its allowed passes')
        self._remaining -= 1
        for row in self._rows:
            yield row


def test_threemerge_streams_through_spool_with_small_buffersize(tmp_path):
    n = 50
    base = [['id', 'v']] + [[i, i] for i in range(n)]
    left = [['id', 'v']] + [[i, i + 1000] for i in range(n)]
    right = [['id', 'v']] + [[i, i] for i in range(n)]
    tm = threemerge(base, left, right, key='id', buffersize=7,
                    tempdir=str(tmp_path))
    # the internal tables really are externally sorted
    assert all(isinstance(t, SortView) for t in
               (tm._base, tm._left, tm._right))
    # spool files are created on the first pass through the data
    rows = list(tm.merged)
    spooled = [f for f in tmp_path.iterdir()]
    assert spooled  # chunk files were actually written
    eq_(n + 1, len(rows))
    eq_((0, 1000), tuple(rows[1]))
    # every left-only edit is reported, streamed without holding all rows
    changes = [r for r in list(tm.changes) if r[1] == MODIFIED]
    eq_(n, len(changes))
    tm.close()


def test_threemerge_one_shot_sources_only_read_via_spool(tmp_path):
    # petl's external sort reads a source twice while building the spool
    # (once for the header, once for the data) and never again once the
    # spool is cached. Allow exactly those two passes and assert the
    # merged/changes views and their re-iterations add no further reads.
    base = _OneShot([['id', 'v'], [1, 'a'], [2, 'b'], [3, 'c']], passes=2)
    left = _OneShot([['id', 'v'], [1, 'x'], [2, 'b'], [3, 'c']], passes=2)
    right = _OneShot([['id', 'v'], [1, 'a'], [2, 'b'], [3, 'y']], passes=2)
    tm = threemerge(base, left, right, key='id', buffersize=2,
                    tempdir=str(tmp_path))
    first = list(tm.merged)
    second = list(tm.changes)   # would re-read sources without spooling
    third = list(tm.merged)
    eq_(first, third)
    assert any(r[1] == MODIFIED for r in second[1:])
    # every source still has headroom: the merge itself added no reads
    assert all(s._remaining == 0 for s in (base, left, right))
    tm.close()


def test_threemerge_close_releases_spool_files(tmp_path):
    base = [['id', 'v']] + [[i, i] for i in range(20)]
    side = [['id', 'v']] + [[i, i] for i in range(20)]
    tm = threemerge(base, side, side, key='id', buffersize=3,
                    tempdir=str(tmp_path))
    list(tm.merged)
    assert list(tmp_path.iterdir())  # files present while open
    tm.close()
    eq_([], list(tmp_path.iterdir()))
    # closing twice is harmless
    tm.close()


def test_threemerge_context_manager_closes(tmp_path):
    base = [['id', 'v']] + [[i, i] for i in range(20)]
    with threemerge(base, base, base, key='id', buffersize=3,
                    tempdir=str(tmp_path)) as tm:
        list(tm.merged)
    eq_([], list(tmp_path.iterdir()))


def test_threemerge_presorted_skips_sorting():
    base = etl.sort([['id', 'v'], [2, 'b'], [1, 'a']], 'id')
    left = etl.sort([['id', 'v'], [2, 'b'], [1, 'x']], 'id')
    right = etl.sort([['id', 'v'], [2, 'y'], [1, 'a']], 'id')
    tm = threemerge(base, left, right, key='id', presorted=True)
    # inputs are stored unwrapped, i.e. not re-sorted
    assert tm._base is base and tm._left is left and tm._right is right
    rows = list(tm.merged)
    eq_((1, 'x'), tuple(rows[1]))
    eq_((2, 'y'), tuple(rows[2]))


def test_threemerge_empty_tables():
    tm = threemerge([['id', 'v']], [['id', 'v']], [['id', 'v']], key='id')
    ieq((('id', 'v'),), tm.merged)
    eq_(1, len(list(tm.changes)))  # header only


# --------------------------------------------------------------------------
# Fluent API and package exports
# --------------------------------------------------------------------------

def test_fluent_api():
    tm = etl.wrap(_base()).threemerge(_left(), _right(), key='id')
    assert isinstance(tm, ThreeWayMerge)
    ieq(tm.merged, etl.threemerge(_base(), _left(), _right(), key='id'))
    resolved = etl.wrap([['id', 'v'],
                         [1, MergeConflict(1, 2, 3)]]).resolve(
        {(1, 'v'): 'right'})
    eq_((1, 3), list(resolved)[1])


def test_public_exports():
    for name in ('threemerge', 'resolve', 'MergeConflict',
                 'ADDED', 'DELETED', 'MODIFIED', 'CONFLICT', 'DUPLICATE',
                 'BASE', 'LEFT', 'RIGHT', 'BOTH'):
        assert hasattr(etl, name), name
    eq_('added', etl.ADDED)
    eq_('conflict', etl.CONFLICT)
    eq_('both', etl.BOTH)
