"""Feature verification harness.

Exercises every feature named in the delegation deck (Tiers 0-3) against the real
50k catalog, plus contract compliance and robustness. One Agent is built and reused,
so the ~13s index construction is paid once.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starter import retrieval
from starter.agent import Agent, disclosure_limit, NO_MODEL_USAGE
from starter.dialog_state import DialogState, detect_constraints, ASK_ORDER
from starter.ranking import Reranker

RESULTS = []


def check(tier, name, ok, detail="", known_gap=False):
    """`known_gap=True` marks a documented, deliberate shortfall.

    Those are reported as XFAIL and excluded from the exit code, so this stays usable as
    a pre-submission gate: it goes red on a *regression*, not on a decision the team has
    already taken and written down (see "Known gaps" in CLAUDE.md).
    """
    ok = bool(ok)
    RESULTS.append((tier, name, ok, detail, known_gap))
    label = "PASS" if ok else ("XFAIL" if known_gap else "FAIL")
    print("  [" + label + "] " + name + ("  -- " + detail if detail else ""))


def raises(fn):
    try:
        fn()
        return None
    except Exception as exc:
        return type(exc).__name__


VALID_ATTRS = {"category", "material", "color", "size", "style", "brand",
               "budget", "feature", "use_case", "other", None}

print("Building agent (index over 50k catalog)...")
t0 = time.perf_counter()
agent = Agent("data/catalog.jsonl")
print("  built in %.2fs\n" % (time.perf_counter() - t0))

catalog_ids = set(agent.index.rowid_by_asin)

# ---------------------------------------------------------------- TIER 0
print("TIER 0 -- Baseline")

agent.reset("t0", {"preference_tags": ["fit"], "category_bucket": "clothing"})
r = agent.respond("t0", "I'm looking for a cotton t-shirt for the gym", 1, 10)

check("T0", "respond() returns exactly the 4 contract keys",
      set(r) == {"message", "ask_attribute", "recommendations", "usage"},
      "keys=" + str(sorted(r)))
check("T0", "message is a str", isinstance(r["message"], str), repr(r["message"])[:70])
check("T0", "ask_attribute in the published enum", r["ask_attribute"] in VALID_ATTRS,
      repr(r["ask_attribute"]))
check("T0", "usage has prompt_tokens+completion_tokens ints",
      isinstance(r["usage"], dict)
      and set(r["usage"]) == {"prompt_tokens", "completion_tokens"}
      and all(isinstance(v, int) and v >= 0 for v in r["usage"].values()),
      str(r["usage"]))
check("T0", "usage is a fresh dict per turn (not the shared module object)",
      r["usage"] is not NO_MODEL_USAGE)
recs = r["recommendations"]
check("T0", "recommendations is a list of {parent_asin}",
      isinstance(recs, list) and all(isinstance(x, dict) and set(x) == {"parent_asin"} for x in recs),
      "n=" + str(len(recs)))
check("T0", "every recommended ASIN exists in the catalog",
      all(x["parent_asin"] in catalog_ids for x in recs))
check("T0", "recommendations are unique",
      len({x["parent_asin"] for x in recs}) == len(recs))

# ---------------------------------------------------------------- TIER 1
print("\nTIER 1 -- Must-have (HitRate@10)")

browse = DialogState()
browse.observe("I need a jacket for hiking", 1)
buy = DialogState()
buy.observe("I need a black leather jacket under $80", 1)
check("T1", "browsing track: no constraint -> is_buying False, no AND terms, no price filter",
      browse.is_buying is False and browse.and_terms() == [] and browse.price_max() is None)
check("T1", "buying track: constraints -> is_buying True, AND terms + price ceiling",
      buy.is_buying is True and set(buy.and_terms()) == {"black", "leather"} and buy.price_max() == 80.0,
      "and=" + str(buy.and_terms()) + " price=" + str(buy.price_max()))
check("T1", "price regex ignores measurements ('up to 8-inch wrist')",
      detect_constraints("fits up to 8-inch wrist circumference")["price_max"] is None)
check("T1", "price regex still catches a real ceiling ('under $45')",
      detect_constraints("something under $45")["price_max"] == 45.0)

calls = []
orig_rq = retrieval.CatalogIndex.run_ranked_query


def spy(self, expr, price_max, limit):
    calls.append(expr)
    return orig_rq(self, expr, price_max, limit)


retrieval.CatalogIndex.run_ranked_query = spy
agent.reset("t1r", {})
agent.respond("t1r", "I'm looking for a wool blend overcoat", 1, 10)
agent.respond("t1r", "For that, what matters is: 100% Wool; Button closure; Machine Wash", 2, 10)
retrieval.CatalogIndex.run_ranked_query = orig_rq

kw = [c for c in calls if "categories:" not in c and not c.startswith('"')]
cat = [c for c in calls if "categories:" in c]
phr = [c for c in calls if c.startswith('"')]
check("T1", "route 1 keyword fires every turn", len(kw) >= 2, str(len(kw)) + " calls")
check("T1", "route 2 category-scoped fires every turn", len(cat) >= 2, str(len(cat)) + " calls")
check("T1", "route 3 phrase routes fire once disclosures land", len(phr) >= 1,
      str(len(phr)) + " phrase queries e.g. " + str(phr[:2]))
check("T1", "route 4 dense (LSA) index is live", agent.index.dense_index is not None)

agree = agent.index.fuse_rankings([(["A", "B", "C"], 1.0), (["C", "B"], 0.3)], 3)
check("T1", "RRF: an item surfaced by two routes outranks one surfaced by a single route",
      agree == ["B", "C", "A"], str(agree))
disjoint = agent.index.fuse_rankings([(["K1", "K2", "K3"], 1.0), (["X1", "X2", "X3"], 0.3)], 6)
check("T1", "RRF: a 0.3-weighted route alone can never outrank the keyword route",
      disjoint == ["K1", "K2", "K3", "X1", "X2", "X3"], str(disjoint))
order_kept = agent.index.fuse_rankings([(["A", "B", "C"], 1.0)], 3)
check("T1", "RRF preserves within-route order when only one route fires",
      order_kept == ["A", "B", "C"], str(order_kept))

agent.reset("t1s", {})
s = agent._sessions["t1s"]
agent.respond("t1s", "I want running shoes", 1, 10)
after1 = list(s.evidence)
agent.respond("t1s", "For that, what matters is: Rubber sole; breathable mesh upper", 2, 10)
after2 = list(s.evidence)
check("T1", "evidence accumulates across turns (turn-1 category retained)",
      len(after2) > len(after1) and after2[0] == after1[0],
      str(len(after1)) + " -> " + str(len(after2)) + " entries")
check("T1", "disclosures are split into individual phrases for ranking",
      "Rubber sole" in s.evidence_phrases() and "breathable mesh upper" in s.evidence_phrases(),
      str(s.evidence_phrases()))
agent.reset("t1s2", {})
check("T1", "sessions are isolated by session_id",
      agent._sessions["t1s2"].evidence == [] and len(agent._sessions["t1s"].evidence) == 2)

agent.reset("t1c", {})
asked = []
msg = "I need a belt"
for turn in range(1, 6):
    resp = agent.respond("t1c", msg, turn, 10)
    asked.append(resp["ask_attribute"])
    msg = "I don't have an additional preference for " + str(resp["ask_attribute"]) + "."
check("T1", "ask_attribute is never null (a null turn reveals nothing)",
      all(a is not None for a in asked), str(asked))
check("T1", "exhausted attributes retire, so the question rotates through ASK_ORDER",
      asked == list(ASK_ORDER[:5]), str(asked))
check("T1", "the clarifying question is present in the customer-facing message",
      "?" in agent.respond("t1c", "For that, what matters is: leather", 6, 10)["message"])

d = DialogState()
d.observe("I need a hat", 1)
d.next_attribute()
d.observe("I don't have a preference for color; please use your judgment.", 2)
check("T1", "DECLINE ('a preference') does NOT retire the attribute",
      d.exhausted == set(), str(d.exhausted))
d.observe("I don't have an additional preference for color.", 3)
check("T1", "EXHAUSTED ('an additional preference') DOES retire it",
      d.exhausted == {"color"}, str(d.exhausted))

# ---------------------------------------------------------------- TIER 2
print("\nTIER 2 -- High-value (MRR)")

dense = agent.index.dense_index
top = dense.top_k("blue cotton summer dress", 5)
check("T2", "dense route returns real catalog ASINs",
      len(top) == 5 and all(t in catalog_ids for t in top))
check("T2", "dense degrades to [] on an empty query", dense.top_k("", 5) == [])
sims = dense.similarity_scores("leather boots", top)
check("T2", "dense similarity_scores are clipped to [0, inf)",
      all(v >= 0.0 for v in sims.values()) and len(sims) == 5)

rr = Reranker(agent.index)
pool = [x["parent_asin"] for x in agent.index.retrieve(
    retrieval.terms("wool overcoat button closure"), [], None, 120)]
ordered = rr.order(pool, ["100% Wool", "Button closure"])
check("T2", "reranker never drops or adds a candidate",
      sorted(ordered) == sorted(pool) and len(ordered) == len(pool), "pool=" + str(len(pool)))
check("T2", "reranker actually reorders (it is not a pass-through)", ordered != pool)
check("T2", "reranker is a no-op when there is no phrase evidence", rr.order(pool, []) == pool)

ov = DialogState()
ov.observe("I want a red cotton shirt under $40", 1)
before = dict(ov.slots)
ov.observe("Actually, ignore my earlier preference. What I need is: black leather jacket", 3)
check("T2", "override clears ALL slots (incl. price_max) then refills from the new message",
      before == {"price_max": 40.0, "color": "red", "material": "cotton"}
      and ov.slots == {"price_max": None, "color": "black", "material": "leather"},
      str(before) + " -> " + str(ov.slots))
check("T2", "override keeps accumulated evidence (retracting it was measured as a loss)",
      len(ov.evidence) == 2 and "red cotton shirt" in ov.evidence[0])

nonov = DialogState()
nonov.observe("I want a blue shirt", 1)
nonov.observe("For that, what matters is: green", 2)
check("T2", "first-write-wins still holds for a NON-override contradiction",
      nonov.slots["color"] == "blue", str(nonov.slots["color"]))

free = DialogState()
free.observe("I want a blue shirt", 1)
free.next_attribute()
free.observe("actually make it red", 2)
check("T2", "free-form (human) correction overwrites the slot last-write-wins",
      free.slots["color"] == "red", str(free.slots["color"]))
check("T2", "free-form correction scrubs the superseded value from evidence AND phrases",
      not any("blue" in e.lower() for e in free.evidence)
      and not any("blue" in p.lower() for p in free.phrases),
      str(free.evidence))
check("T2", "free-form reply retires the attribute just answered (question rotates)",
      free.exhausted == {"other", "color"}, str(free.exhausted))
# "color" is in there because the slot is now filled -- see the next check. Asking for a
# value we have already been told is the bug this pins, not an over-eager retirement.

known = DialogState()
known.observe("i need a jacket", 1)
known.next_attribute()
known.observe("i want it in grey", 2)
check("T2", "free-form: a filled slot retires its own question (no re-asking what we know)",
      "color" in known.exhausted and known.next_attribute() != "color", str(known.exhausted))

neg = DialogState()
neg.observe("i need a jacket", 1)
neg.next_attribute()
neg.observe("i want it to be grey and not fully polyester", 2)
check("T2", "free-form negation is an exclusion, not a requirement",
      neg.slots["color"] == "grey" and neg.slots["material"] is None
      and neg.avoided == ["polyester"], str(neg.slots) + " avoid=" + str(neg.avoided))
check("T2", "a ruled-out value is scrubbed from evidence and phrases, the rest survives",
      not any("polyester" in t.lower() for t in neg.evidence + neg.phrases)
      and any("grey" in t.lower() for t in neg.phrases), str(neg.phrases))
check("T2", "the exclusion is said out loud rather than silently inverted",
      "avoiding polyester" in neg.message("style").lower(), neg.message("style"))
check("T2", "negation is free-form only -- the scored vocabulary is untouched",
      detect_constraints("what matters is: not applicable cotton")["material"] == "cotton"
      and detect_constraints("no rush, I want a black leather belt",
                             extended=True)["material"] == "leather")

pool = agent.index.retrieve(retrieval.terms("polyester jacket"), [], None, 10)
pool_ids = [row["parent_asin"] for row in pool]
demoted = agent.index.demote_terms(pool_ids, ["polyester"])
kept = [pid for pid in pool_ids
        if " polyester " not in agent.index.document_profile(pid)[1]]
check("T2", "a ruled-out value is demoted in the results, and nothing is dropped",
      sorted(demoted) == sorted(pool_ids) and demoted[:len(kept)] == kept,
      "%d of %d demoted" % (len(pool_ids) - len(kept), len(pool_ids)))

STARTER = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / "starter"
prof_used = [m for m in ["dialog_state.py", "ranking.py", "retrieval.py", "dense_retrieval.py"]
             if "user_profile" in (STARTER / m).read_text(encoding="utf-8")]
check("T2", "personalization from user_profile is NOT wired (documented decision, not a bug)",
      not prof_used, "reset() discards the profile; no other module reads it")

# ---------------------------------------------------------------- TIER 3
print("\nTIER 3 -- Polish & efficiency")

schedule = [disclosure_limit(t, 10, True) for t in range(1, 7)]
check("T3", "disclosure schedule widens 1 -> 1 -> 4 -> 8 -> 10", schedule == [1, 1, 4, 8, 10, 10],
      str(schedule))
check("T3", "full list goes out the moment no more evidence is coming",
      disclosure_limit(1, 10, False) == 10)
check("T3", "disclosure never exceeds top_k", disclosure_limit(5, 3, True) == 3)

# ------------------------------------------------------ Generic facets
print("\nFACETS -- arbitrary parameters on the free-form path")

from starter import facets as F  # noqa: E402

fx = DialogState()
fx.observe("men tshirt", 1)
fx.next_attribute()
fx.observe("round neck, blue, cotton, under 50 dollars, men tshirt", 2)
check("FACET", "gender, neckline and the three slots are all captured",
      fx.facet_values().get("gender") == "men"
      and fx.facet_values().get("neckline") == "crew neck"
      and fx.slots == {"price_max": 50.0, "color": "blue", "material": "cotton"},
      str(fx.facet_values()))
check("FACET", "budget WORDS leave the query, the numeric ceiling stays",
      "dollars" not in fx.evidence_text() and "under 50" not in fx.evidence_text()
      and fx.price_max() == 50.0, repr(fx.evidence_text()))
check("FACET", "product words survive the budget strip",
      all(w in fx.evidence_text() for w in ["round neck", "blue", "cotton", "men tshirt"]),
      repr(fx.evidence_text()))
check("FACET", "the message names every active constraint, not just the three slots",
      all(w in fx.message(None) for w in ["men", "crew neck", "blue", "cotton", "$50.00"]),
      fx.message(None))

op = DialogState(freeform=True)
op.observe("mens v neck slim fit t shirt, not polyester, under 30 dollars", 1)
check("FACET", "the OPENING message gets facets too when a human is typing",
      op.facet_values() == {"gender": "men", "neckline": "v neck", "fit": "slim"},
      str(op.facet_values()))
check("FACET", "turn-1 negation and budget both handled on the free-form path",
      op.avoid_terms() == ["polyester"] and op.price_max() == 30.0
      and "dollars" not in op.evidence_text(), repr(op.evidence_text()))

scored_open = DialogState()
scored_open.observe("mens v neck slim fit t shirt, not polyester, under 30 dollars", 1)
check("FACET", "the SCORED opener is untouched: no facets, no exclusions, raw evidence",
      scored_open.facet_values() == {} and scored_open.avoid_terms() == []
      and scored_open.evidence == ["mens v neck slim fit t shirt, not polyester, under 30 dollars"],
      str(scored_open.evidence))
check("FACET", "Agent defaults to freeform=False; only the WebUI opts in",
      agent.freeform is False)

check("FACET", "stating a value implies rejecting its siblings",
      set(F.sibling_forms({"gender": "men"})) >= {"women", "ladies", "girls"}
      and "men" not in F.sibling_forms({"gender": "men"}))
check("FACET", "an unmentioned group contributes no siblings (silence is not a preference)",
      F.sibling_forms({"gender": "men"}) == F.sibling_forms({"gender": "men"})
      and not any(f in F.sibling_forms({"gender": "men"}) for f in ["v neck", "slim fit"]))
check("FACET", "facet queries are TITLE-scoped (spam 'for men women teens' must not match)",
      'title:"men"' in F.title_expression({"gender": "men"}),
      F.title_expression({"gender": "men"}))
check("FACET", "multi-word forms become phrase queries",
      'title:"long sleeve"' in F.title_expression({"sleeve": "long sleeve"}),
      F.title_expression({"sleeve": "long sleeve"}))
check("FACET", "no facets stated -> empty expression and no siblings",
      F.title_expression({}) == "" and F.sibling_forms({}) == [] and F.detect_facets("") == {})
check("FACET", "adding a parameter is a dictionary entry, not new code",
      len(F.FACET_GROUPS) >= 8 and "neckline" in F.FACET_GROUPS and "fit" in F.FACET_GROUPS,
      "%d groups: %s" % (len(F.FACET_GROUPS), ", ".join(sorted(F.FACET_GROUPS))))

# The WebUI's display-only second pass must apply every constraint the agent applied.
# When it does not, the consistency guard in `_deep_list` fires and the page collapses to
# the single disclosed row -- which is exactly how the missing `avoid_terms` (feature 15)
# and `facets` were found. Compared structurally so a future kwarg cannot be forgotten.
import ast as _ast  # noqa: E402


def _retrieve_kwargs(path, function_name):
    tree = _ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef) and node.name == function_name:
            for call in _ast.walk(node):
                if isinstance(call, _ast.Call) and getattr(
                    getattr(call.func, "attr", None), "__str__", lambda: ""
                )() == "retrieve":
                    return {kw.arg for kw in call.keywords}
    return set()


_repo = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_agent_kwargs = _retrieve_kwargs(_repo / "starter" / "agent.py", "_respond")
_deep_kwargs = _retrieve_kwargs(_repo / "webui" / "agent_bridge.py", "_deep_list")
check("FACET", "the WebUI deep list passes every constraint the agent does",
      _agent_kwargs and _agent_kwargs <= _deep_kwargs,
      "agent=%s missing from deep list=%s" % (
          sorted(_agent_kwargs), sorted(_agent_kwargs - _deep_kwargs)))

demoted = agent.index.demote_title_forms(
    ["__missing_a__", "__missing_b__"], F.sibling_forms({"gender": "men"}))
check("FACET", "demotion never drops a candidate, only reorders",
      sorted(demoted) == ["__missing_a__", "__missing_b__"], str(demoted))

fq = DialogState()
fq.observe("men tshirt", 1)
fq.next_attribute()
fq.observe("round neck, blue, cotton, under 50 dollars, men tshirt", 2)
res = agent.index.retrieve(
    retrieval.terms(fq.evidence_text()), fq.and_terms(), fq.price_max(), 10,
    reranker=lambda p: agent.reranker.order(p, fq.evidence_phrases()),
    phrases=fq.evidence_phrases(), avoid_terms=fq.avoid_terms(), facets=fq.facet_values())
titles = []
for row in res:
    titles.append(agent.index.connection.execute(
        "SELECT title FROM products WHERE rowid = ?",
        (agent.index.rowid_by_asin[row["parent_asin"]],)).fetchone()[0].lower())
mens = sum(1 for t in titles if "men" in t and "women" not in t)
check("FACET", "asking for men's clothing returns men's clothing", mens >= 7,
      "%d of %d titles are men's (was 0 of 10 before facets)" % (mens, len(titles)))

# ------------------------------------------- The score-isolation invariant
#
# Every free-form-only feature (11, 12, 15, facets) is score-neutral *by construction*
# rather than by measurement, and the whole argument rests on one fact: no simulator reply
# ever reaches `_observe_freeform`. That is a property of the regexes, and a future edit
# widening one of them would silently put free-form code on the scored path. So assert it
# rather than trusting it -- this check is what keeps the guarantee true over time.
print("\nSCORE ISOLATION -- free-form code must be unreachable while scoring")

from evaluator.local_evaluator import catalog_index, evaluate, load_jsonl  # noqa: E402
from starter import dialog_state as _ds  # noqa: E402

_reached = {"freeform": 0, "observe": 0}
_orig_ff, _orig_ob = _ds.DialogState._observe_freeform, _ds.DialogState.observe


def _spy_ff(self, message):
    _reached["freeform"] += 1
    return _orig_ff(self, message)


def _spy_ob(self, message, turn):
    _reached["observe"] += 1
    return _orig_ob(self, message, turn)


_ds.DialogState._observe_freeform = _spy_ff
_ds.DialogState.observe = _spy_ob
try:
    _samples = load_jsonl("data/public_set.jsonl")
    _cids, _cats, _prods = catalog_index("data/catalog.jsonl")
    _res = evaluate(Agent("data/catalog.jsonl"), _samples, _cids, _cats, _prods)
finally:
    _ds.DialogState._observe_freeform = _orig_ff
    _ds.DialogState.observe = _orig_ob

check("ISO", "the scored run actually exercised the dialog state",
      _reached["observe"] > 500, "%d observe() calls" % _reached["observe"])
check("ISO", "ZERO simulator replies reach the free-form branch",
      _reached["freeform"] == 0,
      "%d free-form calls (must be 0, or free-form features can move the score)"
      % _reached["freeform"])
check("ISO", "a full scored run still reproduces the score of record",
      _res["recommended_technical_score"] == 0.912205,
      str(_res["recommended_technical_score"]))

_iso_state = DialogState()
_iso_state.observe("I need a jacket", 1)
_iso_state.observe("For that, what matters is: 100% Wool; Button closure", 2)
check("ISO", "facets stay empty when only the simulator has spoken",
      _iso_state.facet_values() == {} and _iso_state.avoid_terms() == [],
      str(_iso_state.facet_values()))

print("")
check("T3", "no model is configured by default -- the judged configuration is offline",
      agent.llm is None and agent.llm_mode == "off",
      "llm=%r mode=%r" % (agent.llm, agent.llm_mode))
check("T3", "model_stats() records the absence explicitly",
      agent.model_stats() == {"enabled": False, "mode": "off"}, str(agent.model_stats()))

stats = agent.latency_stats()
check("T3", "latency_stats() reports real per-turn timings",
      stats["turns"] > 0 and stats["mean_ms"] > 0 and "p95_ms" in stats and "max_ms" in stats,
      json.dumps(stats))
check("T3", "usage reports honest zeros (no model call is made anywhere)",
      r["usage"] == {"prompt_tokens": 0, "completion_tokens": 0})

import socket

attempts = []
real_socket, real_conn, real_gai = socket.socket, socket.create_connection, socket.getaddrinfo


def no_socket(*a, **k):
    attempts.append(a)
    raise OSError("network disabled by verification harness")


socket.socket = no_socket
socket.create_connection = no_socket
socket.getaddrinfo = no_socket
try:
    agent.reset("t3net", {})
    off1 = agent.respond("t3net", "I need a winter scarf", 1, 10)
    off2 = agent.respond("t3net", "For that, what matters is: 100% Cashmere; Dry Clean Only", 2, 10)
    offline_ok = len(off1["recommendations"]) > 0 and len(off2["recommendations"]) > 0
    offline_err = ""
except Exception as exc:
    offline_ok, offline_err = False, repr(exc)
finally:
    socket.socket, socket.create_connection, socket.getaddrinfo = real_socket, real_conn, real_gai
check("T3", "agent runs fully with the network disabled", offline_ok,
      offline_err or "zero socket attempts (" + str(len(attempts)) + ")")


class Boom:
    def top_k(self, *a, **k):
        raise RuntimeError("dense exploded")

    def similarity_scores(self, *a, **k):
        raise RuntimeError("dense exploded")


saved = agent.index.dense_index
agent.index.dense_index = Boom()
try:
    got = agent.index.retrieve(retrieval.terms("cotton shirt"), [], None, 10,
                               reranker=lambda p: agent.reranker.order(p, ["cotton shirt"]),
                               phrases=["cotton shirt"])
    check("T3", "a broken dense index costs only that route, not the turn", len(got) == 10)
finally:
    agent.index.dense_index = saved


def boom_rerank(pool):
    raise RuntimeError("rerank boom")


got2 = agent.index.retrieve(retrieval.terms("cotton shirt"), [], None, 10, reranker=boom_rerank)
check("T3", "a broken reranker costs ordering, not the turn", len(got2) == 10)

narrow = agent.index.retrieve(retrieval.terms("shirt"), ["magenta", "cashmere"], 3.0, 10)
check("T3", "over-narrow hard filters are backfilled to a full list", len(narrow) == 10,
      "n=" + str(len(narrow)))

# ------------------------------------------------ Robustness (known gap 4)
print("\nROBUSTNESS -- malformed input (documented known gap 4)")

check("GAP", "respond() before reset() raises RuntimeError (by design)",
      raises(lambda: agent.respond("never-reset", "hi", 1, 10)) == "RuntimeError",
      "documented; organizer evaluator catches it")
agent.reset("t4", {})
e1 = raises(lambda: agent.respond("t4", None, 1, 10))
check("GAP", "respond(user_message=None) does not raise", e1 is None, e1 or "handled",
      known_gap=True)
agent.reset("t5", {})
e2 = raises(lambda: agent.respond("t5", "a belt", "2", 10))
check("GAP", "respond(turn='2') does not raise", e2 is None, e2 or "handled",
      known_gap=True)
agent.reset("t6", {})
e3 = raises(lambda: agent.respond("t6", "", 1, 10))
check("GAP", "respond(user_message='') returns a valid shape", e3 is None, e3 or "handled")

# ---------------------------------------------------------------- TOOLING
print("\nTOOLING & WEBUI")

import importlib
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for mod in ["webui.agent_bridge", "webui.catalog", "webui.target", "webui.server"]:
    err = raises(lambda m=mod: importlib.import_module(m))
    check("TOOL", "%s imports cleanly" % mod, err is None, err or "")

for tool in ["tools/score_delta.py", "tools/feasibility_report.py", "tools/sweep_constants.py"]:
    proc = subprocess.run(["py", "-X", "utf8", "-c",
                           "import ast,io,sys; ast.parse(io.open(sys.argv[1],encoding='utf-8').read())",
                           tool], cwd=REPO, capture_output=True, text=True)
    check("TOOL", "%s parses" % tool, proc.returncode == 0, proc.stderr.strip()[:120])

proc = subprocess.run(["py", "-X", "utf8", "tools/sweep_constants.py", "--list"],
                      cwd=REPO, capture_output=True, text=True)
check("TOOL", "sweep_constants.py --list runs", proc.returncode == 0,
      proc.stdout.strip().replace("\n", " / ")[:150] or proc.stderr.strip()[:150])

# ---------------------------------------------------------------- SUMMARY
print("\n" + "=" * 72)
passed = sum(1 for row in RESULTS if row[2])
regressions = [row for row in RESULTS if not row[2] and not row[4]]
expected = [row for row in RESULTS if not row[2] and row[4]]
print("%d/%d checks passed" % (passed, len(RESULTS)))
for tier, name, _, detail, _ in expected:
    print("  XFAIL [%s] %s -- %s (documented known gap, not a regression)" % (tier, name, detail))
for tier, name, _, detail, _ in regressions:
    print("  FAIL  [%s] %s -- %s" % (tier, name, detail))
print("=" * 72)
raise SystemExit(1 if regressions else 0)
