"""Synthetic receipts verify diagnostics never disclose text or identifiers."""
import json
import unittest

from utils.receipt_diagnostics import summarize_json_receipt


class ReceiptDiagnosticTests(unittest.TestCase):
    def test_nested_numeric_error_is_reported(self):
        summary = summarize_json_receipt({"data": {"statusCode": "8", "message": "private-text"}})
        self.assertEqual(summary["codes"], {"data.statusCode": 8})
        self.assertNotIn("private-text", json.dumps(summary))

    def test_identifiers_and_challenge_values_are_never_reported(self):
        summary = summarize_json_receipt({"server_message_id": "private-id", "bdturing": "private-token",
                                          "private-user-as-key": "private-value"})
        self.assertTrue(summary["ack_fields_present"])
        self.assertTrue(summary["challenge_fields_present"])
        self.assertNotIn("private", json.dumps(summary))
        self.assertEqual(summary["unknown_field_count"], 1)

    def test_untrusted_code_text_bool_and_large_integer_are_omitted(self):
        for value in ("private-token", True, 2**63, "123456789012345"):
            self.assertEqual(summarize_json_receipt({"code": value})["codes"], {})

    def test_non_object_response_is_safe(self):
        self.assertEqual(summarize_json_receipt(["private-value"])["root_type"], "list")
        self.assertNotIn("private", json.dumps(summarize_json_receipt(["private-value"])))


if __name__ == "__main__":
    unittest.main()
