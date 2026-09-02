"""Deterministic proof-item parsing and fail-closed lazy context building.

Paper-style blueprints use one level-one heading per proof item::

    # lemma lem:foo

    <!-- rethlas-depends-on: lem:bar, proposition prop:baz -->
    ## statement
    ...
    ## proof
    ...

The dependency comment is optional, must be a single line between the item's
level-one heading and ``## statement``, and may name an earlier or later item
by its complete level-one title or its final label token.  An empty comment
declares no dependencies.  For backwards compatibility, an item without the
comment depends on the compact frontier of the preceding-item DAG.  That
frontier's transitive closure contains every preceding item, preserving
conservative prefix semantics without emitting a quadratic number of edges.

Completely unstructured legacy proof text is represented as one synthetic main
item when ``target_statement`` is supplied. Its historical raw target semantics
are preserved so existing publication receipts remain replayable. Once a
level-one item heading (or an orphan ``## statement``/``## proof`` heading) is
present, malformed input is rejected rather than silently falling back.

Problem files may wrap the mathematical target in one initial level-one title
and one trailing ``## Retrieval restriction`` control section.  Those two
presentation/control regions are excluded deterministically from proof-item
matching, while the caller remains responsible for binding the exact raw problem
bytes at the publication boundary.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Sequence


PROOF_ITEM_SCHEMA_VERSION = 1
PROOF_CONTEXT_SCHEMA_VERSION = 2
AGGREGATE_CONTEXT_SCHEMA_VERSION = 1
ADAPTIVE_AGGREGATE_CONTEXT_SCHEMA_VERSION = 2

_ATX_HEADING_RE = re.compile(
    r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+[ \t]*)?$"
)
_FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
_DEPENDS_ON_RE = re.compile(
    r"^[ \t]*<!--[ \t]*rethlas-depends-on[ \t]*:[ \t]*(.*?)[ \t]*-->[ \t]*$",
    re.IGNORECASE,
)
_DEPENDS_ON_MARKER_RE = re.compile(r"rethlas-depends-on", re.IGNORECASE)


class ProofContextError(ValueError):
    """Base class for deterministic proof-context failures."""


class ProofParseError(ProofContextError):
    """Raised when a blueprint violates the paper-like markdown contract."""


class ProofDependencyError(ProofParseError):
    """Raised when proof-item dependency metadata is invalid."""


@dataclass(frozen=True, slots=True)
class ProofItem:
    """One content-addressed statement/proof item in a parsed blueprint.

    For ``dependency_mode == "conservative-prefix"``, ``depends_on`` is a
    compact frontier encoding whose closure covers the complete textual prefix;
    those direct edges are not claims about which earlier results were cited.
    """

    index: int
    item_id: str
    digest: str
    title: str
    label: str
    statement: str
    proof: str
    depends_on: tuple[str, ...]
    dependency_mode: Literal["explicit", "conservative-prefix", "synthetic"]


@dataclass(frozen=True, slots=True)
class ProofManifest:
    """Validated proof items and their deterministic topological ordering."""

    proof_digest: str
    items: tuple[ProofItem, ...]
    topological_item_ids: tuple[str, ...]
    source_kind: Literal["structured", "synthetic"]

    @property
    def item_ids(self) -> tuple[str, ...]:
        return tuple(item.item_id for item in self.items)

    def get_item(self, item_id: str) -> ProofItem:
        for item in self.items:
            if item.item_id == item_id:
                return item
        raise KeyError(item_id)


@dataclass(frozen=True, slots=True)
class _RawItem:
    index: int
    title: str
    label: str
    statement: str
    proof: str
    dependency_references: tuple[str, ...] | None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def proof_digest(proof: str) -> str:
    """Return the exact UTF-8 SHA-256 digest of a complete proof payload."""

    if not isinstance(proof, str):
        raise TypeError("proof must be a string")
    return hashlib.sha256(proof.encode("utf-8")).hexdigest()


def aggregate_context_digest(manifest: ProofManifest) -> str:
    """Attest the complete item manifest independently of transport budgets."""

    if not isinstance(manifest, ProofManifest):
        raise TypeError("manifest must be a ProofManifest")
    return _sha256_json(
        {
            "schema_version": AGGREGATE_CONTEXT_SCHEMA_VERSION,
            "source_kind": manifest.source_kind,
            "proof_digest": manifest.proof_digest,
            "items": [
                {
                    "item_id": item.item_id,
                    "digest": item.digest,
                    "depends_on": list(item.depends_on),
                }
                for item in manifest.items
            ],
        }
    )


def aggregate_adaptive_context_digest(
    manifest: ProofManifest,
    item_context_attestations: Sequence[dict[str, Any]],
) -> str:
    """Attest the manifest and every item's final adaptive context.

    The caller must first rebuild and validate each item context.  This helper
    deliberately hashes only canonical, transportable attestation fields so a
    generation client can recompute the value independently before publishing.
    """

    if not isinstance(manifest, ProofManifest):
        raise TypeError("manifest must be a ProofManifest")
    if isinstance(item_context_attestations, (str, bytes)) or not isinstance(
        item_context_attestations, Sequence
    ):
        raise TypeError("item_context_attestations must be a sequence")
    normalized: list[dict[str, Any]] = []
    for index, record in enumerate(item_context_attestations):
        if not isinstance(record, dict):
            raise TypeError(
                f"item_context_attestations[{index}] must be an object"
            )
        normalized.append(
            {
                "item_id": record.get("item_id"),
                "disposition": record.get("disposition"),
                "final_round": record.get("final_round"),
                "expanded_proof_ids": record.get("expanded_proof_ids"),
                "max_chars": record.get("max_chars"),
                "context_digest": record.get("context_digest"),
                "verdict": record.get("verdict"),
            }
        )
    return _sha256_json(
        {
            "schema_version": ADAPTIVE_AGGREGATE_CONTEXT_SCHEMA_VERSION,
            "domain": "rethlas-adaptive-aggregate",
            "manifest_digest": aggregate_context_digest(manifest),
            "item_context_attestations": normalized,
        }
    )


def _normalise_alias(value: str) -> str:
    return " ".join(value.split()).casefold()


def _heading(line: str) -> tuple[int, str] | None:
    match = _ATX_HEADING_RE.match(line)
    if match is None:
        return None
    return len(match.group(1)), match.group(2).strip()


def _outside_fence(lines: list[str]) -> list[bool]:
    """Mark lines that are outside CommonMark-style fenced code blocks."""

    result: list[bool] = []
    fence_character: str | None = None
    fence_length = 0

    for line in lines:
        if fence_character is None:
            opener = _FENCE_OPEN_RE.match(line)
            if opener is None:
                result.append(True)
                continue
            marker = opener.group(1)
            fence_character = marker[0]
            fence_length = len(marker)
            result.append(False)
            continue

        result.append(False)
        stripped = line.lstrip(" \t")
        if len(line) - len(stripped) > 3:
            continue
        marker_length = len(stripped) - len(stripped.lstrip(fence_character))
        remainder = stripped[marker_length:]
        if marker_length >= fence_length and not remainder.strip():
            fence_character = None
            fence_length = 0

    return result


def _nonblank(lines: Iterable[str]) -> bool:
    return any(line.strip() for line in lines)


def _trim_blank_lines(lines: Iterable[str]) -> str:
    """Remove boundary blank lines without altering mathematical text."""

    material = list(lines)
    start = 0
    end = len(material)
    while start < end and not material[start].strip():
        start += 1
    while end > start and not material[end - 1].strip():
        end -= 1
    return "\n".join(material[start:end])


def extract_verification_target(problem_markdown: str) -> str:
    """Extract the mathematical target from a trusted problem document.

    Only two repository-level wrappers are recognized: an initial outside-fence
    H1 title, and an outside-fence ``## Retrieval restriction`` section together
    with everything following it. Other headings and all fenced content remain
    part of the target. Boundary blank lines are normalized exactly as they were
    by the historical target comparison.
    """

    if not isinstance(problem_markdown, str):
        raise TypeError("problem_markdown must be a string")
    if not problem_markdown.strip():
        raise ProofParseError("target_statement must contain non-whitespace text")

    lines = problem_markdown.splitlines()
    outside_fence = _outside_fence(lines)
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start < len(lines) and outside_fence[start]:
        initial_heading = _heading(lines[start])
        if initial_heading is not None and initial_heading[0] == 1:
            start += 1

    end = len(lines)
    for position in range(start, len(lines)):
        if not outside_fence[position]:
            continue
        section_heading = _heading(lines[position])
        if (
            section_heading is not None
            and section_heading[0] == 2
            and section_heading[1].casefold() == "retrieval restriction"
        ):
            end = position
            break

    target = _trim_blank_lines(lines[start:end])
    if not target:
        raise ProofParseError(
            "target_statement has no mathematical content after removing wrappers"
        )
    return target


def _parse_dependency_comment(
    lines: list[str],
    *,
    item_title: str,
) -> tuple[str, ...] | None:
    matches: list[tuple[int, re.Match[str]]] = []
    for offset, line in enumerate(lines):
        match = _DEPENDS_ON_RE.match(line)
        if match is not None:
            matches.append((offset, match))
        elif _DEPENDS_ON_MARKER_RE.search(line):
            raise ProofDependencyError(
                f"malformed rethlas-depends-on metadata in item '{item_title}'"
            )

    if len(matches) > 1:
        raise ProofDependencyError(
            f"duplicate rethlas-depends-on metadata in item '{item_title}'"
        )
    if not matches:
        if _nonblank(lines):
            raise ProofParseError(
                f"unexpected content before ## statement in item '{item_title}'"
            )
        return None

    match_offset, match = matches[0]
    for offset, line in enumerate(lines):
        if offset != match_offset and line.strip():
            raise ProofParseError(
                f"unexpected content before ## statement in item '{item_title}'"
            )

    raw_references = match.group(1).strip()
    if not raw_references:
        return ()

    references = tuple(part.strip() for part in raw_references.split(","))
    if any(not reference for reference in references):
        raise ProofDependencyError(
            f"empty dependency reference in item '{item_title}'"
        )
    normalised = [_normalise_alias(reference) for reference in references]
    if len(set(normalised)) != len(normalised):
        raise ProofDependencyError(
            f"duplicate dependency reference in item '{item_title}'"
        )
    return references


def _canonical_final_statement_region(
    lines: list[str],
    outside_fence: list[bool],
    canonical_target: str,
) -> tuple[int, int] | None:
    """Locate an exact canonical target between final-item control headings.

    The target is trusted problem text, not proof-item markup.  It may itself
    contain level-one or level-two headings (including headings literally named
    ``statement`` or ``proof``), so an exact match must be identified before
    those lines are interpreted structurally.  A matching region is eligible
    only when an item heading precedes it and no item heading follows its
    closing control heading.  Ambiguity fails closed.
    """

    statement_positions: list[int] = []
    proof_positions: list[int] = []
    h1_positions: list[int] = []
    for position, line in enumerate(lines):
        if not outside_fence[position]:
            continue
        heading = _heading(line)
        if heading is None:
            continue
        level, title = heading
        if level == 1:
            h1_positions.append(position)
        elif level == 2 and title.casefold() == "statement":
            statement_positions.append(position)
        elif level == 2 and title.casefold() == "proof":
            proof_positions.append(position)

    candidates: list[tuple[int, int]] = []
    for statement_position in statement_positions:
        if not any(position < statement_position for position in h1_positions):
            continue
        for proof_position in proof_positions:
            if proof_position <= statement_position:
                continue
            if _trim_blank_lines(
                lines[statement_position + 1 : proof_position]
            ) != canonical_target:
                continue
            if any(position > proof_position for position in h1_positions):
                continue
            candidates.append((statement_position, proof_position))

    if len(candidates) > 1:
        raise ProofParseError(
            "canonical target matches more than one possible final ## statement "
            "region"
        )
    return candidates[0] if candidates else None


def _parse_structured_items(
    lines: list[str],
    outside_fence: list[bool],
    h1_positions: list[int],
) -> list[_RawItem]:
    if _nonblank(lines[: h1_positions[0]]):
        raise ProofParseError("unexpected content before the first proof item")

    raw_items: list[_RawItem] = []
    for index, start in enumerate(h1_positions):
        end = h1_positions[index + 1] if index + 1 < len(h1_positions) else len(lines)
        heading = _heading(lines[start])
        assert heading is not None and heading[0] == 1
        title = heading[1]
        if not title:
            raise ProofParseError(f"proof item {index + 1} has an empty title")
        label = title.split()[-1]

        section_positions: dict[str, list[int]] = {"statement": [], "proof": []}
        proof_subheadings: list[tuple[int, str]] = []
        for position in range(start + 1, end):
            if not outside_fence[position]:
                continue
            section_heading = _heading(lines[position])
            if section_heading is None or section_heading[0] != 2:
                continue
            section_name = section_heading[1].casefold()
            if section_name in section_positions:
                section_positions[section_name].append(position)
            else:
                proof_subheadings.append((position, section_heading[1]))

        for section_name in ("statement", "proof"):
            count = len(section_positions[section_name])
            if count != 1:
                raise ProofParseError(
                    f"item '{title}' must contain exactly one ## {section_name} section; "
                    f"found {count}"
                )

        statement_position = section_positions["statement"][0]
        proof_position = section_positions["proof"][0]
        if statement_position >= proof_position:
            raise ProofParseError(
                f"## statement must precede ## proof in item '{title}'"
            )
        for position, subheading in proof_subheadings:
            if position < proof_position:
                raise ProofParseError(
                    f"unexpected level-two section '## {subheading}' "
                    f"before ## proof in item '{title}'"
                )

        for position in range(statement_position, end):
            if outside_fence[position] and _DEPENDS_ON_MARKER_RE.search(lines[position]):
                raise ProofDependencyError(
                    "rethlas-depends-on metadata must appear before "
                    f"## statement in item '{title}'"
                )

        dependency_references = _parse_dependency_comment(
            lines[start + 1 : statement_position], item_title=title
        )
        statement = _trim_blank_lines(lines[statement_position + 1 : proof_position])
        proof = _trim_blank_lines(lines[proof_position + 1 : end])
        if not statement:
            raise ProofParseError(f"item '{title}' has an empty ## statement section")
        if not proof:
            raise ProofParseError(f"item '{title}' has an empty ## proof section")

        raw_items.append(
            _RawItem(
                index=index,
                title=title,
                label=label,
                statement=statement,
                proof=proof,
                dependency_references=dependency_references,
            )
        )

    return raw_items


def _validate_unique_names(raw_items: list[_RawItem]) -> dict[str, set[int]]:
    title_owners: dict[str, int] = {}
    label_owners: dict[str, int] = {}
    aliases: dict[str, set[int]] = {}

    for item in raw_items:
        title_key = _normalise_alias(item.title)
        label_key = _normalise_alias(item.label)
        if title_key in title_owners:
            raise ProofParseError(f"duplicate proof-item title '{item.title}'")
        if label_key in label_owners:
            raise ProofParseError(f"duplicate proof-item label '{item.label}'")
        title_owners[title_key] = item.index
        label_owners[label_key] = item.index
        aliases.setdefault(title_key, set()).add(item.index)
        aliases.setdefault(label_key, set()).add(item.index)

    return aliases


def _resolve_dependencies(raw_items: list[_RawItem]) -> list[tuple[int, ...]]:
    aliases = _validate_unique_names(raw_items)
    declared: list[tuple[int, ...] | None] = []

    for item in raw_items:
        if item.dependency_references is None:
            declared.append(None)
            continue

        dependency_indices: list[int] = []
        for reference in item.dependency_references:
            candidates = aliases.get(_normalise_alias(reference), set())
            if not candidates:
                raise ProofDependencyError(
                    f"unknown dependency '{reference}' in item '{item.title}'"
                )
            if len(candidates) != 1:
                raise ProofDependencyError(
                    f"ambiguous dependency '{reference}' in item '{item.title}'"
                )
            dependency_index = next(iter(candidates))
            if dependency_index == item.index:
                raise ProofDependencyError(
                    f"self-dependency in item '{item.title}' via '{reference}'"
                )
            dependency_indices.append(dependency_index)

        if len(set(dependency_indices)) != len(dependency_indices):
            raise ProofDependencyError(
                f"duplicate resolved dependency in item '{item.title}'"
            )
        declared.append(tuple(sorted(dependency_indices)))

    # A dependency from an earlier item to a later one becomes an edge inside
    # the induced prefix only when the later node is reached.  Remember those
    # inbound edges so the incremental frontier remains exact with forward
    # references as well as ordinary backward references.
    inbound_from_earlier = [False] * len(raw_items)
    for item_index, item_dependencies in enumerate(declared):
        if item_dependencies is None:
            continue
        for dependency_index in item_dependencies:
            if dependency_index > item_index:
                inbound_from_earlier[dependency_index] = True

    resolved: list[tuple[int, ...]] = []
    prefix_frontier: set[int] = set()
    for item in raw_items:
        item_dependencies = declared[item.index]
        if item_dependencies is None:
            # The maximal nodes of the preceding induced DAG form the smallest
            # deterministic frontier whose ancestor closure covers that whole
            # prefix.  Pure legacy documents therefore become a linear chain;
            # mixed explicit branches retain one edge per uncovered frontier.
            item_dependencies = tuple(sorted(prefix_frontier))
        resolved.append(item_dependencies)

        prefix_frontier.add(item.index)
        if inbound_from_earlier[item.index]:
            prefix_frontier.discard(item.index)
        for dependency_index in item_dependencies:
            if dependency_index < item.index:
                prefix_frontier.discard(dependency_index)

    return resolved


def _topological_order(dependencies: list[tuple[int, ...]]) -> tuple[int, ...]:
    indegrees = [len(item_dependencies) for item_dependencies in dependencies]
    dependents: list[list[int]] = [[] for _ in dependencies]
    for item_index, item_dependencies in enumerate(dependencies):
        for dependency_index in item_dependencies:
            dependents[dependency_index].append(item_index)

    ready = [index for index, indegree in enumerate(indegrees) if indegree == 0]
    heapq.heapify(ready)
    ordered: list[int] = []
    while ready:
        item_index = heapq.heappop(ready)
        ordered.append(item_index)
        for dependent_index in dependents[item_index]:
            indegrees[dependent_index] -= 1
            if indegrees[dependent_index] == 0:
                heapq.heappush(ready, dependent_index)

    if len(ordered) != len(dependencies):
        cyclic = [str(index + 1) for index, degree in enumerate(indegrees) if degree]
        raise ProofDependencyError(
            "dependency cycle detected involving proof item positions "
            + ", ".join(cyclic)
        )
    return tuple(ordered)


def _build_manifest(
    raw_items: list[_RawItem],
    *,
    complete_proof_digest: str,
    source_kind: Literal["structured", "synthetic"],
) -> ProofManifest:
    dependencies = _resolve_dependencies(raw_items)
    topological_indices = _topological_order(dependencies)
    full_digests: list[str | None] = [None] * len(raw_items)
    item_ids: list[str | None] = [None] * len(raw_items)

    for item_index in topological_indices:
        item = raw_items[item_index]
        dependency_digests = [full_digests[index] for index in dependencies[item_index]]
        assert all(digest is not None for digest in dependency_digests)
        digest = _sha256_json(
            {
                "schema_version": PROOF_ITEM_SCHEMA_VERSION,
                "title": item.title,
                "label": item.label,
                "statement": item.statement,
                "proof": item.proof,
                "dependency_digests": dependency_digests,
            }
        )
        full_digests[item_index] = digest
        item_ids[item_index] = f"pi_{digest[:24]}"

    items: list[ProofItem] = []
    for item in raw_items:
        digest = full_digests[item.index]
        item_id = item_ids[item.index]
        assert digest is not None and item_id is not None
        resolved_ids = tuple(item_ids[index] for index in dependencies[item.index])
        assert all(resolved_id is not None for resolved_id in resolved_ids)
        if source_kind == "synthetic":
            dependency_mode: Literal[
                "explicit", "conservative-prefix", "synthetic"
            ] = "synthetic"
        elif item.dependency_references is None:
            dependency_mode = "conservative-prefix"
        else:
            dependency_mode = "explicit"
        items.append(
            ProofItem(
                index=item.index,
                item_id=item_id,
                digest=digest,
                title=item.title,
                label=item.label,
                statement=item.statement,
                proof=item.proof,
                depends_on=tuple(resolved_id for resolved_id in resolved_ids if resolved_id),
                dependency_mode=dependency_mode,
            )
        )

    topological_item_ids = tuple(
        item_ids[index] for index in topological_indices if item_ids[index] is not None
    )
    return ProofManifest(
        proof_digest=complete_proof_digest,
        items=tuple(items),
        topological_item_ids=topological_item_ids,
        source_kind=source_kind,
    )


def parse_blueprint(
    proof: str,
    *,
    target_statement: str | None = None,
) -> ProofManifest:
    """Parse and validate a complete proof blueprint.

    Args:
        proof: Raw markdown proof text.
        target_statement: Required for a completely unstructured legacy proof,
            which becomes one synthetic main item.  When supplied for a
            structured blueprint, it must exactly equal the final item statement.

    Raises:
        ProofParseError: If structured markdown is malformed or unstructured
            input lacks a target statement.
        ProofDependencyError: If dependency metadata is invalid.
    """

    if not isinstance(proof, str):
        raise TypeError("proof must be a string")
    if not proof.strip():
        raise ProofParseError("proof must be non-empty")

    lines = proof.splitlines()
    outside_fence = _outside_fence(lines)
    raw_headings = [
        (position, parsed)
        for position, line in enumerate(lines)
        if outside_fence[position] and (parsed := _heading(line)) is not None
    ]
    raw_h1_positions = [
        position for position, (level, _) in raw_headings if level == 1
    ]
    paper_h2_positions = [
        position
        for position, (level, title) in raw_headings
        if level == 2 and title.casefold() in {"statement", "proof"}
    ]
    complete_proof_digest = proof_digest(proof)

    if not raw_h1_positions:
        if paper_h2_positions:
            raise ProofParseError(
                "paper-style ## statement/## proof section found without a level-one item"
            )
        if not isinstance(target_statement, str) or not target_statement.strip():
            raise ProofParseError(
                "target_statement is required for an unstructured legacy proof"
            )
        raw_items = [
            _RawItem(
                index=0,
                title="theorem synthetic:main",
                label="synthetic:main",
                statement=_trim_blank_lines(target_statement.splitlines()),
                proof=_trim_blank_lines(lines),
                dependency_references=(),
            )
        ]
        return _build_manifest(
            raw_items,
            complete_proof_digest=complete_proof_digest,
            source_kind="synthetic",
        )

    canonical_target: str | None = None
    structural_outside = outside_fence
    if target_statement is not None:
        if not isinstance(target_statement, str) or not target_statement.strip():
            raise ProofParseError("target_statement must contain non-whitespace text")
        canonical_target = extract_verification_target(target_statement)
        target_region = _canonical_final_statement_region(
            lines, outside_fence, canonical_target
        )
        if target_region is not None:
            statement_position, proof_position = target_region
            structural_outside = list(outside_fence)
            for position in range(statement_position + 1, proof_position):
                structural_outside[position] = False

    headings = [
        (position, parsed)
        for position, line in enumerate(lines)
        if structural_outside[position] and (parsed := _heading(line)) is not None
    ]
    h1_positions = [position for position, (level, _) in headings if level == 1]
    raw_items = _parse_structured_items(lines, structural_outside, h1_positions)
    manifest = _build_manifest(
        raw_items,
        complete_proof_digest=complete_proof_digest,
        source_kind="structured",
    )
    if target_statement is not None:
        assert canonical_target is not None
        if manifest.items[-1].statement != canonical_target:
            raise ProofParseError(
                "the final proof-item statement must exactly match target_statement"
            )
    return manifest


def _current_record(item: ProofItem) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "digest": item.digest,
        "title": item.title,
        "label": item.label,
        "statement": item.statement,
        "proof": item.proof,
        "depends_on": list(item.depends_on),
        "dependency_mode": item.dependency_mode,
    }


def _premise_record(item: ProofItem) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "digest": item.digest,
        "title": item.title,
        "label": item.label,
        "statement": item.statement,
        "depends_on": list(item.depends_on),
        "dependency_mode": item.dependency_mode,
    }


def _expanded_proof_record(item: ProofItem) -> dict[str, Any]:
    return {
        **_premise_record(item),
        "proof": item.proof,
    }


def _context_envelope(
    *,
    manifest: ProofManifest,
    requested_item_id: str,
    current_item: dict[str, Any] | None,
    premises: list[dict[str, Any]],
    expanded_proofs: list[dict[str, Any]],
    complete: bool,
    truncated: bool,
    missing: list[str],
    omitted: list[str],
    characters_used: int,
    expanded_proof_characters: int,
    max_chars: int | None,
    round_index: int,
    strict_ancestor_item_ids: list[str],
    expanded_proof_ids: list[str],
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "schema_version": PROOF_CONTEXT_SCHEMA_VERSION,
        "proof_digest": manifest.proof_digest,
        "requested_item_id": requested_item_id,
        "current_item": current_item,
        "premises": premises,
        "expanded_proofs": expanded_proofs,
        "scope": {
            "current_item_id": requested_item_id,
            "strict_ancestor_item_ids": strict_ancestor_item_ids,
        },
        "round": round_index,
        "expanded_proof_ids": expanded_proof_ids,
        "complete": complete,
        "truncated": truncated,
        "missing": missing,
        "omitted": omitted,
        "characters_used": characters_used,
        "expanded_proof_characters": expanded_proof_characters,
        "max_chars": max_chars,
        "character_accounting": (
            "sum of complete canonical JSON proof-item record characters; "
            "expanded_proof_characters counts each complete expanded proof record"
        ),
    }
    envelope["digest"] = _sha256_json(envelope)
    return envelope


def build_item_context(
    manifest: ProofManifest,
    item_id: str,
    *,
    max_chars: int | None = None,
    expanded_proof_ids: Sequence[str] = (),
    round_index: int = 0,
) -> dict[str, Any]:
    """Build a deterministic, proof-body-lazy context for one proof item.

    The current item record is attempted first and contains its complete proof.
    Ancestor statement/edge records follow in stable topological order and
    never contain proof bodies. ``expanded_proofs`` is a separate list of
    complete records whose exact ids appear in ``expanded_proof_ids``; it is
    empty in round zero. ``max_chars`` counts complete canonical JSON records.
    No record is sliced: a record that does not fit, and every later record, is
    listed in ``omitted`` and makes the envelope explicitly incomplete.

    An unknown requested item produces a fail-closed envelope with the id in
    ``missing`` rather than raising.  Invalid manifests cannot be constructed by
    :func:`parse_blueprint`.
    """

    if not isinstance(manifest, ProofManifest):
        raise TypeError("manifest must be a ProofManifest")
    if not isinstance(item_id, str) or not item_id:
        raise TypeError("item_id must be a non-empty string")
    if max_chars is not None:
        if isinstance(max_chars, bool) or not isinstance(max_chars, int):
            raise TypeError("max_chars must be an integer or None")
        if max_chars < 0:
            raise ValueError("max_chars must be >= 0")
    if isinstance(round_index, bool) or not isinstance(round_index, int):
        raise TypeError("round_index must be an integer")
    if round_index < 0:
        raise ValueError("round_index must be >= 0")
    if isinstance(expanded_proof_ids, (str, bytes)) or not isinstance(
        expanded_proof_ids, Sequence
    ):
        raise TypeError("expanded_proof_ids must be a sequence of strings")
    requested_expansions = list(expanded_proof_ids)
    if any(not isinstance(value, str) or not value for value in requested_expansions):
        raise TypeError("expanded_proof_ids must contain non-empty strings")
    if len(set(requested_expansions)) != len(requested_expansions):
        raise ProofContextError("expanded_proof_ids contains duplicate ids")
    if round_index == 0 and requested_expansions:
        raise ProofContextError("round zero must not contain expanded proofs")
    if round_index > 0 and not requested_expansions:
        raise ProofContextError("positive adaptive rounds require expanded proofs")
    if round_index > len(requested_expansions):
        raise ProofContextError(
            "adaptive round count cannot exceed the number of expanded proofs"
        )

    items_by_id = {item.item_id: item for item in manifest.items}
    requested = items_by_id.get(item_id)
    if requested is None:
        return _context_envelope(
            manifest=manifest,
            requested_item_id=item_id,
            current_item=None,
            premises=[],
            expanded_proofs=[],
            complete=False,
            truncated=False,
            missing=[item_id],
            omitted=[],
            characters_used=0,
            expanded_proof_characters=0,
            max_chars=max_chars,
            round_index=round_index,
            strict_ancestor_item_ids=[],
            expanded_proof_ids=[],
        )

    required_ids = {item_id}
    missing: set[str] = set()
    stack = list(requested.depends_on)
    while stack:
        dependency_id = stack.pop()
        if dependency_id in required_ids or dependency_id in missing:
            continue
        dependency = items_by_id.get(dependency_id)
        if dependency is None:
            missing.add(dependency_id)
            continue
        required_ids.add(dependency_id)
        stack.extend(dependency.depends_on)

    ancestor_ids = [
        ancestor_id
        for ancestor_id in manifest.topological_item_ids
        if ancestor_id in required_ids and ancestor_id != item_id
    ]
    ancestor_id_set = set(ancestor_ids)
    invalid_expansions = [
        expanded_id
        for expanded_id in requested_expansions
        if expanded_id not in ancestor_id_set
    ]
    if invalid_expansions:
        invalid_id = invalid_expansions[0]
        if invalid_id == item_id:
            raise ProofContextError("cannot expand the current proof item")
        if invalid_id not in items_by_id:
            raise ProofContextError(f"unknown expanded proof item {invalid_id!r}")
        raise ProofContextError(
            f"expanded proof item {invalid_id!r} is not a strict ancestor"
        )
    expanded_id_set = set(requested_expansions)
    ordered_expanded_ids = [
        ancestor_id for ancestor_id in ancestor_ids if ancestor_id in expanded_id_set
    ]
    ordered_records: list[
        tuple[str, Literal["current", "premise", "expanded"], dict[str, Any]]
    ] = [
        (item_id, "current", _current_record(requested))
    ]
    ordered_records.extend(
        (ancestor_id, "premise", _premise_record(items_by_id[ancestor_id]))
        for ancestor_id in ancestor_ids
    )
    ordered_records.extend(
        (
            f"expanded_proof:{ancestor_id}",
            "expanded",
            _expanded_proof_record(items_by_id[ancestor_id]),
        )
        for ancestor_id in ordered_expanded_ids
    )

    current_item: dict[str, Any] | None = None
    premises: list[dict[str, Any]] = []
    expanded_proofs: list[dict[str, Any]] = []
    omitted: list[str] = []
    characters_used = 0
    expanded_proof_characters = 0
    exhausted = False
    for record_id, record_kind, record in ordered_records:
        record_characters = len(_canonical_json(record))
        if exhausted or (
            max_chars is not None and characters_used + record_characters > max_chars
        ):
            exhausted = True
            omitted.append(record_id)
            continue
        characters_used += record_characters
        if record_kind == "current":
            current_item = record
        elif record_kind == "premise":
            premises.append(record)
        else:
            expanded_proofs.append(record)
            expanded_proof_characters += record_characters

    missing_list = sorted(missing)
    complete = current_item is not None and not missing_list and not omitted
    return _context_envelope(
        manifest=manifest,
        requested_item_id=item_id,
        current_item=current_item,
        premises=premises,
        expanded_proofs=expanded_proofs,
        complete=complete,
        truncated=bool(omitted),
        missing=missing_list,
        omitted=omitted,
        characters_used=characters_used,
        expanded_proof_characters=expanded_proof_characters,
        max_chars=max_chars,
        round_index=round_index,
        strict_ancestor_item_ids=ancestor_ids,
        expanded_proof_ids=ordered_expanded_ids,
    )


__all__ = [
    "ADAPTIVE_AGGREGATE_CONTEXT_SCHEMA_VERSION",
    "AGGREGATE_CONTEXT_SCHEMA_VERSION",
    "PROOF_ITEM_SCHEMA_VERSION",
    "PROOF_CONTEXT_SCHEMA_VERSION",
    "ProofContextError",
    "ProofDependencyError",
    "ProofItem",
    "ProofManifest",
    "ProofParseError",
    "aggregate_adaptive_context_digest",
    "aggregate_context_digest",
    "build_item_context",
    "extract_verification_target",
    "parse_blueprint",
    "proof_digest",
]
