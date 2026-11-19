import os

import pytest

from agentic_os.kernel.journal import CommitConflict, Journal
from agentic_os.kernel.overlay import OverlayWorkspace
from agentic_os.kernel.store import KernelStore
from agentic_os.persistence.db import Database
from agentic_os.tools.fileio import atomic_replace
from agentic_os.tools.pathguard import PathError

from .harness import make_env, snapshot


class Crash(Exception):
    pass


@pytest.fixture()
def env(tmp_path):
    e = make_env(tmp_path)
    db = Database(e.db_path)
    db.migrate()
    e.store = KernelStore(db)
    return e


def overlay(env, cap_check=None, oid="ov1"):
    return OverlayWorkspace(env.workspace, os.path.join(env.data_dir, "overlays", oid, "upper"), oid, env.store,
                            cap_check=cap_check)


def write(ov, rel, text):
    target = ov.resolve(rel, for_write=True)
    os.makedirs(os.path.dirname(target.abs_path), exist_ok=True)
    atomic_replace(target.abs_path, text.encode())


def read(ov, rel):
    with open(ov.resolve(rel).abs_path, encoding="utf-8") as fh:
        return fh.read()


def test_writes_are_staged_and_reads_see_them(env):
    before = snapshot(env.workspace)
    ov = overlay(env)
    write(ov, "docs/notes.md", "changed\n")
    write(ov, "new/dir/file.txt", "hello\n")
    assert snapshot(env.workspace) == before                       # lower untouched
    assert read(ov, "docs/notes.md") == "changed\n"                 # read-your-writes
    assert read(ov, "README.md").startswith("# Demo")               # untouched files read from lower
    paths = {c["rel_path"]: c["op"] for c in ov.changes()}
    assert paths == {"docs/notes.md": "modify", "new/dir/file.txt": "create"}


def test_merged_listing(env):
    ov = overlay(env)
    write(ov, "docs/extra.md", "x\n")
    entries, _, _ = ov.walk(ov.resolve("."), max_depth=4, max_entries=500)
    paths = {e["path"] for e in entries}
    assert {"README.md", "docs/notes.md", "docs/extra.md", "src/app.py"} <= paths
    files = list(ov.iter_files(ov.resolve("docs"), max_depth=3, max_files=50))
    assert sorted(files) == ["docs/extra.md", "docs/notes.md"]


def test_unchanged_copy_up_is_not_a_change(env):
    ov = overlay(env)
    ov.resolve("README.md", for_write=True)    # copy-up during an assessment that never writes
    assert ov.changes() == []


def test_sensitive_files_never_copied_up(env):
    ov = overlay(env)
    target = ov.resolve(".env", for_write=True)
    assert target.sensitive
    assert not os.path.exists(target.abs_path)


def test_capability_scope_enforced_at_resolve(env):
    def only_docs(rel, right):
        if not (rel == "docs" or rel.startswith("docs/")):
            raise PathError("PATH_CAPABILITY_DENIED", "nope")

    ov = overlay(env, cap_check=only_docs)
    ov.resolve("docs/notes.md")
    with pytest.raises(PathError) as exc:
        ov.resolve("src/app.py")
    assert exc.value.code == "PATH_CAPABILITY_DENIED"


def test_commit_then_undo_restores_exact_bytes(env):
    before = snapshot(env.workspace)
    ov = overlay(env)
    write(ov, "docs/notes.md", "changed\n")
    write(ov, "new/dir/file.txt", "hello\n")
    j = Journal(env.store, env.data_dir)
    res = j.commit(ov, "p1", "t1")
    assert {f["path"] for f in res["files"]} == {"docs/notes.md", "new/dir/file.txt"}
    with open(os.path.join(env.workspace, "docs", "notes.md")) as fh:
        assert fh.read() == "changed\n"
    assert ov.changes() == []                                       # overlay cleared
    j.undo(res["txn_id"])
    assert snapshot(env.workspace) == before
    assert not os.path.exists(os.path.join(env.workspace, "new"))   # created folders removed


def test_commit_refuses_on_conflict(env):
    ov = overlay(env)
    write(ov, "docs/notes.md", "agent version\n")
    with open(os.path.join(env.workspace, "docs", "notes.md"), "w") as fh:
        fh.write("user edited meanwhile\n")
    after_user_edit = snapshot(env.workspace)
    with pytest.raises(CommitConflict) as exc:
        Journal(env.store, env.data_dir).commit(ov, "p1", "t1")
    assert exc.value.conflicts[0]["path"] == "docs/notes.md"
    assert snapshot(env.workspace) == after_user_edit               # nothing written


def test_create_conflict_when_file_appears(env):
    ov = overlay(env)
    write(ov, "fresh.txt", "agent\n")
    with open(os.path.join(env.workspace, "fresh.txt"), "w") as fh:
        fh.write("user\n")
    with pytest.raises(CommitConflict):
        Journal(env.store, env.data_dir).commit(ov, "p1", "t1")


def test_undo_refuses_when_file_edited_after_commit(env):
    ov = overlay(env)
    write(ov, "docs/notes.md", "agent\n")
    j = Journal(env.store, env.data_dir)
    res = j.commit(ov, "p1", "t1")
    with open(os.path.join(env.workspace, "docs", "notes.md"), "w") as fh:
        fh.write("user kept editing\n")
    with pytest.raises(CommitConflict):
        j.undo(res["txn_id"])


@pytest.mark.parametrize("point,expect", [
    ("prepared", "original"),        # crash before the commit point: aborted, nothing applied
    ("commit_point", "committed"),   # crash right after the commit point: rolled forward
    ("applied:1", "committed"),      # crash mid-apply: rolled forward
    ("applied:2", "committed"),
])
def test_crash_recovery(env, point, expect):
    before = snapshot(env.workspace)
    ov = overlay(env)
    write(ov, "docs/notes.md", "changed\n")
    write(ov, "src/app.py", "def main():\n    return 'bye'\n")
    write(ov, "zz/new.txt", "new\n")

    def fault(p):
        if p == point:
            raise Crash(p)

    with pytest.raises(Crash):
        Journal(env.store, env.data_dir, fault=fault).commit(ov, "p1", "t1")
    actions = Journal(env.store, env.data_dir).recover()
    assert len(actions) == 1
    if expect == "original":
        assert actions[0]["action"] == "aborted"
        assert snapshot(env.workspace) == before
        assert len(ov.changes()) == 3                               # staged work still there to retry
    else:
        assert actions[0]["action"] == "rolled_forward"
        with open(os.path.join(env.workspace, "src", "app.py")) as fh:
            assert "bye" in fh.read()
        assert os.path.exists(os.path.join(env.workspace, "zz", "new.txt"))


def test_undo_crash_rolls_forward(env):
    before = snapshot(env.workspace)
    ov = overlay(env)
    write(ov, "docs/notes.md", "changed\n")
    write(ov, "zz/new.txt", "new\n")
    res = Journal(env.store, env.data_dir).commit(ov, "p1", "t1")

    def fault(p):
        if p == "undone:2":
            raise Crash(p)

    with pytest.raises(Crash):
        Journal(env.store, env.data_dir, fault=fault).undo(res["txn_id"])
    Journal(env.store, env.data_dir).recover()
    assert snapshot(env.workspace) == before
    assert env.store.get_txn(res["txn_id"])["state"] == "UNDONE"
