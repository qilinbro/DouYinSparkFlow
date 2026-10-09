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

    def test_unknown_wrapper_exposes_only_safe_nested_status_and_types(self):
        summary = summarize_json_receipt({"private-user-as-key": {
            "status_code": 8, "message": "private-text", "private-id-as-key": "private-token"}})
        self.assertEqual(summary["codes"], {"<unknown0>.status_code": 8})
        self.assertEqual(summary["unknown_value_type_counts"], {"dict": 1, "str": 1})
        self.assertNotIn("private", json.dumps(summary))

    def test_unknown_wrappers_in_lists_can_reveal_business_status(self):
        summary = summarize_json_receipt({"private-wrapper": [{"check_code": 100}]})
        self.assertEqual(summary["codes"], {"<unknown0>[0].check_code": 100})
        self.assertNotIn("private", json.dumps(summary))

    def test_empty_challenge_field_is_not_a_verification_request(self):
        for value in (None, "", {}, []):
            summary = summarize_json_receipt({"bdturing": value})
            self.assertTrue(summary["challenge_fields_present"])
            self.assertFalse(summary["challenge_value_nonempty"])

    def test_unknown_wrapper_recursion_is_bounded(self):
        response = {"status_code": 8}
        for _ in range(20):
            response = {"private-wrapper": response}
        summary = summarize_json_receipt(response)
        self.assertEqual(summary["codes"], {})
        self.assertNotIn("private", json.dumps(summary))


if __name__ == "__main__":
    unittest.main()
