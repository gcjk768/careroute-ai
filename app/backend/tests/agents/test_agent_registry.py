"""[Agentic] Agent Registry / discovery (AAS Day 3 AM, slide 18).

The properties worth pinning:
  * there is ONE canonical agent list, and the test harness uses it — a worker
    added to the app cannot slip past the parametrized contract tests;
  * discovery reports each worker's OWN declared capability, so it cannot
    flatter the app: if a worker is a policy node, `/api/agents` says so;
  * the registry stays honest about what it is NOT — no endpoints, no transport,
    because these are in-process stages and not addressable services.
"""
from __future__ import annotations

import pytest

from app.agents import registry
from app.agents.capability import AGENT, ORCHESTRATOR, POLICY_NODE

pytestmark = pytest.mark.capability


def test_the_harness_uses_the_apps_registry_not_its_own_copy():
    """The duplicate list this replaced carried a hand-maintained 'keep in
    lock-step' comment, which is the kind of promise that quietly stops being
    true — a new agent would simply have had no boundary test."""
    from harness import AGENT_CLASSES

    assert AGENT_CLASSES is registry.AGENT_CLASSES
    print("[REGISTRY PASS] The test harness and the app share ONE agent list, so a new worker is")
    print("                covered by the contract/comms/capability tests automatically.")


def test_every_registered_agent_is_in_the_pipeline_order_and_has_an_owner():
    assert set(registry.PIPELINE_ORDER) == set(registry.AGENT_CLASSES)
    assert set(registry.AGENT_OWNERS) == set(registry.AGENT_CLASSES)
    for slug, owner in registry.AGENT_OWNERS.items():
        assert owner and owner != "unassigned", slug
    print("[REGISTRY PASS] Every worker has a position in the sequence and a named owner — a")
    print("                registry that cannot say who to ask is a glossary.")


def test_discovery_reports_each_workers_own_declaration():
    """Discovery must not be able to flatter the app: it reads CAPABILITY."""
    for record in registry.discover():
        declared = registry.capability_for(record["slug"])
        assert record["classification"] == declared.classification
        assert record["actionSpace"] == list(declared.action_space)
        assert record["usesTrainedModel"] == declared.uses_trained_model
        assert record["justification"] == declared.justification
    print("[REGISTRY PASS] /api/agents serves each worker's own CAPABILITY, so a policy node is")
    print("                reported as a policy node rather than described as an 'agent'.")


def test_the_summary_matches_the_audit_note():
    """The numbers [[Agent Capability Audit]] claims, checkable at runtime."""
    summary = registry.summary()
    assert summary["agents"] == 7
    assert summary["byClassification"][ORCHESTRATOR] == 1
    assert summary["byClassification"][AGENT] == 5
    assert summary["byClassification"][POLICY_NODE] == 1
    # [Point 3] Exactly one worker consumes the trained RandomForest.
    assert summary["usesTrainedModel"] == ["classifier"]
    # The last worker that has not earned the word "agent" still owes an upgrade path.
    # WHICH worker that is has moved once already and may move again: the registry is
    # derived from each worker's own declared AgentCapability, so promoting one is a
    # one-line change in that worker. The invariant worth pinning is the COUNT — one
    # policy node, one outstanding upgrade path, and they are the same worker.
    assert summary["byClassification"][POLICY_NODE] == 1
    assert len(summary["withUpgradePath"]) == 1
    policy_node = summary["withUpgradePath"][0]
    assert registry.describe(policy_node)["classification"] == POLICY_NODE
    print("[REGISTRY PASS] 5 agents + 1 orchestrator + 1 policy node, one trained-model consumer,")
    print(f"                one outstanding upgrade path ({policy_node}) — served live.")


def test_a_policy_node_still_advertises_its_upgrade_path():
    [slug] = registry.summary()["withUpgradePath"]
    record = registry.describe(slug)
    assert record["classification"] == POLICY_NODE
    assert record["upgradePath"].strip()
    print("[REGISTRY PASS] A worker that has not earned the word 'agent' says so at runtime, not")
    print("                only in a document a reviewer may never open.")


def test_discovery_advertises_no_endpoint_or_transport():
    """Slide 18's two exposure modes are declined in writing. A record carrying
    an address would quietly claim a distributed system we do not have."""
    forbidden = {"endpoint", "url", "transport", "address", "host", "port"}
    for record in registry.discover():
        assert not (forbidden & set(record)), record["slug"]
    print("[REGISTRY PASS] No record carries an address — the registry is for discovery and")
    print("                introspection, and does not pretend the agents are services.")
