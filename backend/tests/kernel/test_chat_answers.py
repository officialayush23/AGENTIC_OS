"""Chat routing and answer shaping: small talk goes to the model, focus questions brief the day,
answers keep only the sources they used and get local time."""

from types import SimpleNamespace

from agentic_os.kernel.planner import HybridPlanner, RulesPlanner, StaticPlanner
from agentic_os.services import chat as chat_mod
from agentic_os.services.chat import CONTEXT_MARK, ChatService, _keep_used


def _llm_planner():
    return StaticPlanner({"goal": "llm", "steps": [{"id": "r", "tool": "respond_to_user", "args": {"text": "Haan bhai!"}}],
                          "needs_clarification": False})


def test_small_talk_and_questions_go_to_the_model_when_available():
    hp = HybridPlanner(llm_factory=_llm_planner)
    assert hp.plan("BOL BHAI", [])["planner"] == "llm"
    assert hp.plan("hello", [])["planner"] == "llm"
    assert hp.plan("how does the scheduler work?", [])["planner"] == "llm"


def test_offline_still_greets_and_answers_questions():
    rp = RulesPlanner()
    assert rp.match("bol bhai")["steps"][0]["tool"] == "respond_to_user"
    assert rp.match("how does the scheduler work?")["steps"][0]["tool"] == "search_knowledge"


def test_focus_question_briefs_the_day_with_or_without_a_model():
    for offline in (True, False):
        p = RulesPlanner().match("WHAT IS MY MAIN FOCUS POINT RIGHT NOW", offline=offline)
        assert [s["tool"] for s in p["steps"]] == ["calendar_list", "email_list", "search_knowledge"]
    hp = HybridPlanner(llm_factory=_llm_planner)
    assert hp.plan("what should I work on today?" + CONTEXT_MARK + "\nuser: hi", [])["planner"] == "rules"


def test_keep_used_drops_unused_passages():
    cards = [{"kind": "hits", "items": [{"path": "src/app.py"}, {"path": "notes/ideas.md"}]}, {"kind": "events", "items": []}]
    kept = _keep_used(cards, ["notes/ideas.md"])
    assert kept[0]["items"] == [{"path": "notes/ideas.md"}] and kept[1]["kind"] == "events"
    assert [c["kind"] for c in _keep_used(cards, [])] == ["events"]


class _DB:
    def __init__(self):
        self.updates = []

    def execute(self, sql, params=()):
        self.updates.append((sql, params))


def test_answer_uses_model_text_local_time_and_used_sources(monkeypatch):
    seen = {}

    class Provider:
        def generate_structured(self, system, user, schema, name):
            seen["system"], seen["user"] = system, user
            return SimpleNamespace(data={"text": "Focus: ship the kernel UI. Meeting tomorrow 3:00 PM.",
                                         "used_sources": ["notes/ideas.md"]})

    task = {"request": "what is my focus" + CONTEXT_MARK + "\nuser: hi", "exit": {"code": "completed"},
            "plan": {"steps": [{"id": "cal", "tool": "calendar_list"}, {"id": "kb", "tool": "search_knowledge"},
                               {"id": "r", "tool": "respond_to_user"}]}}
    outs = {"cal": {"events": [{"title": "Meeting", "start": "2026-11-20T15:00"}]},
            "kb": {"hits": [{"path": "src/app.py", "start_line": 1, "end_line": 3, "text": "demo"},
                            {"path": "notes/ideas.md", "start_line": 1, "end_line": 4, "text": "TODO"}]},
            "r": {"text": "written before the results"}}
    db = _DB()
    svc = ChatService(db, spawn=None, task_view=lambda pid: task, outputs=lambda pid, sid: outs[sid],
                      llm=lambda: Provider(), publish=lambda e: None)
    svc._finish({"id": "m1", "pid": "p1", "conversation_id": "c1", "meta_json": "{}"})
    text, meta = db.updates[-1][1][0], chat_mod.json_loads(db.updates[-1][1][1], {})
    assert text.startswith("Focus: ship the kernel UI")
    hits = [c for c in meta["cards"] if c["kind"] == "hits"]
    assert [h["path"] for h in hits[0]["items"]] == ["notes/ideas.md"]
    assert '"now"' in seen["user"] and "never label them UTC" in seen["system"]
    assert "respond_to_user" not in seen["user"]


def test_email_topic_search_and_follow_up():
    from agentic_os.services.mail import html_to_text, search_alternatives, search_matches
    p = RulesPlanner().match("CHECK EMAILS RELATED TO HACKATHONS , ESPECIALLY VOYAGE HACK", offline=False)
    assert p["steps"][0]["tool"] == "email_search" and p["steps"][0]["args"]["query"] == "HACKATHONS, VOYAGE HACK"
    ctx = CONTEXT_MARK + "\nassistant: 20 message(s), 20 unread."
    assert RulesPlanner().match("WHICH ARE OF VOYAGE HACK" + ctx, offline=False)["steps"][0]["args"]["query"] == "VOYAGE HACK"
    assert RulesPlanner().match("which is the best framework?" + ctx, offline=False) is None
    alts = search_alternatives("HACKATHONS, VOYAGE HACK")
    assert search_matches("1 Day Left: Round 1 | TBO VOYAGEHACK 4.0", alts)
    assert search_matches("Join our hackathon", alts) and not search_matches("Founders' Office Intern", alts)
    assert html_to_text("<!DOCTYPE html><html><head><title>t</title><style>x{}</style></head><body><p>Hi&nbsp;there</p></body></html>") == "Hi there"


def test_keep_used_narrows_emails_only_when_ids_given():
    cards = [{"kind": "emails", "items": [{"id": "INBOX:1"}, {"id": "INBOX:2"}]}]
    assert _keep_used(cards, ["INBOX:2"])[0]["items"] == [{"id": "INBOX:2"}]
    assert len(_keep_used(cards, [])[0]["items"]) == 2
