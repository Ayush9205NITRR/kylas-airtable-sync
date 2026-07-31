"""Tests for the standalone gmail_to_airtable.py.

It deliberately duplicates the package's logic so it can be copied out and run
on its own — these tests stop the two drifting apart silently.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gmail_to_airtable as g

T1 = 1748858696000   # 2025-06-02 IST
T2 = 1749105000000   # 2025-06-05 IST


def _msg(ts, message_id, subject, frm, to="", cc="", parts=None):
    payload = {"headers": [
        {"name": "Message-ID", "value": message_id},
        {"name": "Subject", "value": subject},
        {"name": "From", "value": frm},
        {"name": "To", "value": to},
        {"name": "Cc", "value": cc},
    ]}
    if parts:
        payload["parts"] = parts
    return {"id": f"msg{message_id}", "internalDate": str(ts),
            "payload": payload, "snippet": "preview text"}


class TestQuery(unittest.TestCase):
    def test_bare_keyword_becomes_exact_phrase(self):
        self.assertEqual(g.build_query("proposal"), '"proposal" in:anywhere')
        self.assertEqual(g.build_query("cost sheet"), '"cost sheet" in:anywhere')

    def test_gmail_syntax_passes_through(self):
        for raw in ("from:x@y.com has:attachment", '"a" OR "b"', "-label:spam"):
            self.assertEqual(g.build_query(raw), raw)

    def test_since_clause(self):
        self.assertEqual(g.since_clause(""), "newer_than:180d")
        self.assertEqual(g.since_clause("30d"), "newer_than:30d")
        self.assertEqual(g.since_clause("45"), "newer_than:45d")
        self.assertEqual(g.since_clause("2025-01-15"), "after:2025/01/15")
        self.assertEqual(g.since_clause("all"), "")


class TestAttachments(unittest.TestCase):
    def test_inline_parts_without_attachment_id_are_excluded(self):
        self.assertEqual(
            g.attachments_in({"parts": [{"filename": "sig.png",
                                         "body": {"size": 400}}]}), [])

    def test_nested_attachments_are_found(self):
        payload = {"parts": [{"parts": [
            {"filename": "cost.pdf", "mimeType": "application/pdf",
             "body": {"attachmentId": "a1", "size": 900}}]}]}
        found = g.attachments_in(payload)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["attachment_id"], "a1")


class TestThreadRow(unittest.TestCase):
    def setUp(self):
        thread = {"id": "t1", "messages": [
            # newest first on purpose — the code must sort by internalDate
            _msg(T2, "<reply>", "Re: Cost sheet Goa", "DMC <sales@dmc.com>",
                 "ayush@enout.in", "fin@enout.in",
                 parts=[{"filename": "cost.pdf", "mimeType": "application/pdf",
                         "body": {"attachmentId": "a1", "size": 900}}]),
            _msg(T1, "<original>", "Cost sheet Goa", "Ayush <ayush@enout.in>",
                 "sales@dmc.com", "ops@enout.in"),
        ]}
        self.row, self.refs = g.thread_row(
            thread, ["Cost Sheet", "Proposal"], "ayush@enout.in")

    def test_subject_and_sender_from_first_message(self):
        self.assertEqual(self.row["Subject"], "Cost sheet Goa")
        self.assertEqual(self.row["Sender Email"], "ayush@enout.in")
        self.assertEqual(self.row["Sender Name"], "Ayush")
        self.assertEqual(self.row["First Message ID"], "<original>")

    def test_cc_is_union_across_the_thread(self):
        self.assertEqual(self.row["CC Emails"], "ops@enout.in, fin@enout.in")

    def test_dates_span_the_thread_in_ist(self):
        self.assertTrue(self.row["First Email Date"].startswith("2025-06-02"))
        self.assertTrue(self.row["Last Email Date"].startswith("2025-06-05"))
        self.assertTrue(self.row["First Email Date"].endswith("+05:30"))

    def test_counts_and_categories(self):
        self.assertEqual(self.row["Attachments"], "cost.pdf")
        self.assertEqual(self.row["Attachment Count"], 1)
        self.assertEqual(self.row["Message Count"], 2)
        self.assertEqual(self.row["All Categories"], "Cost Sheet, Proposal")
        self.assertEqual(self.row["Category"], "Cost Sheet")

    def test_attachment_refs_can_be_downloaded(self):
        self.assertEqual(self.refs[0]["attachment_id"], "a1")
        self.assertEqual(self.refs[0]["message_id"], "msg<reply>")

    def test_empty_thread(self):
        row, refs = g.thread_row({"id": "x", "messages": []}, ["X"], "")
        self.assertIsNone(row)
        self.assertEqual(refs, [])


class TestSchemaContract(unittest.TestCase):
    def test_thread_id_is_the_primary_field_and_upsert_key(self):
        self.assertEqual(g.FIELDS[0]["name"], g.KEY_FIELD)

    def test_attachment_field_is_an_attachment_type(self):
        field = next(f for f in g.FIELDS if f["name"] == g.ATTACHMENT_FIELD)
        self.assertEqual(field["type"], "multipleAttachments")

    def test_every_scraped_key_has_a_field_defined(self):
        thread = {"id": "t", "messages": [_msg(T1, "<a>", "S", "a@b.com")]}
        row, _ = g.thread_row(thread, ["X"], "me@x.com")
        defined = {f["name"] for f in g.FIELDS}
        self.assertEqual(set(row) - defined, set(),
                         "row keys with no matching Airtable field")


if __name__ == "__main__":
    unittest.main()
