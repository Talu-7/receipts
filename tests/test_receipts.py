import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from receipts import codegen, llm, stats  # noqa: E402
from receipts.arxiv_src import github_links, parse_arxiv_id  # noqa: E402
from receipts.config import Settings  # noqa: E402
from receipts.pipeline import Pipeline  # noqa: E402


class TestStats(unittest.TestCase):
    def test_reproduced(self):
        v = stats.decide(96.9, [96.5, 96.9, 97.2, 96.8], 4)
        self.assertEqual(v["verdict"], stats.REPRODUCED)

    def test_discrepancy(self):
        v = stats.decide(94.0, [87.0, 88.0, 86.5, 87.5, 87.2], 5)
        self.assertEqual(v["verdict"], stats.DISCREPANCY)
        self.assertFalse(v["claimed_in_range"])

    def test_partial(self):
        # gap of ~2 points: beyond the 1-point tolerance but within 3x of it
        v = stats.decide(94.0, [92.0, 92.2, 91.9, 91.9], 4)
        self.assertEqual(v["verdict"], stats.PARTIAL)
        # a ~4 point gap with tight runs is a discrepancy, not "partial"
        self.assertEqual(stats.decide(94.0, [90.0, 90.5, 89.8, 90.2], 4)["verdict"], stats.DISCREPANCY)

    def test_inconclusive_when_runs_break(self):
        v = stats.decide(94.0, [87.0], 8)  # 1 of 8 produced a number: do not accuse the paper
        self.assertEqual(v["verdict"], stats.INCONCLUSIVE)
        self.assertEqual(stats.decide(94.0, [], 4)["verdict"], stats.INCONCLUSIVE)

    def test_better_than_claimed_is_reproduced(self):
        v = stats.decide(80.0, [85.0, 85.2, 84.9], 3)
        self.assertEqual(v["verdict"], stats.REPRODUCED)
        self.assertIn("better", v["reason"])

    def test_lower_is_better(self):
        # perplexity claimed 20, we get 25: worse
        v = stats.decide(20.0, [25.0, 25.2, 24.8], 3, higher_is_better=False)
        self.assertEqual(v["verdict"], stats.DISCREPANCY)

    def test_noise_widens_tolerance(self):
        v = stats.decide(90.0, [86.0, 92.0, 84.0, 90.0], 4)  # std ~3.6 -> 2*std covers the gap
        self.assertEqual(v["verdict"], stats.REPRODUCED)


class TestHelpers(unittest.TestCase):
    def test_arxiv_id(self):
        self.assertEqual(parse_arxiv_id("https://arxiv.org/abs/2005.14165v4"), "2005.14165")
        self.assertEqual(parse_arxiv_id("1706.03762"), "1706.03762")
        self.assertIsNone(parse_arxiv_id("hello"))

    def test_github_links(self):
        t = "code at https://github.com/foo/bar. also github.com/foo/bar and https://github.com/x/y.git"
        self.assertEqual(github_links(t)[0], "https://github.com/foo/bar")

    def test_extract_json(self):
        self.assertEqual(llm.extract_json("<think>hm</think>sure ```json\n{\"a\": 1}\n``` done"), {"a": 1})
        self.assertEqual(llm.extract_json('prefix {"b": [1,2]} suffix'), {"b": [1, 2]})

    def test_lint_rejects_hardcoded_claim(self):
        bad = 'import json\nprint("RECEIPTS_RESULT " + json.dumps({"value": 94.2}))\n'
        self.assertIn("literal", codegen.lint_script(bad, 94.2))
        good = 'import json\nacc = compute()\nprint("RECEIPTS_RESULT " + json.dumps({"value": acc}))\n'
        self.assertIsNone(codegen.lint_script(good, 94.2))


class TestGuards(unittest.TestCase):
    def test_reduced_scale_run_never_becomes_a_discrepancy(self):
        from receipts.pipeline import guard_reduced_scale
        v = stats.decide(28.4, [3.1, 3.3, 3.0], 3)
        self.assertEqual(v["verdict"], stats.DISCREPANCY)
        guard_reduced_scale(v, [{"deviations": "trained 200 steps instead of 300000; base model"}] * 3)
        self.assertEqual(v["verdict"], stats.INCONCLUSIVE)
        self.assertEqual(v["verdict_at_reduced_scale"], stats.DISCREPANCY)
        self.assertIn("not evidence against the paper", v["reason"])

    def test_full_scale_verdict_is_untouched(self):
        from receipts.pipeline import guard_reduced_scale
        v = stats.decide(94.0, [87.0, 87.5, 86.9], 3)
        guard_reduced_scale(v, [{"variant_x": 1.0}] * 3)
        self.assertEqual(v["verdict"], stats.DISCREPANCY)

    def test_generic_pages_are_not_presented_as_errata(self):
        from receipts.discover import filter_evidence

        class Off:  # no LLM available -> keyword fallback
            available = False

        title = "Attention Is All You Need"
        ev = [
            {"kind": "erratum", "title": "Attention Is All You Need, explained | Medium", "url": "u1", "snippet": "a friendly walkthrough"},
            {"kind": "replication", "title": "Attention Is all you need - Wikipedia", "url": "u2", "snippet": "a 2017 paper"},
            {"kind": "erratum", "title": "Erratum: Attention Is All You Need", "url": "u3", "snippet": "correction to Table 2 of Attention Is All You Need"},
            {"kind": "code", "title": "repo", "url": "u4", "snippet": ""},
        ]
        kept = {e["url"] for e in filter_evidence(Off(), title, ev)}
        self.assertEqual(kept, {"u3", "u4"})

    def test_infeasible_claim_is_not_run_and_receipt_keeps_the_claim(self):
        import receipts.pipeline as P
        s = Settings.load()
        s.nebius_api_key = "x"
        s.out_dir = tempfile.mkdtemp()
        p = P.Pipeline(s)
        claim = {"id": "c1", "statement": "28.4 BLEU", "value": 28.4, "metric": "BLEU", "dataset": "WMT14", "where": "T2",
                 "higher_is_better": True, "tractable": False, "est_gpu_minutes": 5000}
        from unittest import mock
        meta = {"title": "T", "abstract": "A", "url": "u", "arxiv_id": "1706.03762", "authors": [], "published": ""}
        with mock.patch.object(P.arxiv_src, "fetch_metadata", return_value=meta), \
             mock.patch.object(P.arxiv_src, "fetch_fulltext", return_value="text"), \
             mock.patch.object(P.claims_mod, "extract_claims", return_value={"claims": [claim], "headline": claim}):
            rec = p.run("1706.03762", runner="local")
        self.assertEqual(rec["verdict"]["verdict"], "inconclusive")
        self.assertIn("Not attempted", rec["verdict"]["reason"])
        self.assertEqual(rec["claim"]["value"], 28.4)
        self.assertEqual(rec["runs"], [])


class TestReasoningModels(unittest.TestCase):
    def _llm(self, replies):
        from receipts import http
        s = Settings.load()
        s.nebius_api_key = "test"
        l = llm.LLM(s)
        l._resolved = {"nano": "m", "super": "m", "ultra": "m"}
        sent = []

        def fake_post(url, body, **kw):
            sent.append(body["max_tokens"])
            return replies.pop(0)

        self._orig, http.post_json = http.post_json, fake_post
        self.addCleanup(lambda: setattr(http, "post_json", self._orig))
        return l, sent

    def test_empty_answer_from_reasoning_is_retried_with_bigger_budget(self):
        empty = {"choices": [{"message": {"content": None}, "finish_reason": "length"}], "usage": {}}
        good = {"choices": [{"message": {"content": "<think>hmm</think>OK"}, "finish_reason": "stop"}], "usage": {}}
        l, sent = self._llm([empty, good])
        self.assertEqual(l.chat("nano", "s", "u", max_tokens=50), "OK")
        self.assertEqual(sent[1], sent[0] * 2)
        self.assertGreater(sent[0], 50)  # headroom was added on top of the answer budget

    def test_truly_empty_raises(self):
        empty = {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}], "usage": {}}
        l, _ = self._llm([empty, empty, empty])
        with self.assertRaises(RuntimeError):
            l.chat("nano", "s", "u", max_tokens=50, retries=3)


class TestPublishing(unittest.TestCase):
    def _project(self):
        root = tempfile.mkdtemp()
        for rel, text in {"receipts/__init__.py": "", "README.md": "hi", "docs/index.html": "<html></html>",
                          ".env": "NEBIUS_API_KEY=sk-test-1234567890abcdef\n", "out/receipt-x.json": "{}",
                          "SUBMISSION_GUIDE.md": "g", "submission/a.md": "a", "bench/planted.json": "[]",
                          ".env.example": "NEBIUS_API_KEY=\n"}.items():
            p = os.path.join(root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(text)
        return root

    def test_bundle_never_copies_env_or_private_material(self):
        from receipts.publish import bundle
        root = self._project()
        dst = os.path.join(tempfile.mkdtemp(), "pub")
        n, problems = bundle(root, dst)
        names = {os.path.relpath(os.path.join(d, f), dst) for d, _, fs in os.walk(dst) for f in fs}
        self.assertNotIn(".env", names)
        self.assertFalse(any(x.startswith(("out", "submission")) for x in names))
        self.assertNotIn("SUBMISSION_GUIDE.md", names)
        self.assertIn(os.path.join("docs", "index.html"), names)
        self.assertIn(".env.example", names)
        self.assertEqual(problems, [])
        self.assertGreater(n, 3)

    def test_bundle_refuses_a_folder_that_already_has_files(self):
        from receipts.publish import bundle
        root = self._project()
        busy = tempfile.mkdtemp()
        with open(os.path.join(busy, "important.txt"), "w") as f:
            f.write("x")
        with self.assertRaises(SystemExit):
            bundle(root, busy)
        self.assertTrue(os.path.exists(os.path.join(busy, "important.txt")))

    def test_leakcheck_finds_planted_secrets_without_printing_them(self):
        from receipts.safety import leakcheck
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "notes.txt"), "w") as f:
            f.write("my key is " + "tvly" + "-" + "abcdefghijklmnopqrstuvwx")  # built at runtime so this file never looks like a leak
        with open(os.path.join(d, ".env"), "w") as f:
            f.write("X=1")
        problems = leakcheck(d, [])
        self.assertTrue(any(".env" in p for p in problems))
        self.assertTrue(any("notes.txt" in p for p in problems))
        self.assertFalse(any("abcdefghijkl" in p for p in problems))

    def test_site_builds_from_receipts(self):
        from receipts.site import build
        s = Settings.load()
        s.nebius_api_key = ""
        s.out_dir = tempfile.mkdtemp()
        Pipeline(s).run("demo:reproduces", seeds=2, runner="local")
        site = os.path.join(tempfile.mkdtemp(), "docs")
        info = build(s.out_dir, site, "https://github.com/x/y")
        self.assertEqual(info["receipts"], 1)
        with open(os.path.join(site, "index.html")) as f:
            html = f.read()
        self.assertNotIn("__RECEIPTS_DATA__", html)
        self.assertIn("Standardised logistic regression on handwritten digits", html)
        self.assertTrue(os.path.exists(os.path.join(site, "receipts")))


class TestNebiusTimeout(unittest.TestCase):
    def test_timeouts_below_one_hour_are_raised(self):
        from receipts.runner import clamp_job_timeout as c
        self.assertEqual(c("30m"), "1h")
        self.assertEqual(c("59m59s"), "1h")
        self.assertEqual(c(""), "1h")
        self.assertEqual(c("nonsense"), "1h")

    def test_valid_timeouts_are_kept(self):
        from receipts.runner import clamp_job_timeout as c
        self.assertEqual(c("1h"), "1h")
        self.assertEqual(c("2h30m10s"), "2h30m10s")
        self.assertEqual(c("24h"), "24h")

    def test_runner_applies_the_clamp(self):
        from receipts.runner import NebiusRunner
        self.assertEqual(NebiusRunner("img", "gpu-l40s-a", timeout="30m").timeout, "1h")


class TestDockerRunner(unittest.TestCase):
    def setUp(self):
        from receipts.runner import DockerRunner, JobSpec
        self.R, self.S = DockerRunner, JobSpec
        self.r = DockerRunner("img:latest", memory="2g", cpus="2")

    def _payload(self, cmd):
        import base64
        return json.loads(base64.b64decode(cmd[-1]))

    def test_offline_script_runs_with_no_network_and_hardening(self):
        cmd = self.r.command(self.S(script="print(1)", seed=1, install=False, no_network=True), "c1")
        self.assertEqual(cmd[:3], ["docker", "run", "--rm"])
        for flag in ("--cap-drop=ALL", "--security-opt=no-new-privileges"):
            self.assertIn(flag, cmd)
        i = cmd.index("--network")
        self.assertEqual(cmd[i + 1], "none")
        self.assertEqual(cmd[-2], "img:latest")
        self.assertFalse(self._payload(cmd)["no_network"])  # unshare needs a capability we drop

    def test_repo_runs_keep_network_because_clone_needs_it(self):
        cmd = self.r.command(self.S(script="x", seed=0, repo_url="https://github.com/a/b", install=True), "c2")
        self.assertNotIn("--network", cmd)

    def _fake(self, run_cp=None, run_exc=None, calls=None):
        import subprocess

        def fake(args, **kw):
            if calls is not None:
                calls.append(args)
            if "inspect" in args:
                return subprocess.CompletedProcess(args, 0, stdout="sha256:abc\n", stderr="")
            if "rm" in args:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if run_exc:
                raise run_exc
            return run_cp
        return fake

    def test_parses_result_and_records_image_id(self):
        import subprocess
        from unittest import mock
        out = 'RECEIPTS_META {"seed": 1}\nRECEIPTS_RESULT {"value": 42, "metric": "smoke", "extras": {}}\n'
        cp = subprocess.CompletedProcess([], 0, stdout=out, stderr="")
        with mock.patch.object(self.R, "available", return_value=True), \
             mock.patch("receipts.runner.subprocess.run", side_effect=self._fake(run_cp=cp)):
            res = self.r.run(self.S(script="x", seed=1, install=False))
        self.assertTrue(res.ok)
        self.assertEqual(res.value, 42.0)
        self.assertEqual(res.backend, "docker")
        self.assertEqual(res.meta["image_id"], "sha256:abc")

    def test_docker_daemon_failure_is_reported_clearly(self):
        import subprocess
        from unittest import mock
        cp = subprocess.CompletedProcess([], 125, stdout="", stderr="Cannot connect to the Docker daemon")
        with mock.patch.object(self.R, "available", return_value=True), \
             mock.patch("receipts.runner.subprocess.run", side_effect=self._fake(run_cp=cp)):
            res = self.r.run(self.S(script="x", seed=1, install=False))
        self.assertFalse(res.ok)
        self.assertIn("could not start the container", res.error)

    def test_timeout_removes_the_container(self):
        import subprocess
        from unittest import mock
        calls = []
        exc = subprocess.TimeoutExpired(cmd="docker", timeout=1)
        with mock.patch.object(self.R, "available", return_value=True), \
             mock.patch("receipts.runner.subprocess.run", side_effect=self._fake(run_exc=exc, calls=calls)):
            res = self.r.run(self.S(script="x", seed=1, install=False))
        self.assertEqual(res.error, "timeout")
        self.assertTrue(any(c[:3] == ["docker", "rm", "-f"] for c in calls))

    def test_missing_docker_is_a_clear_error(self):
        from unittest import mock
        with mock.patch.object(self.R, "available", return_value=False):
            res = self.r.run(self.S(script="x", seed=1))
        self.assertIn("docker not found", res.error)


class TestEntrypointFailsClosed(unittest.TestCase):
    def test_no_network_requested_but_unavailable_refuses_to_run(self):
        import base64
        import subprocess
        from receipts.runner import ENTRY
        payload = {"script": "print('SHOULD_NOT_RUN')", "seed": 1, "install": False, "no_network": True, "timeout_s": 10}
        b64 = base64.b64encode(json.dumps(payload).encode()).decode()
        # empty PATH => no `unshare` binary => isolation impossible
        p = subprocess.run([sys.executable, ENTRY, b64], capture_output=True, text=True, env={"PATH": ""})
        self.assertEqual(p.returncode, 5)
        self.assertIn("network_isolation_unavailable", p.stdout)
        self.assertNotIn("SHOULD_NOT_RUN", p.stdout)


class TestPipelineOffline(unittest.TestCase):
    def run_case(self, name, seeds=4):
        s = Settings.load()
        s.nebius_api_key = ""
        s.tavily_api_key = ""
        s.out_dir = tempfile.mkdtemp()
        events = []
        rec = Pipeline(s, emit=events.append).run(f"demo:{name}", seeds=seeds, runner="local")
        return rec, events

    def test_leakage_is_flagged_and_explained(self):
        rec, ev = self.run_case("leakage")
        self.assertEqual(rec["verdict"]["verdict"], "discrepancy_found")
        self.assertEqual(rec["diagnosis"]["category"], "data_leakage")
        self.assertTrue(rec["synthetic_demo"])
        self.assertTrue(os.path.exists(rec["_paths"]["html"]))
        self.assertEqual(ev[-1]["stage"], "receipt")

    def test_clean_case_reproduces(self):
        rec, _ = self.run_case("reproduces")
        self.assertEqual(rec["verdict"]["verdict"], "reproduced")
        self.assertIsNone(rec["diagnosis"])

    def test_broken_case_is_inconclusive_not_an_accusation(self):
        rec, _ = self.run_case("broken")
        self.assertEqual(rec["verdict"]["verdict"], "inconclusive")
        self.assertEqual(rec["diagnosis"]["category"], "data_unavailable")

    def test_every_planted_case_gets_its_expected_verdict(self):
        from receipts import demo_cases
        for key, case in demo_cases.CASES.items():
            rec, _ = self.run_case(key, seeds=3)
            self.assertEqual(rec["verdict"]["verdict"], case["expected"], f"{key}: {rec['verdict']}")

    def test_real_paper_without_keys_degrades_gracefully(self):
        s = Settings.load()
        s.nebius_api_key = ""
        s.out_dir = tempfile.mkdtemp()
        rec = Pipeline(s).run("https://arxiv.org/abs/1706.03762", runner="local")
        self.assertEqual(rec["verdict"]["verdict"], "inconclusive")
        self.assertIn("NEBIUS_API_KEY", rec["diagnosis"]["explanation"])

    def test_receipt_json_roundtrips_and_hash_is_stable(self):
        from receipts.receipt import canonical_hash
        rec, _ = self.run_case("reproduces", seeds=2)
        with open(rec["_paths"]["json"]) as f:
            d = json.load(f)
        self.assertEqual(d["sha256"], canonical_hash(d))


if __name__ == "__main__":
    unittest.main()
