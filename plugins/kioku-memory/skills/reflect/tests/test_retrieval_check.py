import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import retrieval_check as rc  # noqa: E402

VAULTS_TOML = '''
[[vault]]
name = "personal"
paths = ["~"]
domains = ["local", "personal", "personal/*"]

[[vault]]
name = "work"
paths = ["{home}/src/work"]
domains = ["work", "work/*"]

[[area]]
domain = "personal/app"
paths = ["{home}/src/personal/app"]

[[area]]
domain = "work/infra"
paths = ["{home}/src/work/infra"]
'''


def variant(queries, **overall):
    base = {"queries": len(queries), "recall": 1.0, "mrr": 1.0, "ndcg": 1.0, "hit_at_1": 1.0,
            "abstained": 0, "abstention_queries": 0}
    base.update(overall)
    return {"name": "hybrid+graph", "overall": base, "queries": queries}


def q(qid, ndcg, ranked, recall=1.0):
    return {"id": qid, "ranked": ranked, "recall": recall, "ndcg": ndcg, "reciprocal_rank": 1.0}


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = rc.main(argv)
    return code, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.saved = dict(os.environ)
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def write(self, name, data):
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return path


class CompareTest(Base):
    def reports(self):
        before = {"variants": [variant([q("a", 1.0, ["k1"]), q("b", 0.5, ["k9", "k2"]), q("c", 1.0, ["k3"]),
                                        q("d", 1.0, ["k4"])], ndcg=0.875)]}
        after = {"variants": [variant([q("a", 0.5, ["k7", "k1"]), q("b", 1.0, ["k2"]), q("c", 1.0, ["k3"]),
                                       q("d", 0.0, ["k8"], recall=0.0)], ndcg=0.625)],
                 "retrieval_check": {"queries": {
                     "a": {"query": "qa", "relevant": {"k1": 2}, "missing": []},
                     "b": {"query": "qb", "relevant": {"k2": 2}, "missing": []},
                     "d": {"query": "qd", "relevant": {"k4": 2}, "missing": ["k4"]}}}}
        return self.write("b.json", before), self.write("a.json", after)

    def test_json_buckets(self):
        b, a = self.reports()
        code, out, _ = run_main(["compare", str(b), str(a), "--format", "json"])
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual([r["id"] for r in result["worse"]], ["a"])
        self.assertEqual([r["id"] for r in result["better"]], ["b"])
        self.assertEqual(result["worse"][0]["top_after"], ["k7", "k1"])
        self.assertEqual(result["stale"], [{"id": "d", "query": "qd", "missing": ["k4"]}])
        self.assertAlmostEqual(result["headline"]["ndcg"]["delta"], -0.25)
        self.assertTrue(result["regressed"])

    def test_markdown_and_exit_codes(self):
        b, a = self.reports()
        code, out, _ = run_main(["compare", str(b), str(a)])
        self.assertEqual(code, 0)
        self.assertIn("## Worse (1)", out)
        self.assertIn("## Stale (1)", out)
        self.assertIn("nDCG@k", out)
        code, _, _ = run_main(["compare", str(b), str(a), "--fail-on-regression"])
        self.assertEqual(code, 1)

    def test_no_regression_exits_zero(self):
        b, _ = self.reports()
        code, _, _ = run_main(["compare", str(b), str(b), "--fail-on-regression"])
        self.assertEqual(code, 0)

    def test_missing_report(self):
        code, _, err = run_main(["compare", str(self.tmp / "nope.json"), str(self.tmp / "nope.json")])
        self.assertEqual(code, 2)
        self.assertIn("cannot read report", err)


def msg(role, content, **extra):
    d = {"type": role, "message": {"role": role, "content": content}}
    d.update(extra)
    return json.dumps(d)


def search(tid, query, domain=None, cwd=None):
    inp = {"query": query}
    if domain:
        inp["domain"] = domain
    extra = {"cwd": cwd} if cwd else {}
    return msg("assistant", [{"type": "tool_use", "id": tid, "name": "mcp__kioku__memory_search", "input": inp}], **extra)


def result(tid, keys):
    body = json.dumps({"results": [{"key": k} for k in keys]})
    return msg("user", [{"type": "tool_result", "tool_use_id": tid, "content": [{"type": "text", "text": body}]}])


def get(tid, keys):
    return msg("assistant", [{"type": "tool_use", "id": tid, "name": "mcp__kioku__memory_get", "input": {"keys": keys}}])


class MineTest(Base):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        area = self.home / "src/personal/app"
        work = self.home / "src/work/infra"
        for folder in (area, work):
            folder.mkdir(parents=True)
        self.area, self.work = area, work
        os.environ["HOME"] = str(self.home)
        toml = self.write("vaults.toml", VAULTS_TOML.format(home=self.home))
        os.environ["KIOKU_VAULTS_TOML"] = str(toml)
        os.environ["KIOKU_PROJECTS_DIR"] = str(self.tmp / "projects")

    def transcripts(self, folder, name, lines, sub=False):
        enc = rc.encode_folder(folder)
        path = self.tmp / "projects" / enc / ("sess/subagents/" + name if sub else name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")

    def mine(self, domain="personal/app"):
        out = self.tmp / "out.jsonl"
        code, stdout, err = run_main(["mine", "--domain", domain, "--out", str(out)])
        rows = [json.loads(x) for x in out.read_text().splitlines()] if out.exists() else []
        return code, rows, err

    def test_grades_dedup_and_filters(self):
        cwd = str(self.area)
        lines = [
            msg("user", "hello"),
            search("s1", "how are notes anchored to code paths", cwd=cwd),
            result("s1", ["n1", "n2", "n3"]),
            get("g1", ["n2"]),
            get("g2", ["n1", "n2"]),
            msg("user", "next question"),
            search("s2", "How are notes anchored to code paths?", domain="personal/app", cwd=cwd),  # near-duplicate
            result("s2", ["n3"]),
            get("g3", ["n3"]),
            search("s3", "something in another domain", domain="local", cwd=cwd),
            result("s3", ["x1"]),
            get("g4", ["x1"]),
            search("s4", "searched but never read", cwd=cwd),
            result("s4", ["n1"]),
            msg("user", "<local-command-stdout>ignored</local-command-stdout>"),
        ]
        self.transcripts(self.area, "main.jsonl", lines)
        code, rows, _ = self.mine()
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["relevant"], {"n2": 2, "n1": 1, "n3": 2})
        self.assertEqual(rows[0]["category"], "mined")

    def test_get_attributed_to_the_search_that_returned_it(self):
        cwd = str(self.area)
        lines = [
            msg("user", "go"),
            search("a", "first topic about alpha", cwd=cwd), result("a", ["alpha"]),
            search("b", "second topic about beta", cwd=cwd), result("b", ["beta"]),
            get("g", ["alpha"]),
        ]
        self.transcripts(self.area, "main.jsonl", lines)
        _, rows, _ = self.mine()
        self.assertEqual([r["relevant"] for r in rows], [{"alpha": 2}])

    def test_subagent_transcripts_and_session_outside_area(self):
        self.transcripts(self.area, "agent-1.jsonl",
                         [msg("user", "task"), search("s", "subagent question", cwd=str(self.area)), result("s", ["k"]),
                          get("g", ["k"])], sub=True)
        # a no-domain search from a cwd outside the area must not count
        self.transcripts(self.area, "other.jsonl",
                         [msg("user", "task"), search("s", "elsewhere", cwd=str(self.home / "somewhere")),
                          result("s", ["k"]), get("g", ["k"])])
        _, rows, _ = self.mine()
        self.assertEqual([r["query"] for r in rows], ["subagent question"])

    def test_never_reads_other_vault_transcripts(self):
        self.transcripts(self.work, "main.jsonl",
                         [msg("user", "x"), search("s", "work secret", cwd=str(self.work)), result("s", ["f"]),
                          get("g", ["f"])])
        _, rows, _ = self.mine()
        self.assertEqual(rows, [])
        code, _, err = self.mine("work/infra")[0], None, self.mine("work/infra")[2]
        self.assertEqual(code, 0)  # its own vault is allowed
        # an area folder placed in the wrong vault is refused
        bad = VAULTS_TOML.format(home=self.home) + '\n[[area]]\ndomain = "personal/leak"\npaths = ["%s"]\n' % self.work
        os.environ["KIOKU_VAULTS_TOML"] = str(self.write("bad.toml", bad))
        code, _, err = self.mine("personal/leak")
        self.assertEqual(code, 2)
        self.assertIn("not in vault", err)


class RunRefusalTest(Base):
    def setUp(self):
        super().setUp()
        toml = self.write("vaults.toml", VAULTS_TOML.format(home=self.tmp))
        os.environ["KIOKU_VAULTS_TOML"] = str(toml)
        self.marker = self.tmp / "kioku-called"
        fake_kioku = self.write("kioku", "#!/bin/sh\ntouch %s\nexit 1\n" % self.marker)
        fake_kioku.chmod(fake_kioku.stat().st_mode | stat.S_IXUSR)
        os.environ["KIOKU_BIN"] = str(fake_kioku)
        os.environ["KIOKU_ENV_FILE_PATH"] = str(self.tmp / "env")

    def fake_where(self, stdout):
        script = self.write("vaults.py", "import sys\nprint(%r)\n" % stdout)
        os.environ["KIOKU_VAULTS_PY"] = str(script)

    def dataset(self, **extra):
        data = {"name": "t", "domain": "personal/app", "queries": "queries.jsonl"}
        data.update(extra)
        self.write("ds/queries.jsonl", json.dumps({"id": "a", "query": "q", "relevant": {"k": 2}}) + "\n")
        return self.write("ds/dataset.json", data)

    def run_check(self, dataset):
        return run_main(["run", "--dataset", str(dataset), "--out", str(self.tmp / "report.json")])

    def test_says_so_when_kioku_home_is_unset(self):
        for name in ("KIOKU_VAULTS_PY", "KIOKU_REPO", "KIOKU_HOME"):
            os.environ.pop(name, None)
        code, _, err = self.run_check(self.dataset())
        self.assertEqual(code, 2)
        self.assertIn("KIOKU_HOME is not set", err)
        self.assertFalse(self.marker.exists())

    def test_refuses_other_vault(self):
        self.fake_where('/x: vault "work", at its root')
        code, _, err = self.run_check(self.dataset())
        self.assertEqual(code, 2)
        self.assertIn('not in vault "work"', err)
        self.assertFalse(self.marker.exists())

    def test_refuses_no_vault(self):
        self.fake_where("/: no memory: this session started in /")
        code, _, err = self.run_check(self.dataset())
        self.assertEqual(code, 2)
        self.assertIn("no vault", err)

    def test_refuses_corpus(self):
        self.fake_where('/x: area "personal/app" of vault "personal"')
        code, _, err = self.run_check(self.dataset(corpus="corpus"))
        self.assertEqual(code, 2)
        self.assertIn("corpus", err)
        self.assertFalse(self.marker.exists())

    def test_runs_and_marks_stale_keys(self):
        self.fake_where('/x: area "personal/app" of vault "personal"')
        report = {"k": 5, "variants": [variant([q("a", 1.0, ["k"])])]}
        script = ("#!/bin/sh\n"
                  "case \"$3\" in\n"
                  "  eval) while [ $# -gt 0 ]; do [ \"$1\" = --output ] && out=$2; shift; done\n"
                  "        echo '%s' > \"$out\" ;;\n"
                  "  search) echo '{\"results\": [{\"key\": \"other\", \"match\": \"search\"}]}' ;;\n"
                  "esac\n" % json.dumps(report))
        fake = self.write("kioku2", script)
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        os.environ["KIOKU_BIN"] = str(fake)
        code, out, err = self.run_check(self.dataset())
        self.assertEqual(code, 0, err)
        saved = json.loads((self.tmp / "report.json").read_text())
        self.assertEqual(saved["retrieval_check"]["queries"]["a"]["missing"], ["k"])
        self.assertIn("no longer exist", out)


class TomlTest(unittest.TestCase):
    def test_parse(self):
        tables = rc.parse_vaults_toml('# c\n[[vault]]\nname = "p"\ndomains = [\n  "a", # x\n  "b/*",\n]\n')
        self.assertEqual(tables["vault"][0]["domains"], ["a", "b/*"])
        self.assertTrue(rc.domain_in_vault("b/x", "p", tables))
        self.assertFalse(rc.domain_in_vault("c", "p", tables))


if __name__ == "__main__":
    unittest.main()
