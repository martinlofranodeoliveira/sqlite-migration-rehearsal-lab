"""Strict bounded manifest parsing. Diagnostics never reproduce supplied values."""
from __future__ import annotations

import json
import os
import re

from sqlite_migration_rehearsal.errors import (
    ErrorCode, ManifestValidationError, RehearsalPhase, RehearsalError,
    ResourceLimitError, SanitizedDiagnostic,
)
from sqlite_migration_rehearsal.models import (
    InvariantSpec, MigrationManifest, RehearsalLimits, SUPPORTED_SCHEMA_VERSION, TypedValue,
)
from sqlite_migration_rehearsal.sql_guard import validate_invariant_query, validate_migration_statement
from sqlite_migration_rehearsal.workspace import regular_reader

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", re.ASCII)
KEYS = {"schema_version", "migration_id", "description", "up_statements", "down_statements",
        "baseline_invariants", "post_up_invariants", "post_down_invariants"}
PHASE = RehearsalPhase.MANIFEST_VALIDATION


def _bad():
    return ManifestValidationError(SanitizedDiagnostic(
        ErrorCode.MANIFEST_VALIDATION_ERROR, PHASE, "Manifest does not satisfy the 0.1.0 contract."
    ))


def _limit():
    return ResourceLimitError(SanitizedDiagnostic(
        ErrorCode.RESOURCE_LIMIT_EXCEEDED, PHASE, "Manifest resource limit exceeded."
    ))


def _check_tree(value):
    # Inspect iteratively, before any recursive JSON serialization. Count keys as well as values.
    stack = [(value, 0)]
    count = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if depth > 12 or count > 4096:
            raise _limit()
        if isinstance(item, dict):
            if len(item) > 4096 or any(not isinstance(k, str) for k in item):
                raise _bad()
            stack.extend((k, depth + 1) for k in item)
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            if len(item) > 4096:
                raise _limit()
            stack.extend((v, depth + 1) for v in item)
        elif item is not None and type(item) not in (str, int, bool):
            raise _bad()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _bad()
        result[key] = value
    return result


def _no_constant(value):
    raise _bad()


def load_manifest_file(path: str | os.PathLike[str], *, limits=None):
    active = limits or RehearsalLimits()
    try:
        with regular_reader(path) as stream:
            if os.fstat(stream.fileno()).st_size > active.max_manifest_bytes:
                raise _limit()
            raw = stream.read(active.max_manifest_bytes + 1)
    except (OSError, ValueError, TypeError, RehearsalError) as exc:
        if isinstance(exc, ResourceLimitError):
            raise
        raise _bad() from None
    return parse_manifest_bytes(raw, limits=active)


def parse_manifest_bytes(raw_bytes: bytes, *, limits=None):
    active = limits or RehearsalLimits()
    if not isinstance(raw_bytes, bytes):
        raise _bad()
    if len(raw_bytes) > active.max_manifest_bytes:
        raise _limit()
    # Bound JSON nesting before the recursive standard-library decoder sees it.
    quoted = escaped = False
    depth = 0
    for byte in raw_bytes:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > 12:
                raise _limit()
        elif byte in (93, 125):
            depth -= 1
    try:
        payload = json.loads(raw_bytes.decode("utf-8"), object_pairs_hook=_pairs,
                             parse_constant=_no_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise _bad() from None
    return parse_manifest_dict(payload, limits=active)


def _description(value, maximum):
    if not isinstance(value, str) or len(value) > maximum:
        raise _bad()
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise _bad() from None
    return value


def parse_manifest_dict(payload, *, limits=None):
    active = limits or RehearsalLimits()
    _check_tree(payload)
    try:
        encoded_size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise _bad() from None
    if encoded_size > active.max_manifest_bytes:
        raise _limit()
    if not isinstance(payload, dict) or set(payload) - KEYS:
        raise _bad()
    version = payload.get("schema_version", SUPPORTED_SCHEMA_VERSION)
    migration_id = payload.get("migration_id")
    if version != SUPPORTED_SCHEMA_VERSION or not isinstance(migration_id, str) or not IDENTIFIER.fullmatch(migration_id):
        raise _bad()
    description = _description(payload.get("description", ""), 1024)

    def statements(field, phase):
        values = payload.get(field)
        if not isinstance(values, list) or not values:
            raise _bad()
        if len(values) > active.max_statements:
            raise _limit()
        for index, sql in enumerate(values):
            validate_migration_statement(sql, max_statement_bytes=active.max_statement_bytes,
                                         phase=phase, statement_index=index)
        return tuple(values)

    up = statements("up_statements", RehearsalPhase.UP_MIGRATION)
    down = statements("down_statements", RehearsalPhase.DOWN_MIGRATION)
    seen = set()

    def invariants(field):
        values = payload.get(field, [])
        if not isinstance(values, list):
            raise _bad()
        if len(values) > active.max_invariants:
            raise _limit()
        result = []
        for item in values:
            if not isinstance(item, dict) or set(item) - {"id", "description", "query", "expected"}:
                raise _bad()
            inv_id = item.get("id")
            if not isinstance(inv_id, str) or not IDENTIFIER.fullmatch(inv_id) or inv_id in seen:
                raise _bad()
            seen.add(inv_id)
            desc = _description(item.get("description", ""), 512)
            query = item.get("query")
            validate_invariant_query(query, max_statement_bytes=active.max_statement_bytes,
                                     phase=PHASE, invariant_id=inv_id)
            expected = item.get("expected")
            if not isinstance(expected, dict) or set(expected) != {"type", "value"}:
                raise _bad()
            try:
                value = TypedValue(expected["type"], expected["value"])
            except (ValueError, TypeError):
                raise _bad() from None
            if value.storage_type == "text" and len(value.value.encode("utf-8")) > active.max_cell_bytes:
                raise _limit()
            if value.storage_type == "blob" and len(value.value) // 2 > active.max_cell_bytes:
                raise _limit()
            result.append(InvariantSpec(inv_id, query, value, desc))
        return tuple(result)

    baseline = invariants("baseline_invariants")
    post_up = invariants("post_up_invariants")
    post_down = invariants("post_down_invariants")
    return MigrationManifest(version, migration_id, description, up, down, post_up, baseline, post_down)


def revalidate_manifest(manifest, *, limits):
    """Dataclass construction is not a validation bypass at the engine boundary."""
    if not isinstance(manifest, MigrationManifest):
        raise _bad()
    try:
        payload = {"schema_version": manifest.schema_version, "migration_id": manifest.migration_id,
                   "description": manifest.description, "up_statements": list(manifest.up_statements),
                   "down_statements": list(manifest.down_statements)}
        for field in ("baseline_invariants", "post_up_invariants", "post_down_invariants"):
            payload[field] = [{"id": i.invariant_id, "query": i.query,
                               "expected": i.expected.to_dict(), "description": i.description}
                              for i in getattr(manifest, field)]
    except (AttributeError, TypeError):
        raise _bad() from None
    return parse_manifest_dict(payload, limits=limits)
