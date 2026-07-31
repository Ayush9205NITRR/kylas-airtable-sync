"""Tests for the Gmail scraper's pure parsing / query-building logic.

No network: threads are hand-built payloads in the shape the Gmail API returns.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gmail_scraper import config, parse, search_terms


def _msg(internal_date, subject=None, frm=None, to=None, cc=None,
         parts=None, snippet=""):
    headers = []
    if subject is not None:
        headers.append({"name": "Subject", "value": subject})
    if frm is not None:
        headers.append({"name": "From", "value": frm})
    if to is not None:
        headers.append({"name": "To", "value": to})
    if cc is not None:
        headers.append({"name": "Cc", "value": cc})
    payload = {"headers": headers}
    if parts:
        payload["parts"] = parts
    return {"internalDate": str(internal_date), "payload": payload,
            "snippet": snippet}


# 2025-06-02 15:34:56 IST and 2025-06-05 12:00:00 IST
T1 = 1748858696000
T2 = 1749105000000


class TestHeaders(unittest.TestCase):
    def test_header_is_case_insensitive(self):
        m = _msg(T1, subject="Hi")
        self.assertEqual(parse.header(m, "subject"), "Hi")
        self.assertEqual(parse.header(m, "SUBJECT"), "Hi")
        self.assertEqual(parse.header(m, "Missing"), "")

    def test_addresses_dedups_and_lowercases(self):
        value = 'Ayush <Ayush@Enout.in>, ops@enout.in, "Dup" <ayush@enout.in>'
        self.assertEqual(parse.addresses(value), ["ayush@enout.in", "ops@enout.in"])

    def test_addresses_on_empty(self):
        self.assertEqual(parse.addresses(""), [])
        self.assertEqual(parse.addresses(None), [])

    def test_display_name_falls_back_to_local_part(self):
        self.assertEqual(parse.display_name("Ayush K <ayush@enout.in>"), "Ayush K")
        self.assertEqual(parse.display_name("ayush@enout.in"), "ayush")


class TestSubject(unittest.TestCase):
    def test_strips_stacked_prefixes(self):
        self.assertEqual(parse.clean_subject("Re: Fwd: RE: Offsite DMC Goa"),
                         "Offsite DMC Goa")

    def test_leaves_clean_subject_alone(self):
        self.assertEqual(parse.clean_subject("Offsite DMC Goa"), "Offsite DMC Goa")

    def test_does_not_eat_words_starting_with_re(self):
        self.assertEqual(parse.clean_subject("Rebooking the offsite"),
                         "Rebooking the offsite")


class TestAttachments(unittest.TestCase):
    def test_collects_nested_attachments_only(self):
        payload = {"parts": [
            {"filename": "", "body": {"size": 400}},
            {"parts": [
                {"filename": "quote.pdf", "body": {"attachmentId": "a1", "size": 900}},
                {"filename": "logo.png", "body": {"size": 0}},
            ]},
            {"filename": "itinerary.xlsx", "body": {"attachmentId": "a2", "size": 10}},
        ]}
        self.assertEqual(parse.attachment_names(payload),
                         ["quote.pdf", "itinerary.xlsx"])

    def test_empty_payload(self):
        self.assertEqual(parse.attachment_names({}), [])


class TestThreadToRecord(unittest.TestCase):
    def setUp(self):
        self.thread = {"id": "thread123", "messages": [
            # Deliberately out of order — the code must sort by internalDate.
            _msg(T2, subject="Re: Offsite DMC Goa",
                 frm="DMC Goa <sales@dmcgoa.com>",
                 to="ayush@enout.in",
                 cc="finance@enout.in, ops@enout.in",
                 parts=[{"filename": "quote.pdf",
                         "body": {"attachmentId": "a1", "size": 500}}]),
            _msg(T1, subject="Offsite DMC Goa",
                 frm="Ayush <ayush@enout.in>",
                 to="sales@dmcgoa.com",
                 cc="ops@enout.in",
                 snippet="Sharing the brief for the offsite"),
        ]}
        self.record = parse.thread_to_record(
            self.thread, category="Offsite DMC",
            all_categories=["Offsite DMC", "Offsite"], mailbox="ayush@enout.in")

    def test_subject_and_sender_come_from_first_message(self):
        self.assertEqual(self.record["Subject"], "Offsite DMC Goa")
        self.assertEqual(self.record["Sender Email"], "ayush@enout.in")
        self.assertEqual(self.record["Sender Name"], "Ayush")

    def test_cc_is_union_across_thread_without_duplicates(self):
        self.assertEqual(self.record["CC Emails"], "ops@enout.in, finance@enout.in")

    def test_dates_span_first_to_last_message_in_ist(self):
        self.assertTrue(self.record["First Email Date"].startswith("2025-06-02"))
        self.assertTrue(self.record["Last Email Date"].startswith("2025-06-05"))
        self.assertTrue(self.record["First Email Date"].endswith("+05:30"))
        self.assertLess(self.record["First Email Date"], self.record["Last Email Date"])

    def test_attachments_and_counts(self):
        self.assertEqual(self.record["Attachments"], "quote.pdf")
        self.assertEqual(self.record["Attachment Count"], 1)
        self.assertEqual(self.record["Message Count"], 2)

    def test_key_and_categories(self):
        self.assertEqual(self.record[config.KEY_FIELD], "thread123")
        self.assertEqual(self.record["Category"], "Offsite DMC")
        self.assertEqual(self.record["All Categories"], "Offsite DMC, Offsite")
        self.assertIn("thread123", self.record["Gmail Link"])

    def test_missing_subject_gets_placeholder(self):
        thread = {"id": "t2", "messages": [_msg(T1, frm="a@b.com")]}
        rec = parse.thread_to_record(thread, "Offsite")
        self.assertEqual(rec["Subject"], "(no subject)")
        self.assertEqual(rec["CC Emails"], "")

    def test_empty_thread_returns_empty(self):
        self.assertEqual(parse.thread_to_record({"id": "x", "messages": []}, "X"), {})


class TestQueryBuilding(unittest.TestCase):
    def test_since_shorthands(self):
        self.assertEqual(config.since_clause("30d"), "newer_than:30d")
        self.assertEqual(config.since_clause("6m"), "newer_than:6m")
        self.assertEqual(config.since_clause("45"), "newer_than:45d")
        self.assertEqual(config.since_clause("2025-01-15"), "after:2025/01/15")
        self.assertEqual(config.since_clause("all"), "")
        self.assertEqual(config.since_clause(""),
                         f"newer_than:{config.DEFAULT_LOOKBACK_DAYS}d")

    def test_since_rejects_garbage(self):
        with self.assertRaises(ValueError):
            config.since_clause("last tuesday")

    def test_categories_file_builds_scoped_queries(self):
        cats = config.load_categories()
        self.assertTrue(cats, "config/email_categories.json has no categories")
        by_name = {c["name"]: c["query"] for c in cats}
        self.assertEqual(by_name["Offsite DMC"], '"Offsite DMC" in:anywhere')
        self.assertIn(" OR ", by_name["Offsite"])
        self.assertTrue(by_name["Offsite"].endswith("in:anywhere"))

    def test_bare_name_is_quoted_and_scoped(self):
        self.assertEqual(config.build_query("Offsite DMC"),
                         '"Offsite DMC" in:anywhere')
        self.assertEqual(config.build_query("Acme Travels Pvt Ltd"),
                         '"Acme Travels Pvt Ltd" in:anywhere')

    def test_gmail_syntax_passes_through_untouched(self):
        for raw in ('from:sales@dmc.com has:attachment',
                    '"Offsite" OR "Outing"',
                    'subject:quote -label:spam',
                    '(offsite AND goa)'):
            self.assertEqual(config.build_query(raw), raw)

    def test_scope_can_be_overridden_or_dropped(self):
        self.assertEqual(config.build_query("Offsite", scope="in:inbox"),
                         '"Offsite" in:inbox')
        self.assertEqual(config.build_query("Offsite", scope=""), '"Offsite"')

    def test_blank_term_yields_no_query(self):
        self.assertEqual(config.build_query("   "), "")
        self.assertEqual(config.build_query(None), "")

    def test_explicit_query_is_used_verbatim(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "_tmp_categories.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"default_scope": "in:anywhere", "categories": [
                {"name": "Raw", "query": "from:dmc@x.com has:attachment"},
            ]}, fh)
        original = config.CATEGORIES_FILE
        try:
            config.CATEGORIES_FILE = path
            self.assertEqual(config.load_categories(),
                             [{"name": "Raw", "query": "from:dmc@x.com has:attachment"}])
        finally:
            config.CATEGORIES_FILE = original
            os.remove(path)


class TestSearchTermsTable(unittest.TestCase):
    """The Airtable rows -> search terms mapping, with _fetch_rows stubbed."""

    def _rows(self, *field_dicts):
        return [{"id": f"rec{i}", "fields": f} for i, f in enumerate(field_dicts)]

    def _with_rows(self, rows):
        original = search_terms._fetch_rows
        search_terms._fetch_rows = lambda: rows
        self.addCleanup(lambda: setattr(search_terms, "_fetch_rows", original))
        return search_terms.from_airtable()

    def test_bare_name_row(self):
        out = self._with_rows(self._rows({"Search Term": "Acme Travels",
                                          "Active": True}))
        self.assertEqual(out, [{"name": "Acme Travels",
                                "query": '"Acme Travels" in:anywhere'}])

    def test_inactive_rows_are_ignored(self):
        out = self._with_rows(self._rows(
            {"Search Term": "On", "Active": True},
            {"Search Term": "Off", "Active": False},
        ))
        self.assertEqual([t["name"] for t in out], ["On"])

    def test_missing_active_column_defaults_to_on(self):
        out = self._with_rows(self._rows({"Search Term": "No checkbox here"}))
        self.assertEqual(len(out), 1)

    def test_blank_and_whitespace_rows_are_skipped(self):
        out = self._with_rows(self._rows({"Search Term": "   "}, {"Notes": "x"}))
        self.assertEqual(out, [])

    def test_category_and_scope_overrides(self):
        out = self._with_rows(self._rows({
            "Search Term": "Goa DMC", "Category": "Offsite DMC",
            "Scope": "in:inbox", "Active": True,
        }))
        self.assertEqual(out, [{"name": "Offsite DMC",
                                "query": '"Goa DMC" in:inbox'}])

    def test_renamed_term_column_still_works(self):
        out = self._with_rows(self._rows({"Name": "Fallback column"}))
        self.assertEqual(out[0]["name"], "Fallback column")


class TestAttachmentRefs(unittest.TestCase):
    def test_refs_carry_ids_needed_to_download(self):
        thread = {"id": "t1", "messages": [
            _msg(T1, parts=[{"filename": "quote.pdf",
                             "mimeType": "application/pdf",
                             "body": {"attachmentId": "att1", "size": 1234}}]),
            _msg(T2, parts=[{"parts": [
                {"filename": "itinerary.xlsx",
                 "mimeType": "application/vnd.ms-excel",
                 "body": {"attachmentId": "att2", "size": 99}},
            ]}]),
        ]}
        thread["messages"][0]["id"] = "m1"
        thread["messages"][1]["id"] = "m2"
        refs = parse.attachment_refs(thread)
        self.assertEqual([r["filename"] for r in refs],
                         ["quote.pdf", "itinerary.xlsx"])
        self.assertEqual(refs[0]["message_id"], "m1")
        self.assertEqual(refs[0]["attachment_id"], "att1")
        self.assertEqual(refs[0]["mime_type"], "application/pdf")
        self.assertEqual(refs[1]["message_id"], "m2")

    def test_inline_parts_without_attachment_id_are_excluded(self):
        thread = {"id": "t1", "messages": [
            _msg(T1, parts=[{"filename": "signature.png",
                             "body": {"size": 400}}]),
        ]}
        self.assertEqual(parse.attachment_refs(thread), [])

    def test_defaults_to_octet_stream_when_mime_missing(self):
        thread = {"id": "t1", "messages": [
            _msg(T1, parts=[{"filename": "x.bin",
                             "body": {"attachmentId": "a", "size": 1}}]),
        ]}
        self.assertEqual(parse.attachment_refs(thread)[0]["mime_type"],
                         "application/octet-stream")


if __name__ == "__main__":
    unittest.main()
