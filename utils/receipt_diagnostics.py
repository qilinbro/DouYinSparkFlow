"""Report only allowlisted receipt structure and numeric codes, never payload values."""
import re

CONTAINERS = frozenset(("data", "response", "body", "error", "BaseResp", "base_resp", "baseResp",
                        "send_message_body", "sendMessageBody"))
CODES = frozenset(("code", "status_code", "statusCode", "StatusCode", "error_code", "errorCode",
                   "check_code", "checkCode", "status", "filter_status_code", "filter_block_code", "errno"))
ACK_FIELDS = frozenset(("message_id", "messageId", "server_message_id", "serverMessageId", "client_message_id"))
CHALLENGE_FIELDS = frozenset(("bdturing", "verify_data", "verifyData", "verify_config", "VerifyConfig", "captcha"))
ERROR_TEXT_FIELDS = frozenset(("message", "error_desc", "errorDesc", "check_message", "status_msg"))


def summarize_json_receipt(data):
    summary = {"root_type": type(data).__name__, "known_fields": [], "unknown_field_count": 0,
               "codes": {}, "ack_fields_present": False, "challenge_fields_present": False,
               "error_text_fields_present": False}
    visited = 0

    def visit(node, path="", depth=0):
        nonlocal visited
        if not isinstance(node, dict) or depth > 3 or visited >= 12:
            return
        visited += 1
        for key, value in node.items():
            # Unknown keys might themselves be identifiers; count without printing them.
            if key not in CONTAINERS | CODES | ACK_FIELDS | CHALLENGE_FIELDS | ERROR_TEXT_FIELDS:
                summary["unknown_field_count"] += 1
                continue
            field = f"{path}.{key}" if path else key
            summary["known_fields"].append(field)
            if key in CODES:
                if isinstance(value, str) and re.fullmatch(r"-?\d{1,10}", value):
                    value = int(value)
                if isinstance(value, int) and not isinstance(value, bool) and -(2**31) <= value < 2**31:
                    summary["codes"][field] = value
            if key in ACK_FIELDS:
                summary["ack_fields_present"] = True
            if key in CHALLENGE_FIELDS:
                summary["challenge_fields_present"] = True
            if key in ERROR_TEXT_FIELDS:
                summary["error_text_fields_present"] = True
            if key in CONTAINERS:
                visit(value, field, depth + 1)

    visit(data)
    return summary
