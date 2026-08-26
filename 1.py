#!/usr/bin/env python3
"""Run NetBackup nbinstallcmd safely on a Linux primary server.

This script deliberately does not depend on nbinstallcmd stdout containing a job ID.
The companion PowerShell script discovers jobs by comparing REST snapshots.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
INSTALLABLE_RE = re.compile(r"(?im)^\s*Installables:\s+(\S+)")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def read_clients(path: Path) -> list[str]:
    if not path.is_file():
        raise ValueError(f"Client file does not exist: {path}")
    clients: list[str] = []
    seen: set[str] = set()
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        client = raw.strip()
        if not client or client.startswith("#"):
            continue
        if not HOST_RE.fullmatch(client):
            raise ValueError(f"Invalid client name on line {number}: {client!r}")
        key = client.lower()
        if key not in seen:
            clients.append(client)
            seen.add(key)
    if not clients:
        raise ValueError("Client file contains no usable client names.")
    return clients


def resolve_package(args: argparse.Namespace) -> tuple[str, dict]:
    if args.package:
        return args.package, {"source": "argument", "packageId": None}
    command = [args.nbrepo, "-p", str(args.package_id)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=args.command_timeout)
    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode != 0:
        raise RuntimeError(f"nbrepo failed with return code {result.returncode}: {combined.strip()}")
    match = INSTALLABLE_RE.search(combined)
    if not match:
        raise RuntimeError("Could not find the Installables value in nbrepo output.")
    return match.group(1), {
        "source": "nbrepo",
        "packageId": str(args.package_id),
        "nbrepoReturnCode": result.returncode,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operation", required=True, choices=("precheck", "stage", "install"))
    package = parser.add_mutually_exclusive_group(required=True)
    package.add_argument("--package", help="Installables value, for example nbclient_10.5")
    package.add_argument("--package-id", help="Repository package ID resolved through nbrepo -p")
    parser.add_argument("--master-server", required=True)
    parser.add_argument("--client-file", required=True, type=Path)
    parser.add_argument("--result-json", type=Path, help="Optional local audit metadata file")
    parser.add_argument("--limit-jobs", type=int, default=5)
    parser.add_argument("--nbinstallcmd", default="/usr/openv/netbackup/bin/nbinstallcmd")
    parser.add_argument("--nbrepo", default="/usr/openv/netbackup/bin/admincmd/nbrepo")
    parser.add_argument("--certificate-argument", action="append", default=[])
    parser.add_argument("--approve-install", action="store_true")
    parser.add_argument("--command-timeout", type=int, default=600)
    parser.add_argument("--lock-file", type=Path, default=Path("/tmp/netbackup_client_migration.lock"))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started_at = utc_now()
    result_record: dict = {
        "schemaVersion": 1,
        "operation": args.operation,
        "masterServer": args.master_server,
        "clientFile": str(args.client_file),
        "startedAt": started_at,
        "completedAt": None,
        "returnCode": None,
        "status": "Preparing",
        "clients": [],
        "package": None,
        "packageMetadata": {},
        "command": [],
        "stdout": "",
        "stderr": "",
        "error": "",
    }
    try:
        if args.operation == "install" and not args.approve_install:
            raise ValueError("Install requires --approve-install.")
        if args.limit_jobs < 1:
            raise ValueError("--limit-jobs must be at least 1.")
        if not args.dry_run and not Path(args.nbinstallcmd).is_file():
            raise ValueError(f"nbinstallcmd not found: {args.nbinstallcmd}")
        clients = read_clients(args.client_file)
        package_name, package_metadata = resolve_package(args) if not args.dry_run or args.package else (
            args.package,
            {"source": "argument", "packageId": None},
        )
        certificate_arguments = args.certificate_argument or ["-use_existing_certs"]
        command = [
            args.nbinstallcmd,
            "-operation_type", args.operation,
            "-package", package_name,
            "-master_server", args.master_server,
            "-host_filelist", str(args.client_file),
            "-limit_jobs", str(args.limit_jobs),
            *certificate_arguments,
        ]
        result_record.update(
            clients=clients,
            package=package_name,
            packageMetadata=package_metadata,
            command=command,
        )
        args.lock_file.parent.mkdir(parents=True, exist_ok=True)
        with args.lock_file.open("w", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another NetBackup client migration submission is active.") from exc
            if args.dry_run:
                result_record.update(returnCode=0, status="DryRun")
            else:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=args.command_timeout,
                    shell=False,
                )
                result_record.update(
                    returnCode=completed.returncode,
                    stdout=completed.stdout or "",
                    stderr=completed.stderr or "",
                    status="Submitted" if completed.returncode == 0 else "CommandFailed",
                )
        return int(result_record["returnCode"] or 0)
    except subprocess.TimeoutExpired as exc:
        result_record.update(returnCode=124, status="TimedOut", error=str(exc))
        return 124
    except Exception as exc:
        result_record.update(returnCode=2, status="Failed", error=str(exc))
        return 2
    finally:
        result_record["completedAt"] = utc_now()
        if args.result_json:
            atomic_json(args.result_json, result_record)


if __name__ == "__main__":
    sys.exit(main())
