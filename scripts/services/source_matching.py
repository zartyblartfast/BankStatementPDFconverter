"""Pure, conservative matching within ONE identified source document.

Public API: SourceRow is immutable cent-exact extracted input; ExistingRow adds
ledger identity and an opaque user-metadata signature. MatchPlan reports row
indices/transaction IDs, never deletes or merges occurrences. Document identity
is account + slash/case-normalised StatementsPDF-relative name, with basename
compatibility for unlinked legacy paths. Account identifiers are preserved literally.
"""
from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json


def document_identifier(value: str, *, statements_root=None) -> str:
    """Canonicalise a raw path, never an already canonical registry/alias key.

    Configured containment takes precedence over directory-name heuristics.
    For relative or moved-root legacy paths, the first StatementsPDF component
    is the boundary; all later components remain part of identity. Without a
    statement-root marker, retain historic basename compatibility. Existing
    linked identity is resolved separately from registry keys/aliases/content,
    never inferred from raw transaction paths or rewritten by this helper.
    """
    path = value.replace('\\', '/')
    root = str(statements_root).replace('\\', '/').rstrip('/') if statements_root is not None else None
    parts = path.split('/')
    markers = [i for i, p in enumerate(parts) if p.casefold() == 'statementspdf']
    if root and path.casefold().startswith(root.casefold() + '/'):
        # The configured root, not a same-named ancestor or nested folder,
        # determines where document identity begins.
        name = path[len(root) + 1:]
    else:
        name = '/'.join(parts[markers[0] + 1:]) if markers else parts[-1]
    if not name or any(p in ('', '.', '..') for p in name.split('/')):
        raise ValueError('Invalid document identifier')
    return name.casefold()


def source_identity(account_id: str, document_key: str) -> str:
    return hashlib.sha256(json.dumps([account_id, document_key], ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class SourceRow:
    txn_date: str
    description_raw: str
    cents: int
    currency: str
    page: int
    row_ref: str
    channel: str | None = None
    merchant_core: str | None = None
    category: str = ''
    subcategory: str = ''
    exception_type: str | None = None

    @property
    def key(self):
        return self.txn_date, self.cents, self.description_raw, self.currency


@dataclass(frozen=True)
class ExistingRow:
    txn_id: int
    source: SourceRow
    metadata_signature: str = ''


@dataclass(frozen=True)
class MatchPlan:
    matches: tuple[tuple[int, int], ...] = ()
    additions: tuple[int, ...] = ()
    potential_removals: tuple[int, ...] = ()
    ambiguous: tuple[int, ...] = ()
    changed_candidates: tuple[int, ...] = ()


def match_source_rows(incoming: tuple[SourceRow, ...], existing: tuple[ExistingRow, ...]) -> MatchPlan:
    new_groups, old_groups = defaultdict(list), defaultdict(list)
    for i, row in enumerate(incoming):
        new_groups[row.key].append(i)
    for row in existing:
        old_groups[row.source.key].append(row)
    matches, additions, removals, ambiguous = [], [], [], []
    for key in new_groups.keys() | old_groups.keys():
        indices, old = new_groups[key], old_groups[key]
        if not old:
            additions.extend(indices)
            continue
        if not indices:
            removals.extend(r.txn_id for r in old)
            continue
        refs = [incoming[i].row_ref for i in indices]
        old_refs = [r.source.row_ref for r in old]
        unique_refs = (len(set(refs)) == len(refs) and len(set(old_refs)) == len(old_refs))
        # Assess the ENTIRE group before assigning even shared refs: after a
        # shift a shared ref can refer to a different physical occurrence. The
        # signature includes meaningful transaction/exception/audit decisions.
        # Equal decision state permits deterministic fallback/order changes;
        # occurrence IDs and creation times alone are not differing decisions.
        if (len(old) > 1 and len({r.metadata_signature for r in old}) > 1
                and (not unique_refs or set(refs) != set(old_refs))):
            ambiguous.extend(indices)
            removals.extend(r.txn_id for r in old)
            continue
        old_by_ref = {r.source.row_ref: r for r in old} if unique_refs else {}
        remaining = list(old)
        unmatched = []
        for i in indices:
            row = old_by_ref.get(incoming[i].row_ref)
            if row is not None:
                matches.append((i, row.txn_id))
                remaining.remove(row)
            else:
                unmatched.append(i)
        for i, row in zip(unmatched, sorted(remaining, key=lambda r: (r.source.row_ref, r.txn_id))):
            matches.append((i, row.txn_id))
        count = min(len(unmatched), len(remaining))
        additions.extend(unmatched[count:])
        removals.extend(r.txn_id for r in remaining[count:])
    # A replacement with missing old rows and new rows might be changed source
    # fields, not legitimate additions. Never guess/overwrite financial fields.
    changed = tuple(sorted(additions)) if removals and additions else ()
    return MatchPlan(tuple(sorted(matches)), tuple(sorted(additions)), tuple(sorted(removals)),
                     tuple(sorted(ambiguous)), changed)
