import pytest

from agentic_os.kernel.capabilities import CapabilityError, CapabilityTable, scope_covers
from agentic_os.kernel.io import SimClock
from agentic_os.kernel.store import KernelStore
from agentic_os.persistence.db import Database


@pytest.fixture()
def table():
    db = Database(":memory:")
    db.migrate()
    clock = SimClock()
    return CapabilityTable(KernelStore(db), clock), clock


def test_scope_prefix_semantics():
    assert scope_covers("", "anything/at/all.txt", "fs")
    assert scope_covers("docs", "docs", "fs")
    assert scope_covers("docs", "docs/a.md", "fs")
    assert not scope_covers("docs", "docsx/a.md", "fs")      # prefix must end at a path boundary
    assert not scope_covers("docs", "src/a.py", "fs")
    assert scope_covers("*", "notepad", "app")
    assert scope_covers("notepad", "notepad", "app")
    assert not scope_covers("notepad", "calc", "app")


def test_grant_and_check(table):
    caps, _ = table
    caps.grant("p1", "fs", "docs", ["read"])
    assert caps.check("p1", "fs", "read", "docs/notes.md")
    with pytest.raises(CapabilityError):
        caps.check("p1", "fs", "write", "docs/notes.md")
    with pytest.raises(CapabilityError):
        caps.check("p1", "fs", "read", "src/app.py")
    with pytest.raises(CapabilityError):
        caps.check("p2", "fs", "read", "docs/notes.md")     # other process holds nothing


def test_attenuation_never_amplifies(table):
    caps, _ = table
    parent = caps.grant("p1", "fs", "docs", ["read", "write"])
    child = caps.attenuate(parent.cap_id, "c1", scope="docs/sub", rights=["read"])
    assert child.scope == "docs/sub" and child.rights == ("read",)
    with pytest.raises(CapabilityError) as e1:
        caps.attenuate(parent.cap_id, "c2", scope="")          # wider scope
    assert e1.value.code == "CAP_AMPLIFICATION"
    with pytest.raises(CapabilityError):
        caps.attenuate(child.cap_id, "c3", rights=["write"])   # more rights than its parent


def test_revocation_cascades_to_delegates(table):
    caps, _ = table
    parent = caps.grant("p1", "fs", "", ["read"])
    child = caps.attenuate(parent.cap_id, "c1")
    grandchild = caps.attenuate(child.cap_id, "g1")
    caps.revoke(parent.cap_id)
    assert caps.get_valid(child.cap_id) is None
    assert caps.get_valid(grandchild.cap_id) is None


def test_expiry_and_one_shot(table):
    caps, clock = table
    timed = caps.grant("p1", "app", "notepad", ["launch"], ttl_s=60)
    assert caps.find("p1", "app", "launch", "notepad") is not None
    clock.advance(61)
    assert caps.get_valid(timed.cap_id) is None
    once = caps.grant("p1", "app", "notepad", ["launch"], uses=1)
    caps.consume(caps.find("p1", "app", "launch", "notepad"))
    assert caps.get_valid(once.cap_id) is None


def test_child_expiry_capped_by_parent(table):
    caps, clock = table
    parent = caps.grant("p1", "fs", "", ["read"], ttl_s=10)
    child = caps.attenuate(parent.cap_id, "c1", ttl_s=1000)
    assert child.expires_at == parent.expires_at


def test_persisted_and_reloaded(table):
    caps, clock = table
    cap = caps.grant("p1", "fs", "docs", ["read"])
    reloaded = CapabilityTable(caps.store, clock)
    reloaded.load()
    assert reloaded.find("p1", "fs", "read", "docs/x") is not None
    caps.revoke(cap.cap_id)
    again = CapabilityTable(caps.store, clock)
    again.load()
    assert again.find("p1", "fs", "read", "docs/x") is None
