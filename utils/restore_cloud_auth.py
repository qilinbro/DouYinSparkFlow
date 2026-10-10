"""Restore only encrypted auth files from the latest cloud run artifact."""

import io
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import zipfile

import requests

from utils.cloud_auth import CloudAuthError, load_bundle

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024


def latest_auth_artifact(artifacts):
    candidates = [artifact for artifact in artifacts
                  if re.fullmatch(r"cloud-auth-state-[0-9]+", artifact["name"]) and not artifact["expired"]]
    if not candidates:
        raise CloudAuthError("No encrypted cloud login exists; run cloud QR login first.")
    # Artifact IDs may come from different shards; they are not a time order.
    return max(candidates, key=lambda artifact: datetime.fromisoformat(artifact["created_at"].replace("Z", "+00:00")))


def restore_archive(content, account_ids, directory):
    if len(content) > MAX_ARCHIVE_BYTES:
        raise CloudAuthError("Cloud auth artifact exceeds the size limit.")
    allowed = {f"{account_id}.bin" for account_id in account_ids}
    destination = Path(directory)
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if set(names) != allowed or len(names) != len(allowed):
                raise CloudAuthError("Cloud auth artifact has unexpected account files.")
            if sum(entry.file_size for entry in entries) > MAX_ARCHIVE_BYTES:
                raise CloudAuthError("Cloud auth artifact exceeds the size limit.")
            payloads = {entry.filename: archive.read(entry) for entry in entries}
    except (zipfile.BadZipFile, RuntimeError, OSError):
        raise CloudAuthError("Cloud auth artifact could not be read.") from None
    destination.mkdir(parents=True, exist_ok=True)
    for name, payload in payloads.items():
        # No extractall: each name was checked against fixed account filenames.
        path = destination / name
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(payload)
        temporary.replace(path)


def main():
    directory = os.environ.get("CLOUD_AUTH_DIR")
    token = os.environ.get("GH_TOKEN")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not directory or not token or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise CloudAuthError("Cloud auth restore configuration is incomplete.")
    tasks = json.loads(os.environ.get("TASKS", "[]"))
    account_ids = [task["unique_id"] for task in tasks]
    if not account_ids or any(not re.fullmatch(r"[A-Za-z0-9_]+", account_id) for account_id in account_ids):
        raise CloudAuthError("Cloud auth account configuration is invalid.")
    base_url = f"https://api.github.com/repos/{repository}"
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}",
                            "Accept": "application/vnd.github+json",
                            "X-GitHub-Api-Version": "2022-11-28"})
    response = session.get(f"{base_url}/actions/artifacts", params={"per_page": 100}, timeout=45)
    response.raise_for_status()
    latest = latest_auth_artifact(response.json()["artifacts"])
    response = session.get(f"{base_url}/actions/artifacts/{int(latest['id'])}/zip", timeout=60)
    response.raise_for_status()
    restore_archive(response.content, account_ids, directory)
    for account_id in account_ids:
        load_bundle(account_id, expected_uid=os.environ.get("CLOUD_EXPECTED_UID") or None)
    print("Encrypted cloud login restored and authenticated encryption verified.")


if __name__ == "__main__":
    try:
        main()
    except CloudAuthError as exc:
        print(f"Cloud login restore failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("Cloud login restore failed; credentials and artifact contents withheld.", file=sys.stderr)
        raise SystemExit(1) from None
