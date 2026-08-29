"""Offline safety and the fail-soft response contract.

Run with:

    py -m unittest discover -s tests -t . -v

Every test in this file runs with network access hard-blocked -- the guard is installed
at import time, before ``starter`` (and therefore numpy/scipy/scikit-learn) is imported,
so the whole stack is loaded and exercised under the block.

What is being asserted, in one line: **``respond()`` returns a contract-valid dict for
every input we could think of, and never raises.** The evaluator scores a raised
exception or a malformed payload as an outright miss
(``evaluator/local_evaluator.py:239-244``), so each of these cases is worth a session.

This suite deliberately does *not* check retrieval quality -- that is what
``py -m evaluator.local_evaluator`` and ``py tools/offline_check.py`` are for.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools import offline_guard  # noqa: E402

# Before any starter import, so scikit-learn is imported under the block too.
offline_guard.install()

from starter import offline  # noqa: E402
from starter.agent import Agent  # noqa: E402

CATALOG = REPO_ROOT / "data" / "catalog.jsonl"

# Enough real catalog rows for TF-IDF + a 75-component SVD to fit (DENSE_COMPONENTS must
# stay below the feature count), while keeping construction under a second. Using real
# rows rather than synthetic ones means the tests exercise the real field shapes.
MINI_CATALOG_ROWS = 200

# Modules that would mean the agent can talk to the network at all. `starter/` must not
# import any of them, directly or under an alias.
NETWORK_MODULES = {
    "socket", "ssl", "urllib", "http", "ftplib", "smtplib", "poplib", "imaplib",
    "telnetlib", "asyncio", "xmlrpc", "webbrowser", "requests", "httpx", "aiohttp",
    "urllib3", "websockets", "boto3", "openai", "anthropic",
}


def build_mini_catalog(directory: Path) -> Path:
    """A small real-row catalog, so tests cost a second instead of half a minute."""
    path = directory / "mini_catalog.jsonl"
    with CATALOG.open(encoding="utf-8") as source, path.open("w", encoding="utf-8") as sink:
        for index, line in enumerate(source):
            if index >= MINI_CATALOG_ROWS:
                break
            sink.write(line)
    return path


class ResponseContractMixin:
    """Assert a payload satisfies `turn_response` in docs/agent_api_contract.json."""

    def assert_valid_response(self, response: object, top_k: int = 10) -> None:
        self.assertIsInstance(response, dict)
        self.assertEqual(set(response), {"message", "ask_attribute", "recommendations", "usage"})

        self.assertIsInstance(response["message"], str)

        attribute = response["ask_attribute"]
        self.assertTrue(
            attribute is None or attribute in offline.ALLOWED_ATTRIBUTES,
            f"ask_attribute {attribute!r} is outside the contract enum",
        )

        recommendations = response["recommendations"]
        self.assertIsInstance(recommendations, list)
        self.assertLessEqual(len(recommendations), top_k)
        identifiers = []
        for item in recommendations:
            self.assertIsInstance(item, dict)
            self.assertLessEqual(set(item), {"parent_asin", "score"})
            self.assertIsInstance(item["parent_asin"], str)
            self.assertTrue(item["parent_asin"])
            identifiers.append(item["parent_asin"])
        self.assertEqual(len(identifiers), len(set(identifiers)), "duplicate parent_asin")

        usage = response["usage"]
        self.assertIsInstance(usage, dict)
        self.assertEqual(set(usage), {"prompt_tokens", "completion_tokens"})
        for value in usage.values():
            self.assertIsInstance(value, int)
            self.assertGreaterEqual(value, 0)


@unittest.skipUnless(CATALOG.exists(), "data/catalog.jsonl is required")
class AgentUnderNoNetworkTests(ResponseContractMixin, unittest.TestCase):
    """The agent, running with every socket operation blocked."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._directory = tempfile.TemporaryDirectory()
        cls.catalog = build_mini_catalog(Path(cls._directory.name))
        cls.agent = Agent(cls.catalog)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.agent.index is not None:
            cls.agent.index.connection.close()
        cls._directory.cleanup()

    def test_constructs_without_network(self) -> None:
        self.assertTrue(offline_guard.is_installed())
        self.assertIsNone(self.agent.degraded_reason)
        self.assertIsNotNone(self.agent.index)

    def test_dense_route_is_available_offline(self) -> None:
        # The one part of the stack with a third-party dependency. If numpy/scipy/
        # scikit-learn needed the network, this is where it would show.
        self.assertIsNotNone(self.agent.index.dense_index)

    def test_full_session_is_contract_valid(self) -> None:
        self.agent.reset("offline-session", {})
        messages = [
            "I'm looking for a black cotton t-shirt for the gym",
            "I can share: 100% Cotton. Machine wash cold.",
            "I can share: Imported. Lightweight and breathable.",
            "I don't have an additional preference for other.",
        ]
        for turn, message in enumerate(messages, start=1):
            with self.subTest(turn=turn):
                response = self.agent.respond("offline-session", message, turn, 10)
                self.assert_valid_response(response)
                self.assertTrue(response["recommendations"], "an empty page is unscoreable")

    def test_recommendations_are_real_catalog_ids(self) -> None:
        known = {
            json.loads(line)["parent_asin"]
            for line in self.catalog.read_text(encoding="utf-8").splitlines()
        }
        self.agent.reset("catalog-ids", {})
        response = self.agent.respond("catalog-ids", "cotton shirt", 1, 10)
        for item in response["recommendations"]:
            self.assertIn(item["parent_asin"], known)

    def test_hostile_inputs_never_raise(self) -> None:
        # Each of these raised before feature 11; the two marked cases are the ones
        # recorded as known gap 4 in CLAUDE.md.
        cases = [
            ("session id absent -- respond() before reset()", ("never-reset", "hello", 1, 10)),
            ("None message (raised TypeError in observe)", ("s", None, 1, 10)),
            ("non-string message", ("s", 12345, 1, 10)),
            ("bytes message", ("s", b"cotton shirt", 1, 10)),
            ("non-int turn (raised in disclosure_limit)", ("s", "hello", "three", 10)),
            ("None turn", ("s", "hello", None, 10)),
            ("turn zero", ("s", "hello", 0, 10)),
            ("negative turn", ("s", "hello", -5, 10)),
            ("turn past the contract maximum", ("s", "hello", 9999, 10)),
            ("None top_k", ("s", "hello", 1, None)),
            ("negative top_k", ("s", "hello", 1, -1)),
            ("absurd top_k", ("s", "hello", 1, 10_000)),
            # int(float("inf")) raises OverflowError, not ValueError -- this escaped an
            # earlier version of the coercion.
            ("infinite top_k", ("s", "hello", 1, float("inf"))),
            ("nan top_k", ("s", "hello", 1, float("nan"))),
            ("infinite turn", ("s", "hello", float("inf"), 10)),
            ("non-string session id", (None, "hello", 1, 10)),
            ("empty message", ("s", "", 1, 10)),
            ("whitespace message", ("s", "   \n\t ", 1, 10)),
            ("punctuation-only message", ("s", "!!! ??? ***", 1, 10)),
            ("very long message", ("s", "cotton " * 5000, 1, 10)),
        ]
        self.agent.reset("s", {})
        for label, arguments in cases:
            with self.subTest(case=label):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    response = self.agent.respond(*arguments)
                expected_top_k = offline.coerce_top_k(arguments[3])
                self.assert_valid_response(response, expected_top_k)

    def test_top_k_zero_returns_nothing(self) -> None:
        # A caller asking for zero must get zero -- over-delivering is malformed output,
        # and must not trip the empty-page fallback either.
        self.agent.reset("zero", {})
        response = self.agent.respond("zero", "cotton shirt", 1, 0)
        self.assert_valid_response(response, 0)
        self.assertEqual(response["recommendations"], [])

    def test_reset_clears_previous_session(self) -> None:
        self.agent.reset("reused", {})
        self.agent.respond("reused", "black leather boots", 1, 10)
        self.agent.reset("reused", {})
        self.assertNotIn("reused", self.agent._last_good)

    def test_latency_stats_expose_the_canaries(self) -> None:
        stats = self.agent.latency_stats()
        self.assertIn("fallback_turns", stats)
        self.assertIn("degraded_reason", stats)
        self.assertIsNone(stats["degraded_reason"])


@unittest.skipUnless(CATALOG.exists(), "data/catalog.jsonl is required")
class DegradedModeTests(ResponseContractMixin, unittest.TestCase):
    """What happens when the things the agent is built on are not there."""

    def test_missing_catalog_degrades_instead_of_raising(self) -> None:
        # The evaluator builds the Agent once, outside its per-turn try/except, so a
        # constructor that raises costs every session rather than one turn.
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            agent = Agent("no/such/catalog.jsonl")
            agent.reset("gone", {})
            response = agent.respond("gone", "cotton shirt", 1, 10)
        self.assertIsNotNone(agent.degraded_reason)
        self.assert_valid_response(response)
        self.assertEqual(agent.fallback_turns, 1)

    def test_broken_index_still_answers_with_real_products(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = Agent(build_mini_catalog(Path(directory)))
            self.assertTrue(agent._fallback_slate, "slate should be populated at build time")

            # Simulate retrieval failing at turn 2 of a live session, after turn 1 worked.
            agent.reset("broken", {})
            first = agent.respond("broken", "black cotton t-shirt", 1, 10)
            self.assertTrue(first["recommendations"])

            index, agent.index = agent.index, None
            index.connection.close()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                second = agent.respond("broken", "I can share: 100% Cotton.", 2, 10)

            self.assert_valid_response(second)
            self.assertEqual(agent.fallback_turns, 1)
            # Degrades to the previous turn's list, not to nothing.
            self.assertEqual(second["recommendations"], first["recommendations"])
            self.assertIn("catalog index unavailable", stderr.getvalue())

    def test_fallback_slate_used_when_there_is_no_earlier_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = Agent(build_mini_catalog(Path(directory)))
            slate = list(agent._fallback_slate)
            index, agent.index = agent.index, None
            index.connection.close()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                response = agent.respond("cold-start", "anything", 1, 10)
            self.assert_valid_response(response)
            self.assertEqual(response["recommendations"], slate)
            self.assertEqual(response["ask_attribute"], "other")


class NoNetworkImportTests(unittest.TestCase):
    """Static proof, independent of what any particular run happens to execute."""

    def test_starter_imports_nothing_networked(self) -> None:
        for path in sorted((REPO_ROOT / "starter").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    root = name.split(".")[0]
                    with self.subTest(module=path.name, imported=name):
                        self.assertNotIn(root, NETWORK_MODULES)


class OfflineGuardTests(unittest.TestCase):
    """The guard itself, since every other test's meaning depends on it working."""

    def test_socket_construction_is_blocked(self) -> None:
        import socket

        with self.assertRaises(offline_guard.NetworkAccessAttempted):
            socket.socket()

    def test_name_resolution_is_blocked(self) -> None:
        import socket

        with self.assertRaises(offline_guard.NetworkAccessAttempted):
            socket.getaddrinfo("example.com", 80)

    def test_urlopen_is_blocked(self) -> None:
        import urllib.request

        with self.assertRaises(offline_guard.NetworkAccessAttempted):
            urllib.request.urlopen("http://example.com")

    def test_ssl_still_imports(self) -> None:
        # Regression: blocking `socket.socket` with a plain function breaks
        # `class SSLSocket(socket)` in ssl.py, which silently takes scikit-learn --
        # and so the whole dense route -- down with it.
        import ssl

        self.assertTrue(hasattr(ssl, "SSLSocket"))


class CoercionTests(unittest.TestCase):
    """starter/offline.py in isolation. Identity on good input is the important half."""

    def test_well_formed_response_passes_through_unchanged(self) -> None:
        payload = {
            "message": "Here are some options.",
            "ask_attribute": "other",
            "recommendations": [{"parent_asin": "B001"}, {"parent_asin": "B002"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }
        self.assertEqual(offline.coerce_response(payload, 10), payload)

    def test_out_of_enum_attribute_becomes_null(self) -> None:
        self.assertIsNone(offline.coerce_attribute("colour"))
        self.assertIsNone(offline.coerce_attribute(7))
        self.assertEqual(offline.coerce_attribute("material"), "material")

    def test_recommendations_are_cleaned(self) -> None:
        messy = [
            {"parent_asin": "B001"},
            {"parent_asin": "B001"},          # duplicate
            {"parent_asin": "  B002  "},      # padded
            {"parent_asin": ""},              # empty
            {"nope": "B003"},                 # missing key
            "B004",                           # bare id
            None,
            {"parent_asin": "B005", "score": 0.5, "junk": "dropped"},
        ]
        self.assertEqual(
            offline.coerce_recommendations(messy, 10),
            [
                {"parent_asin": "B001"},
                {"parent_asin": "B002"},
                {"parent_asin": "B004"},
                {"parent_asin": "B005", "score": 0.5},
            ],
        )

    def test_recommendations_respect_top_k(self) -> None:
        payload = [{"parent_asin": f"B{index}"} for index in range(50)]
        self.assertEqual(len(offline.coerce_recommendations(payload, 10)), 10)
        self.assertEqual(offline.coerce_recommendations(payload, 0), [])
        self.assertEqual(offline.coerce_recommendations("not a list", 10), [])

    def test_turn_and_top_k_coercion(self) -> None:
        self.assertEqual(offline.coerce_turn(3), 3)
        self.assertEqual(offline.coerce_turn("4"), 4)
        self.assertEqual(offline.coerce_turn(0), 1)
        self.assertEqual(offline.coerce_turn(-2), 1)
        self.assertEqual(offline.coerce_turn(None), 1)
        self.assertEqual(offline.coerce_turn("nonsense"), 1)
        self.assertEqual(offline.coerce_turn(float("inf")), 1)   # OverflowError
        self.assertEqual(offline.coerce_turn(float("nan")), 1)   # ValueError
        self.assertEqual(offline.coerce_top_k(10), 10)
        self.assertEqual(offline.coerce_top_k(0), 0)
        self.assertEqual(offline.coerce_top_k(-3), 0)
        self.assertEqual(offline.coerce_top_k(None), 10)
        self.assertEqual(offline.coerce_top_k(10_000), offline.MAX_RECOMMENDATIONS)
        self.assertEqual(offline.coerce_top_k(float("inf")), 10)
        self.assertEqual(offline.coerce_top_k(float("nan")), 10)

    def test_usage_rejects_bad_counters(self) -> None:
        self.assertEqual(offline.coerce_usage({"prompt_tokens": 5, "completion_tokens": 7}),
                         {"prompt_tokens": 5, "completion_tokens": 7})
        # bool is an int subclass; True as a token count is a bug, not a count.
        self.assertEqual(offline.coerce_usage({"prompt_tokens": True, "completion_tokens": -1}),
                         {"prompt_tokens": 0, "completion_tokens": 0})
        self.assertEqual(offline.coerce_usage(None), {"prompt_tokens": 0, "completion_tokens": 0})

    def test_usage_is_a_fresh_dict_each_time(self) -> None:
        first = offline.coerce_usage(None)
        first["prompt_tokens"] = 999
        self.assertEqual(offline.coerce_usage(None)["prompt_tokens"], 0)
        self.assertEqual(offline.NO_MODEL_USAGE["prompt_tokens"], 0)

    def test_message_coercion(self) -> None:
        self.assertEqual(offline.coerce_user_message("hi"), "hi")
        self.assertEqual(offline.coerce_user_message(None), "")
        self.assertEqual(offline.coerce_user_message(42), "42")

    def test_fallback_slate_from_a_missing_catalog_is_empty_not_fatal(self) -> None:
        self.assertEqual(offline.catalog_fallback_asins("no/such/file.jsonl"), [])

    @unittest.skipUnless(CATALOG.exists(), "data/catalog.jsonl is required")
    def test_fallback_slate_reads_without_sqlite(self) -> None:
        identifiers = offline.catalog_fallback_asins(CATALOG, 10)
        self.assertEqual(len(identifiers), 10)
        self.assertTrue(all(isinstance(value, str) and value for value in identifiers))


if __name__ == "__main__":
    unittest.main()
