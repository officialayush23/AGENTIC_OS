import os

import pytest

from agentic_os.kernel import RulesPlanner
from agentic_os.kernel.planner import validate_plan
from agentic_os.tools.registry import build_default_registry

from .harness import build_kernel, make_env, snapshot

REG = build_default_registry()
CASES = [
    ("Write a project report", ["list_workspace_files", "identify_project_config_files", "build_workspace_report",
                                "create_workspace_file"]),
    ("search for \"TODO\"", ["search_workspace_files", "search_knowledge"]),
    ("find files named *.md", ["search_workspace_files"]),
    ("read docs/notes.md", ["read_workspace_file"]),
    ("list files in src", ["list_workspace_files"]),
    ("create notes/todo.md with \"buy milk\"", ["create_workspace_file"]),
    ("append \"more\" to docs/notes.md", ["edit_workspace_file"]),
    ("replace \"TODO\" with \"DONE\" in docs/notes.md", ["edit_workspace_file"]),
    ("how does the app start?", ["search_knowledge"]),
]


@pytest.mark.parametrize("request_text,tools", CASES)
def test_rules_produce_valid_plans(request_text, tools):
    plan = RulesPlanner().match(request_text)
    assert plan is not None, request_text
    assert [s["tool"] for s in plan["steps"]] == tools
    assert validate_plan(plan, REG, max_steps=12) == []


def test_unknown_request_asks_for_clarification():
    out = RulesPlanner().plan("make me a sandwich", [])
    assert out["plan"]["needs_clarification"] and "AI provider" in out["plan"]["clarification_question"]


def test_rules_report_end_to_end(tmp_path):
    env = make_env(tmp_path)
    k = build_kernel(env, planner=RulesPlanner())
    pid = k.spawn("Write a project report")
    k.run_until_idle()
    p = k.proc(pid)
    assert p["pending_user"]["kind"] == "review"
    k.commit(pid)
    k.run_until_idle()
    assert os.path.exists(os.path.join(env.workspace, "reports", "project_report.md"))
