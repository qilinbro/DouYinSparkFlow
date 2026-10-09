"""Validate Actions configuration without loading the application or sending messages."""

import json
import math
import os
import re
import sys
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """An invalid setting, described without including account or secret values."""


@dataclass(frozen=True)
class ConfigurationSummary:
    accounts: int
    targets: int
    cookies: int


def parse_json(value: str, description: str):
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        raise ConfigurationError(f"{description} must contain valid JSON.") from None


def is_nonempty_string(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_cookie(cookie, description: str) -> None:
    if not isinstance(cookie, dict):
        raise ConfigurationError(f"{description} must be an object.")
    if not is_nonempty_string(cookie.get("name")):
        raise ConfigurationError(f"{description} needs a nonempty string name.")
    if not isinstance(cookie.get("value"), str):
        raise ConfigurationError(f"{description} needs a string value.")

    cookie_url = cookie.get("url")
    if cookie_url is not None:
        if not is_nonempty_string(cookie_url):
            raise ConfigurationError(f"{description} needs a valid HTTP(S) URL.")
        try:
            parsed_url = urlsplit(cookie_url)
            valid_url = parsed_url.scheme in {"http", "https"} and bool(parsed_url.hostname)
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ConfigurationError(f"{description} needs a valid HTTP(S) URL.")
    elif not (
        is_nonempty_string(cookie.get("domain"))
        and is_nonempty_string(cookie.get("path"))
    ):
        raise ConfigurationError(f"{description} needs domain and path, or a URL.")


def validate_environment(environ: Mapping[str, str]) -> ConfigurationSummary:
    for key, default in (
        ("BROWSER_ACTION_TIMEOUT", "120"),
        ("FRIEND_LIST_WAIT_TIME", "2"),
    ):
        value = environ.get(key, default)
        try:
            seconds = float(value) if isinstance(value, str) else math.nan
        except ValueError:
            seconds = math.nan
        milliseconds = seconds * 1000
        if not math.isfinite(milliseconds) or milliseconds < 1:
            raise ConfigurationError(
                f"{key} must be a positive number of seconds (at least 0.001)."
            )

    for key, default in (
        ("IM_SCAN_TIMEOUT", "120"),
        ("IM_READY_TIMEOUT", "120"),
        ("IM_MAX_STEPS", "200"),
        ("TASK_RETRY_TIMES", "3"),
    ):
        value = environ.get(key, default)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value.strip()):
            raise ConfigurationError(f"{key} must be a positive integer.")
        try:
            positive = int(value) > 0
        except ValueError:
            positive = False
        if not positive:
            raise ConfigurationError(f"{key} must be a positive integer.")

    if environ.get("MATCH_MODE", "nickname") not in {"nickname", "short_id"}:
        raise ConfigurationError("MATCH_MODE must be nickname or short_id.")

    log_level = environ.get("LOG_LEVEL", "Info")
    if not isinstance(log_level, str) or log_level.lower() not in {
        "error", "warning", "info", "debug"
    }:
        raise ConfigurationError("LOG_LEVEL must be Error, Warning, Info, or Debug.")

    hitokoto_types = parse_json(
        environ.get("HITOKOTO_TYPES", '["文学","影视","诗词","哲学"]'),
        "HITOKOTO_TYPES",
    )
    if not isinstance(hitokoto_types, list) or not all(
        is_nonempty_string(item) for item in hitokoto_types
    ):
        raise ConfigurationError("HITOKOTO_TYPES must be an array of nonempty strings.")

    tasks = parse_json(environ.get("TASKS", ""), "TASKS")
    if not isinstance(tasks, list) or not tasks:
        raise ConfigurationError("TASKS must be a nonempty array.")

    seen_ids = set()
    target_count = 0
    cookie_count = 0
    for task_number, task in enumerate(tasks, start=1):
        description = f"Task {task_number}"
        if not isinstance(task, dict):
            raise ConfigurationError(f"{description} must be an object.")
        unique_id = task.get("unique_id")
        if not isinstance(unique_id, str) or not re.fullmatch(r"[A-Za-z0-9_]+", unique_id):
            raise ConfigurationError(
                f"{description} unique_id must contain only letters, digits, or underscores."
            )
        normalized_id = unique_id.upper()
        if normalized_id in seen_ids:
            raise ConfigurationError("TASKS contains duplicate unique_id values.")
        seen_ids.add(normalized_id)

        targets = task.get("targets")
        if not isinstance(targets, list) or not targets or not all(
            is_nonempty_string(target) for target in targets
        ):
            raise ConfigurationError(f"{description} targets must be a nonempty array of strings.")
        target_count += len(targets)

        cookie_value = environ.get(f"COOKIES_{normalized_id}", "")
        if not is_nonempty_string(cookie_value):
            raise ConfigurationError(f"{description} is missing its cookie secret.")
        try:
            # Match utils.config.get_userData, including generated escaped newlines.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                cookie_json = cookie_value.encode("utf-8").decode("unicode_escape")
        except UnicodeError:
            raise ConfigurationError(f"{description} cookie secret has invalid escaping.") from None
        cookies = parse_json(cookie_json, f"{description} cookie secret")
        if not isinstance(cookies, list) or not cookies:
            raise ConfigurationError(f"{description} cookie secret must be a nonempty array.")
        for cookie_number, cookie in enumerate(cookies, start=1):
            validate_cookie(cookie, f"{description} cookie {cookie_number}")
        cookie_count += len(cookies)

    return ConfigurationSummary(len(tasks), target_count, cookie_count)


def main() -> int:
    try:
        summary = validate_environment(os.environ)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    print(
        f"Configuration validated: {summary.accounts} accounts, "
        f"{summary.targets} targets, {summary.cookies} cookies."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
