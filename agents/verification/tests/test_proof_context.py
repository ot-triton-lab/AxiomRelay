from __future__ import annotations

import hashlib
import json

import pytest

from agents.verification.api.proof_context import (
    ProofDependencyError,
    ProofParseError,
    aggregate_context_digest,
    build_item_context,
    extract_verification_target,
    parse_blueprint,
    proof_digest,
)


def _item(title: str, statement: str, proof: str, metadata: str | None = None) -> str:
    metadata_block = f"\n{metadata}\n" if metadata is not None else "\n"
    return (
        f"# {title}\n"
        f"{metadata_block}"
        f"## statement\n{statement}\n\n"
        f"## proof\n{proof}\n"
    )


def test_legacy_aggregate_manifest_digest_is_byte_stable() -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    manifest = parse_blueprint(proof, target_statement="S")

    assert manifest.item_ids == ("pi_0652364c1f23f18757f6da94",)
    assert aggregate_context_digest(manifest) == (
        "d3bb02f4b90d0c09da1b0fbb10fa2f3dcd3a8df8210598b8c3fa09fb6affcf15"
    )


def test_parse_legacy_blueprint_uses_conservative_prefix_dependencies() -> None:
    source = "\n".join(
        [
            _item("lemma lem:first", "First statement.", "First proof."),
            _item("proposition prop:second", "Second statement.", "Second proof."),
            _item("theorem thm:main", "Main statement.", "Main proof."),
        ]
    )

    manifest = parse_blueprint(source)

    first, second, main = manifest.items
    assert manifest.source_kind == "structured"
    assert first.depends_on == ()
    assert second.depends_on == (first.item_id,)
    assert main.depends_on == (second.item_id,)
    assert all(item.dependency_mode == "conservative-prefix" for item in manifest.items)
    assert manifest.item_ids == tuple(item.item_id for item in manifest.items)
    assert manifest.topological_item_ids == manifest.item_ids


def test_mixed_explicit_branches_use_frontier_with_complete_legacy_prefix_closure() -> None:
    source = "\n".join(
        [
            _item(
                "lemma lem:a",
                "A.",
                "Proof A.",
                "<!-- rethlas-depends-on: -->",
            ),
            _item(
                "lemma lem:b",
                "B.",
                "Proof B.",
                "<!-- rethlas-depends-on: -->",
            ),
            _item("proposition prop:c", "C.", "Proof C."),
            _item("theorem thm:main", "Main.", "Main proof."),
        ]
    )

    manifest = parse_blueprint(source)
    first, second, third, main = manifest.items

    assert third.depends_on == (first.item_id, second.item_id)
    assert main.depends_on == (third.item_id,)
    context = build_item_context(manifest, main.item_id)
    assert [card["item_id"] for card in context["premises"]] == [
        first.item_id,
        second.item_id,
        third.item_id,
    ]
    assert context["complete"] is True


def test_explicit_dependency_syntax_supports_labels_titles_forward_edges_and_empty() -> None:
    source = "\n".join(
        [
            _item(
                "lemma lem:first",
                "First.",
                "Uses the later result.",
                "<!-- rethlas-depends-on: prop:later -->",
            ),
            _item(
                "lemma lem:independent",
                "Independent.",
                "Direct.",
                "<!-- rethlas-depends-on: -->",
            ),
            _item(
                "proposition prop:later",
                "Later.",
                "Uses the independent lemma.",
                "<!-- rethlas-depends-on: lemma lem:independent -->",
            ),
        ]
    )

    manifest = parse_blueprint(source)
    first, independent, later = manifest.items

    assert first.depends_on == (later.item_id,)
    assert independent.depends_on == ()
    assert later.depends_on == (independent.item_id,)
    assert all(item.dependency_mode == "explicit" for item in manifest.items)
    assert manifest.topological_item_ids == (
        independent.item_id,
        later.item_id,
        first.item_id,
    )


def test_item_ids_are_content_addressed_and_deterministic() -> None:
    item_a = _item(
        "lemma lem:a",
        "A.",
        "Proof A.",
        "<!-- rethlas-depends-on: -->",
    )
    item_b = _item(
        "theorem thm:b",
        "B.",
        "Proof B.",
        "<!-- rethlas-depends-on: lem:a -->",
    )
    source = item_a + "\n" + item_b

    first_parse = parse_blueprint(source)
    second_parse = parse_blueprint(source)
    whitespace_variant = parse_blueprint("\n\n" + source.rstrip() + "\n\n")

    assert first_parse == second_parse
    assert first_parse.item_ids == whitespace_variant.item_ids
    assert first_parse.proof_digest != whitespace_variant.proof_digest
    assert all(item.item_id == f"pi_{item.digest[:24]}" for item in first_parse.items)
    assert proof_digest(source) == hashlib.sha256(source.encode("utf-8")).hexdigest()


def test_changing_an_ancestor_propagates_through_merkle_item_ids() -> None:
    first_source = "\n".join(
        [
            _item("lemma lem:a", "A.", "Proof A."),
            _item("theorem thm:b", "B.", "Proof B."),
        ]
    )
    changed_source = first_source.replace("Proof A.", "Changed proof A.")

    original = parse_blueprint(first_source)
    changed = parse_blueprint(changed_source)

    assert original.items[0].item_id != changed.items[0].item_id
    assert original.items[1].item_id != changed.items[1].item_id


def test_unstructured_legacy_proof_becomes_one_synthetic_main_item() -> None:
    source = "A direct prose proof without paper headings.\nTherefore the result follows."

    manifest = parse_blueprint(source, target_statement="The target theorem.")

    assert manifest.source_kind == "synthetic"
    assert len(manifest.items) == 1
    item = manifest.items[0]
    assert item.title == "theorem synthetic:main"
    assert item.statement == "The target theorem."
    assert item.proof == source
    assert item.depends_on == ()
    assert item.dependency_mode == "synthetic"


def test_markdown_headings_inside_fenced_code_do_not_create_items_or_sections() -> None:
    source = _item(
        "theorem thm:main",
        "A statement.",
        """A proof containing an example:
```markdown
# fake theorem thm:fake
## statement
not an item
## proof
not a proof section
```
The real proof continues.""",
    )

    manifest = parse_blueprint(source)

    assert len(manifest.items) == 1
    assert "# fake theorem" in manifest.items[0].proof
    assert "The real proof continues." in manifest.items[0].proof


def test_parser_preserves_indentation_in_full_statement_and_proof_text() -> None:
    source = """# theorem thm:main

## statement

    Indented statement text.

## proof

    Indented proof text.

"""
    manifest = parse_blueprint(source)

    assert manifest.items[0].statement == "    Indented statement text."
    assert manifest.items[0].proof == "    Indented proof text."


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            _item("lemma lem:a", "A.", "Proof A.")
            + "\n"
            + _item("lemma lem:a", "Other A.", "Other proof."),
            "duplicate proof-item title",
        ),
        (
            _item("lemma lem:same", "A.", "Proof A.")
            + "\n"
            + _item("proposition lem:same", "B.", "Proof B."),
            "duplicate proof-item label",
        ),
        ("# lemma lem:a\n\n## proof\nProof.", "exactly one ## statement"),
        ("# lemma lem:a\n\n## statement\nA.", "exactly one ## proof"),
        (
            "# lemma lem:a\n\n## statement\n\n## proof\nProof.",
            "empty ## statement",
        ),
        (
            "# lemma lem:a\n\n## statement\nA.\n\n## proof\n",
            "empty ## proof",
        ),
        (
            "# lemma lem:a\n\n## proof\nProof.\n\n## statement\nA.",
            "must precede",
        ),
        (
            "# lemma lem:a\n\n## statement\nA.\n\n## details\nD.\n\n## proof\nP.",
            "unexpected level-two section",
        ),
        (
            "Introductory text.\n\n" + _item("lemma lem:a", "A.", "Proof."),
            "before the first proof item",
        ),
        (
            "## statement\nA.\n\n## proof\nP.",
            "without a level-one item",
        ),
        (
            "# Overview\nThis attempted structured document is malformed.",
            "exactly one ## statement",
        ),
    ],
)
def test_structured_bad_inputs_fail_closed(source: str, message: str) -> None:
    with pytest.raises(ProofParseError, match=message):
        parse_blueprint(source, target_statement="Must not trigger fallback.")


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        ("<!-- rethlas-depends-on lem:a -->", "malformed"),
        ("<!-- rethlas-depends-on: lem:a, -->", "empty dependency"),
        ("<!-- rethlas-depends-on: lem:a, lem:a -->", "duplicate dependency"),
    ],
)
def test_malformed_dependency_metadata_is_rejected(metadata: str, message: str) -> None:
    source = _item("theorem thm:main", "Main.", "Proof.", metadata)
    with pytest.raises(ProofDependencyError, match=message):
        parse_blueprint(source)


def test_duplicate_dependency_comments_are_rejected() -> None:
    source = """# theorem thm:main

<!-- rethlas-depends-on: -->
<!-- rethlas-depends-on: -->
## statement
Main.
## proof
Proof.
"""
    with pytest.raises(ProofDependencyError, match="duplicate"):
        parse_blueprint(source)


def test_dependency_comment_after_statement_is_rejected() -> None:
    source = """# theorem thm:main

## statement
Main.
<!-- rethlas-depends-on: -->
## proof
Proof.
"""
    with pytest.raises(ProofDependencyError, match="must appear before"):
        parse_blueprint(source)


def test_unknown_self_duplicate_resolved_and_cyclic_dependencies_are_rejected() -> None:
    unknown = _item(
        "theorem thm:main",
        "Main.",
        "Proof.",
        "<!-- rethlas-depends-on: lem:missing -->",
    )
    with pytest.raises(ProofDependencyError, match="unknown dependency"):
        parse_blueprint(unknown)

    self_dependent = _item(
        "theorem thm:main",
        "Main.",
        "Proof.",
        "<!-- rethlas-depends-on: thm:main -->",
    )
    with pytest.raises(ProofDependencyError, match="self-dependency"):
        parse_blueprint(self_dependent)

    duplicate_resolved = "\n".join(
        [
            _item("lemma lem:a", "A.", "Proof A.", "<!-- rethlas-depends-on: -->"),
            _item(
                "theorem thm:main",
                "Main.",
                "Proof.",
                "<!-- rethlas-depends-on: lem:a, lemma lem:a -->",
            ),
        ]
    )
    with pytest.raises(ProofDependencyError, match="duplicate resolved"):
        parse_blueprint(duplicate_resolved)

    cyclic = "\n".join(
        [
            _item(
                "lemma lem:a",
                "A.",
                "Proof A.",
                "<!-- rethlas-depends-on: lem:b -->",
            ),
            _item(
                "lemma lem:b",
                "B.",
                "Proof B.",
                "<!-- rethlas-depends-on: lem:a -->",
            ),
        ]
    )
    with pytest.raises(ProofDependencyError, match="cycle"):
        parse_blueprint(cyclic)


def test_unstructured_proof_requires_target_statement() -> None:
    with pytest.raises(ProofParseError, match="target_statement is required"):
        parse_blueprint("A prose proof.")
    with pytest.raises(ProofParseError, match="non-empty"):
        parse_blueprint("   ", target_statement="Target.")


def test_structured_final_item_must_match_supplied_target_statement() -> None:
    source = "\n".join(
        [
            _item("lemma lem:a", "A.", "Proof A."),
            _item("theorem thm:main", "A different theorem.", "Easy proof."),
        ]
    )

    with pytest.raises(ProofParseError, match="exactly match target_statement"):
        parse_blueprint(source, target_statement="The requested hard theorem.")

    matching = parse_blueprint(source, target_statement="A different theorem.")
    assert matching.items[-1].statement == "A different theorem."


def test_problem_document_wrappers_are_excluded_from_target_matching() -> None:
    mathematical_target = r"""Let $x\in\mathbb R$.

Prove that $x^2\geq0$."""
    problem_document = f"""# Display title

{mathematical_target}

## Retrieval restriction

This run is offline.
"""
    source = _item(
        "theorem thm:main",
        mathematical_target,
        "The square of a real number is nonnegative.",
    )

    assert extract_verification_target(problem_document) == mathematical_target
    manifest = parse_blueprint(source, target_statement=problem_document)
    assert manifest.items[-1].statement == mathematical_target

    with pytest.raises(ProofParseError, match="exactly match target_statement"):
        parse_blueprint(
            _item("theorem thm:main", "A different claim.", "Proof."),
            target_statement=problem_document,
        )


def test_canonical_final_statement_may_contain_structural_headings() -> None:
    mathematical_target = """Opening target text.

## Frozen deterministic setting

The setting is part of the target.

## statement

This heading is mathematical input, not an item control.

## proof

This heading is also mathematical input.

# Nested target heading

The nested heading remains in the target verbatim."""
    problem_document = f"""# Display title

{mathematical_target}

## Retrieval restriction

Offline only.
"""
    source = "\n".join(
        [
            _item(
                "lemma lem:preliminary",
                "A preliminary statement.",
                "A preliminary proof.",
                "<!-- rethlas-depends-on: -->",
            ),
            _item(
                "theorem thm:main",
                mathematical_target,
                "The complete target now follows.",
                "<!-- rethlas-depends-on: lem:preliminary -->",
            ),
        ]
    )

    with pytest.raises(ProofParseError):
        parse_blueprint(source)

    manifest = parse_blueprint(source, target_statement=problem_document)
    assert len(manifest.items) == 2
    assert manifest.items[-1].statement == mathematical_target
    assert manifest.items[-1].dependency_mode == "explicit"


def test_target_wrapper_detection_ignores_fenced_heading_like_text() -> None:
    target = """```markdown
# Not a document title
## Retrieval restriction
```

Prove the displayed claim."""

    assert extract_verification_target(target) == target


def test_target_wrapper_extraction_fails_closed_on_empty_body() -> None:
    with pytest.raises(ProofParseError, match="no mathematical content"):
        extract_verification_target(
            "# Display title\n\n## Retrieval restriction\nOffline only.\n"
        )


def test_unstructured_synthetic_item_preserves_raw_target_for_receipt_compatibility() -> None:
    problem_document = """# Display title

Target theorem.

## Retrieval restriction
Offline only.
"""
    manifest = parse_blueprint("A prose proof.", target_statement=problem_document)

    assert manifest.source_kind == "synthetic"
    assert manifest.items[0].statement == problem_document.strip()


def test_level_two_subheadings_after_proof_heading_remain_in_full_proof() -> None:
    source = """# theorem thm:main

## statement
Main.

## proof
Opening argument.

## Case one
First case.

## Case two
Second case.
"""
    manifest = parse_blueprint(source)

    assert "## Case one\nFirst case." in manifest.items[0].proof
    assert "## Case two\nSecond case." in manifest.items[0].proof


def test_context_contains_current_proof_and_only_ancestor_statements_and_edges() -> None:
    source = "\n".join(
        [
            _item(
                "lemma lem:a",
                "Statement A.",
                "Secret proof A.",
                "<!-- rethlas-depends-on: -->",
            ),
            _item(
                "lemma lem:unrelated",
                "Unrelated.",
                "Unrelated proof.",
                "<!-- rethlas-depends-on: -->",
            ),
            _item(
                "proposition prop:b",
                "Statement B.",
                "Secret proof B.",
                "<!-- rethlas-depends-on: lem:a -->",
            ),
            _item(
                "theorem thm:main",
                "Main statement.",
                "Full current proof.",
                "<!-- rethlas-depends-on: prop:b -->",
            ),
        ]
    )
    manifest = parse_blueprint(source)
    main = manifest.items[-1]

    context = build_item_context(manifest, main.item_id)

    assert context["complete"] is True
    assert context["truncated"] is False
    assert context["missing"] == []
    assert context["omitted"] == []
    assert context["current_item"]["proof"] == "Full current proof."
    premise_ids = [record["item_id"] for record in context["premises"]]
    assert premise_ids == [manifest.items[0].item_id, manifest.items[2].item_id]
    assert all("proof" not in record for record in context["premises"])
    assert context["premises"][1]["depends_on"] == [manifest.items[0].item_id]
    assert manifest.items[1].item_id not in premise_ids


def test_budget_accounting_is_whole_record_and_fail_closed() -> None:
    source = "\n".join(
        [
            _item("lemma lem:a", "A" * 100, "Proof A."),
            _item("theorem thm:main", "Main.", "Current proof " * 20),
        ]
    )
    manifest = parse_blueprint(source)
    item_id = manifest.items[-1].item_id
    unlimited = build_item_context(manifest, item_id)

    exact = build_item_context(
        manifest, item_id, max_chars=unlimited["characters_used"]
    )
    one_short = build_item_context(
        manifest, item_id, max_chars=unlimited["characters_used"] - 1
    )
    zero = build_item_context(manifest, item_id, max_chars=0)

    assert exact["complete"] is True
    assert exact["characters_used"] == unlimited["characters_used"]
    assert one_short["complete"] is False
    assert one_short["truncated"] is True
    assert one_short["current_item"]["proof"] == manifest.items[-1].proof
    assert one_short["premises"] == []
    assert one_short["omitted"] == [manifest.items[0].item_id]
    assert zero["current_item"] is None
    assert zero["premises"] == []
    assert zero["omitted"] == [item_id, manifest.items[0].item_id]
    assert zero["characters_used"] == 0


def test_unknown_context_root_is_explicitly_missing() -> None:
    manifest = parse_blueprint(_item("theorem thm:main", "Main.", "Proof."))

    context = build_item_context(manifest, "pi_missing")

    assert context["complete"] is False
    assert context["truncated"] is False
    assert context["missing"] == ["pi_missing"]
    assert context["omitted"] == []
    assert context["current_item"] is None
    assert context["characters_used"] == 0


def test_context_digest_is_deterministic_and_attests_budget_status() -> None:
    manifest = parse_blueprint(_item("theorem thm:main", "Main.", "Proof."))
    item_id = manifest.items[0].item_id

    first = build_item_context(manifest, item_id)
    second = build_item_context(manifest, item_id)
    truncated = build_item_context(manifest, item_id, max_chars=0)

    assert first == second
    assert first["digest"] != truncated["digest"]


def test_adaptive_context_hydrates_only_requested_complete_proof_records() -> None:
    source = "\n".join(
        [
            _item(
                "lemma lem:root",
                "Root statement.",
                "ROOT PROOF BYTES",
                "<!-- rethlas-depends-on: -->",
            ),
            _item(
                "lemma lem:middle",
                "Middle statement.",
                "MIDDLE PROOF BYTES",
                "<!-- rethlas-depends-on: lem:root -->",
            ),
            _item(
                "theorem thm:main",
                "Main statement.",
                "CURRENT PROOF BYTES",
                "<!-- rethlas-depends-on: lem:middle -->",
            ),
        ]
    )
    manifest = parse_blueprint(source)
    current = manifest.items[-1]
    root = manifest.items[0]

    first = build_item_context(manifest, current.item_id, round_index=0)
    second = build_item_context(
        manifest,
        current.item_id,
        expanded_proof_ids=[root.item_id],
        round_index=1,
    )

    assert first["expanded_proofs"] == []
    assert all("proof" not in premise for premise in first["premises"])
    assert all("proof" not in premise for premise in second["premises"])
    assert [record["item_id"] for record in second["expanded_proofs"]] == [
        root.item_id
    ]
    assert second["expanded_proofs"][0]["proof"] == "ROOT PROOF BYTES"
    assert "MIDDLE PROOF BYTES" not in json.dumps(second, sort_keys=True)
    assert second["round"] == 1
    assert second["digest"] != first["digest"]


@pytest.mark.parametrize("kind", ["duplicate", "current", "unknown", "nonancestor"])
def test_adaptive_context_rejects_out_of_scope_expansions(kind: str) -> None:
    source = "\n".join(
        [
            _item(
                "lemma lem:a",
                "A.",
                "Proof A.",
                "<!-- rethlas-depends-on: -->",
            ),
            _item(
                "lemma lem:unrelated",
                "U.",
                "Proof U.",
                "<!-- rethlas-depends-on: -->",
            ),
            _item(
                "theorem thm:main",
                "M.",
                "Proof M.",
                "<!-- rethlas-depends-on: lem:a -->",
            ),
        ]
    )
    manifest = parse_blueprint(source)
    ancestor, unrelated, current = manifest.items
    if kind == "duplicate":
        expanded = [ancestor.item_id, ancestor.item_id]
    elif kind == "current":
        expanded = [current.item_id]
    elif kind == "unknown":
        expanded = ["pi_" + "0" * 24]
    else:
        expanded = [unrelated.item_id]

    with pytest.raises(ValueError):
        build_item_context(
            manifest,
            current.item_id,
            expanded_proof_ids=expanded,
            round_index=1,
        )


def test_adaptive_context_rejects_inconsistent_round_and_expansion_state() -> None:
    manifest = parse_blueprint(
        "\n".join(
            [
                _item("lemma lem:a", "A.", "Proof A."),
                _item("theorem thm:m", "M.", "Proof M."),
            ]
        )
    )
    ancestor, current = manifest.items
    with pytest.raises(ValueError, match="round zero"):
        build_item_context(
            manifest,
            current.item_id,
            expanded_proof_ids=[ancestor.item_id],
            round_index=0,
        )
    with pytest.raises(ValueError, match="positive adaptive rounds"):
        build_item_context(manifest, current.item_id, round_index=1)


def test_expanded_proof_byte_change_changes_context_attestation() -> None:
    first_source = "\n".join(
        [
            _item("lemma lem:a", "A.", "Proof byte A."),
            _item("theorem thm:m", "M.", "Proof M."),
        ]
    )
    second_source = first_source.replace("Proof byte A.", "Proof byte B.")
    first_manifest = parse_blueprint(first_source)
    second_manifest = parse_blueprint(second_source)
    first_context = build_item_context(
        first_manifest,
        first_manifest.items[-1].item_id,
        expanded_proof_ids=[first_manifest.items[0].item_id],
        round_index=1,
    )
    second_context = build_item_context(
        second_manifest,
        second_manifest.items[-1].item_id,
        expanded_proof_ids=[second_manifest.items[0].item_id],
        round_index=1,
    )

    assert first_context["digest"] != second_context["digest"]
    assert first_context["expanded_proof_characters"] == second_context[
        "expanded_proof_characters"
    ]


@pytest.mark.parametrize("bad_budget", [-1, -100])
def test_negative_context_budget_is_rejected(bad_budget: int) -> None:
    manifest = parse_blueprint(_item("theorem thm:main", "Main.", "Proof."))
    with pytest.raises(ValueError, match=">= 0"):
        build_item_context(manifest, manifest.items[0].item_id, max_chars=bad_budget)


def test_deep_dependency_dag_is_parsed_and_expanded_iteratively() -> None:
    item_count = 1_200
    parts: list[str] = []
    for index in range(item_count):
        dependency = "" if index == 0 else f"lem:{index - 1}"
        parts.append(
            _item(
                f"lemma lem:{index}",
                f"Statement {index}.",
                f"Proof {index}.",
                f"<!-- rethlas-depends-on: {dependency} -->",
            )
        )

    manifest = parse_blueprint("\n".join(parts))
    context = build_item_context(manifest, manifest.items[-1].item_id)

    assert len(manifest.items) == item_count
    assert len(context["premises"]) == item_count - 1
    assert context["complete"] is True
    assert context["premises"][0]["statement"] == "Statement 0."
    assert context["premises"][-1]["statement"] == f"Statement {item_count - 2}."


def test_large_legacy_blueprint_uses_linear_edges_with_complete_prefix_closure() -> None:
    item_count = 1_200
    source = "\n".join(
        _item(f"lemma lem:{index}", f"Statement {index}.", f"Proof {index}.")
        for index in range(item_count)
    )

    manifest = parse_blueprint(source)
    context = build_item_context(manifest, manifest.items[-1].item_id)

    assert sum(len(item.depends_on) for item in manifest.items) == item_count - 1
    assert len(context["premises"]) == item_count - 1
    assert context["complete"] is True


def test_large_diamond_dag_deduplicates_shared_ancestors() -> None:
    branch_count = 600
    parts = [
        _item(
            "lemma lem:root",
            "Root.",
            "Proof root.",
            "<!-- rethlas-depends-on: -->",
        )
    ]
    for index in range(branch_count):
        parts.append(
            _item(
                f"lemma lem:b{index}",
                f"Branch {index}.",
                f"Proof branch {index}.",
                "<!-- rethlas-depends-on: lem:root -->",
            )
        )
    dependencies = ", ".join(f"lem:b{index}" for index in range(branch_count))
    parts.append(
        _item(
            "theorem thm:main",
            "Main.",
            "Proof main.",
            f"<!-- rethlas-depends-on: {dependencies} -->",
        )
    )

    manifest = parse_blueprint("\n".join(parts))
    context = build_item_context(manifest, manifest.items[-1].item_id)
    premise_ids = [record["item_id"] for record in context["premises"]]

    assert len(premise_ids) == branch_count + 1
    assert len(set(premise_ids)) == len(premise_ids)
    assert premise_ids.count(manifest.items[0].item_id) == 1
