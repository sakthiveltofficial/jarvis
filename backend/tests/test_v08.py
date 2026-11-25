"""v0.8: nightly memory consolidation and file indexing."""
import datetime as dt
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("JARVIS_HOME", tempfile.mkdtemp())

from jarvis import consolidation as cons  # noqa: E402
from jarvis import memory as gm  # noqa: E402
from jarvis.files import FileIndex, chunk, extract, wanted  # noqa: E402


def mem():
    m = gm.GraphMemory(os.path.join(tempfile.mkdtemp(), "g.db"))
    m.ensure_user("Alex")
    return m


def backdate(m, fact_id, days):
    t = (dt.datetime.now() - dt.timedelta(days=days)).replace(microsecond=0).isoformat()
    m.q("MATCH ()-[f:Fact {id:$id}]->() SET f.created=$t, f.reinforced=''", {"id": fact_id, "t": t})


class ConsolidationTest(unittest.TestCase):
    def setUp(self):
        self.m = mem()
        self.settings = {"memory": {"consolidate": True, "consolidate_time": "03:00", "half_life_days": 120,
                                    "keep_history_days": 730}, "llm": {"provider": "anthropic", "kind": "anthropic"},
                         "api_keys": {}, "user_name": "Alex", "assistant_name": "Jarvis"}
        self.c = cons.Consolidator(self.settings, self.m)

    def test_old_schema_gets_new_columns(self):
        facts = self.m.facts(valid_only=False)
        self.assertEqual(facts, [])
        self.m.add_fact("USER", "likes", "Coffee", "Alex likes coffee.")
        f = self.m.facts()[0]
        self.assertEqual((f["uses"], f["reason"]), (0, ""))

    def test_repeat_reinforces(self):
        self.m.add_fact("USER", "likes", "Coffee", "Alex likes coffee.")
        self.m.add_fact("USER", "likes", "Coffee", "Alex likes coffee.")
        self.assertEqual(self.m.facts()[0]["uses"], 1)
        self.m.search("does Alex like coffee")
        self.assertEqual(self.m.facts()[0]["uses"], 2)

    def test_merge_duplicate_entities(self):
        self.m.add_fact("USER", "works_at", "Acme Corp", "Alex works at Acme Corp.")
        self.m.add_fact("the acme corp", "located_in", "Chennai", "Acme Corp is in Chennai.")
        self.m.upsert_entity("Mom", "Person")
        self.m.add_fact("mom", "likes", "Roses", "Mom likes roses.")
        report = self.c.run(use_llm=False)
        names = sorted(e["name"] for e in self.m.entities())
        self.assertEqual(report["entities_merged"], 1)     # "the acme corp" -> "Acme Corp"; "mom"/"Mom" share an id
        self.assertEqual(len([n for n in names if "acme" in n.lower()]), 1)
        acme = next(e["id"] for e in self.m.entities() if "acme" in e["name"].lower())
        self.assertEqual(len(self.m.facts(around=[acme])), 2)

    def test_merge_repeated_facts(self):
        self.m.add_fact("USER", "likes", "Coffee", "Alex likes coffee a lot.")
        self.m.add_fact("USER", "enjoys", "Coffee", "Alex likes coffee, a lot!")
        self.m.add_fact("USER", "drinks", "Coffee", "Alex drinks coffee every morning at 7.")
        report = self.c.run(use_llm=False)
        self.assertEqual(report["facts_merged"], 1)
        self.assertEqual(len(self.m.facts()), 2)
        merged = [f for f in self.m.facts(valid_only=False) if f["reason"] == "merged"]
        self.assertEqual(len(merged), 1)

    def test_stale_facts_fade_but_identity_stays(self):
        self.m.add_fact("USER", "interested_in", "Chess", "Alex was into chess openings.")
        self.m.add_fact("USER", "lives_in", "Chennai", "Alex lives in Chennai.")
        self.m.add_fact("USER", "uses", "Vim", "Alex uses Vim.")
        for f in self.m.facts():
            backdate(self.m, f["id"], 400)
        vim = next(f["id"] for f in self.m.facts() if f["relation"] == "uses")
        self.m.reinforce([vim])                     # recalled recently: stays
        report = self.c.run(use_llm=False)
        self.assertEqual(report["facts_faded"], 1)
        current = {f["relation"] for f in self.m.facts()}
        self.assertEqual(current, {"lives_in", "uses"})
        faded = [f for f in self.m.facts(valid_only=False) if f["reason"] == "faded"]
        self.assertEqual(faded[0]["relation"], "interested_in")   # kept as history, not deleted

    def test_strength_curve(self):
        now = dt.datetime.now()
        f = {"created": (now - dt.timedelta(days=120)).isoformat(), "uses": 0}
        self.assertAlmostEqual(cons.strength(f, 120, now), 0.5, places=2)
        f["uses"] = 3
        self.assertGreater(cons.strength(f, 120, now), 1.0)

    def test_prune_old_history_and_orphans(self):
        self.m.add_fact("USER", "lives_in", "Pune", "Alex lived in Pune.")
        fid = self.m.facts()[0]["id"]
        self.m.invalidate(fid)
        old = (dt.datetime.now() - dt.timedelta(days=800)).isoformat()
        self.m.q("MATCH ()-[f:Fact {id:$id}]->() SET f.invalidated=$t", {"id": fid, "t": old})
        self.m.upsert_entity("Lonely Thing")
        self.m.q("MATCH (e:Entity {id:'lonely_thing'}) SET e.updated=$t", {"t": old})
        report = self.c.run(use_llm=False)
        self.assertEqual(report["history_pruned"], 1)
        self.assertGreaterEqual(report["orphans_removed"], 1)
        self.assertNotIn("lonely_thing", {e["id"] for e in self.m.entities()})

    def test_digest_with_llm(self):
        self.m.apply({"facts": [{"source": "USER", "relation": "working_on", "target": "Jarvis project",
                                 "fact": "Alex is building the Jarvis project."}]}, "USER: I'm building Jarvis today")
        self.settings["api_keys"] = {"anthropic": "sk-test"}
        reply = '{"summary": "Alex spent the day building the Jarvis project.", "entities": ["Jarvis project"]}'
        with mock.patch("jarvis.llm.complete", return_value=reply):
            report = self.c.run()
        self.assertEqual(report["days_summarised"], [dt.date.today().isoformat()])
        d = self.m.digests()
        self.assertIn("Jarvis project", d[0]["text"])
        self.assertIn("building the Jarvis", self.m.search("what did I do on the jarvis project")["text"])
        # nothing new since: no second summary
        with mock.patch("jarvis.llm.complete", return_value=reply) as llm:
            report = self.c.run()
        self.assertEqual(report["days_summarised"], [])
        self.assertEqual(llm.call_count, 0)
        self.assertEqual(cons.describe(report), "Everything was already tidy.")

    def test_schedule(self):
        self.c.state = {"last_run": dt.datetime.now().isoformat()}
        nr = self.c.next_run()
        self.assertGreater(nr, dt.datetime.now())
        self.c.state = {"last_run": (dt.datetime.now() - dt.timedelta(days=2)).isoformat()}
        self.assertLessEqual(self.c.next_run(), dt.datetime.now())     # missed run: catch up now
        self.settings["memory"]["consolidate"] = False
        self.assertIsNone(self.c.next_run())

    def test_describe(self):
        self.assertEqual(cons.describe({"facts_faded": 2, "entities_merged": 1}),
                         "Merged 1 duplicate entity, faded 2 stale facts.")


class FileIndexTest(unittest.TestCase):
    def setUp(self):
        self.m = mem()
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "notes").mkdir()
        (self.dir / "notes" / "contract.md").write_text(
            "# Service agreement with Acme Corp\n\nThis agreement is between Alex and Acme Corp.\n\n"
            "## Payment\n\nAcme Corp pays an invoice within 30 days of receipt. Late payment adds 2% interest "
            "per month.\n\n## Termination\n\nEither side may end the agreement with 60 days notice.\n", "utf-8")
        (self.dir / "app.py").write_text("def total(items):\n    return sum(i.price for i in items)\n" * 3, "utf-8")
        (self.dir / "node_modules").mkdir()
        (self.dir / "node_modules" / "x.js").write_text("ignored()", "utf-8")
        self.m.upsert_entity("Acme Corp", "Organization")
        self.settings = {"files": {"folders": [str(self.dir)], "extract_entities": False, "auto_recall": True},
                         "memory": {}, "llm": {"provider": "anthropic", "kind": "anthropic"}, "api_keys": {}}
        self.fi = FileIndex(self.settings, self.m, db_path=Path(tempfile.mkdtemp()) / "f.db", watch=False)

    def test_index_search_and_cite(self):
        self.fi.scan(str(self.dir))
        st = self.fi.status()["folders"][0]
        self.assertEqual(st["files"], 2)                            # node_modules skipped
        hits = self.fi.search("what did the contract say about payment?")
        self.assertTrue(hits)
        self.assertGreaterEqual(hits[0]["coverage"], 0.5)     # good enough to attach automatically
        self.assertEqual(hits[0]["name"], "contract.md")
        self.assertIn("30 days", hits[0]["text"])
        self.assertEqual(hits[0]["loc"], "Payment")
        out = self.fi.tool({"query": "late payment interest"}).text
        self.assertIn("[1] contract.md, Payment", out)
        # linked into the graph: the document is about an entity Jarvis already knew
        g = self.fi.graph_part()
        self.assertEqual(g["edges"][0]["target"], "acme_corp")
        rows = self.m.q("MATCH (c:Chunk)-[:Cites]->(e:Entity {id:'acme_corp'}) RETURN count(c)")
        self.assertGreaterEqual(rows[0][0], 1)

    def test_change_and_delete(self):
        self.fi.scan(str(self.dir))
        f = self.dir / "notes" / "contract.md"
        time.sleep(0.01)
        f.write_text("# Payment\n\nPayment is now due within 45 days, said the amended contract.\n", "utf-8")
        os.utime(f, (time.time() + 5, time.time() + 5))
        self.fi.scan(str(self.dir))
        hits = self.fi.search("payment due days")
        self.assertIn("45 days", hits[0]["text"])
        self.assertFalse(any("30 days" in h["text"] for h in hits))
        f.unlink()
        self.fi.scan(str(self.dir))
        self.assertEqual(self.fi.search("payment"), [])
        self.assertEqual(self.m.q("MATCH (d:Document {name:'contract.md'}) RETURN count(d)")[0][0], 0)

    def test_remove_folder(self):
        self.fi.scan(str(self.dir))
        self.settings["files"]["folders"] = []
        self.fi.sync()
        self.assertEqual(self.m.q("MATCH (d:Document) RETURN count(d)")[0][0], 0)
        self.assertEqual(self.m.q("MATCH (c:Chunk) RETURN count(c)")[0][0], 0)

    def test_entity_extraction_and_merge_follow(self):
        self.settings["files"]["extract_entities"] = True
        reply = '{"summary": "A service agreement with Acme Corp.", "entities": [{"name": "Bob Stone", "type": "Person"}]}'
        (self.dir / "notes" / "contract.md").write_text("Signed by Bob Stone for Acme Corp. Bob Stone approves.", "utf-8")
        self.fi.scan(str(self.dir))
        did = next(r[0] for r in self.fi.db.execute("SELECT id FROM docs WHERE name='contract.md'"))
        self.settings["api_keys"] = {"anthropic": "sk-test"}
        self.fi._queue_entities(str(self.dir))
        self.assertEqual(self.fi.q.get_nowait(), ("entities", did))
        with mock.patch("jarvis.llm.complete", return_value=reply):
            self.fi._extract_entities(did)
        doc = self.fi.document(did)
        self.assertIn("bob_stone", doc["entities"])
        self.assertIn("Acme", doc["summary"])
        # consolidation merging "Bob Stone" into "bob stone" keeps the file link
        self.m.upsert_entity("bob stones", "Person")
        self.m.add_fact("USER", "knows", "bob stones", "Alex knows Bob.")
        keep = "bob_stones"
        self.m.merge_entities(keep, "bob_stone")
        self.assertIn(keep, self.fi.document(did)["entities"])

    def test_extract_formats(self):
        import docx
        d = docx.Document()
        d.add_heading("Payment terms", 1)
        d.add_paragraph("Invoices are paid within 30 days.")
        p = self.dir / "terms.docx"
        d.save(str(p))
        secs = extract(p)
        self.assertEqual(secs[0][0], "Payment terms")
        from pypdf import PdfWriter
        w = PdfWriter()
        w.add_blank_page(100, 100)
        pdf = self.dir / "blank.pdf"
        with open(pdf, "wb") as fh:
            w.write(fh)
        self.assertEqual(extract(pdf), [])        # scanned/blank PDF: no text, no crash

    def test_chunking(self):
        long = "\n\n".join(f"Paragraph {i} " + "word " * 80 for i in range(20))
        parts = chunk([("", long)], code=False)
        self.assertGreater(len(parts), 3)
        self.assertTrue(all(len(t) < 2200 for _, t in parts))
        code = "\n".join(f"x{i} = {i}" for i in range(600))
        cparts = chunk([("", code)], code=True)
        self.assertTrue(cparts[0][0].startswith("lines 1-"))
        self.assertFalse(wanted(Path("/a/node_modules/b.js")))
        self.assertFalse(wanted(Path("/a/b/.git/c.md")))
        self.assertTrue(wanted(Path("/a/b/c.pdf")))


class ServerRoutesTest(unittest.TestCase):
    def test_routes(self):
        from jarvis import server
        paths = {r.path for r in server.app.routes}
        for p in ("/api/memory/consolidation", "/api/memory/consolidate", "/api/files", "/api/files/folders",
                  "/api/files/search", "/api/files/open", "/api/files/pick", "/api/files/reindex"):
            self.assertIn(p, paths)


if __name__ == "__main__":
    unittest.main()
