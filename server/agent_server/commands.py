"""agent-server config ... and agent-server db ...: the stored settings and the database, from the server's shell."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config_io
from .config import Settings
from .db import migrate, redacted
from .db import legacy


def main(kind: str, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"agent-server {kind}")
    commands = parser.add_subparsers(dest="action", required=True)
    if kind == "vault":
        parser.description = "The key provider accounts' managed tokens are encrypted with (AI_PROXY_VAULT_KEYS: <version>:<base64>, comma-separated)."
        key = commands.add_parser("key", help="Print a new random key to add to AI_PROXY_VAULT_KEYS")
        key.add_argument("--version", type=int, default=0, help="Its version (default: one above the newest configured)")
        commands.add_parser("status", help="Which key versions are configured, and how many tokens each encrypts")
        commands.add_parser("rotate", help="Re-encrypt every stored token with the newest configured key")
    elif kind == "audit":
        parser.description = "The audit log: check its hash chains and signed checkpoints, sign one now, or print the key that signs them."
        verify = commands.add_parser("verify", help="Recompute every organization's chain and check the checkpoints (exit 1 on any problem)")
        verify.add_argument("--org", help="Only this organization's chain")
        commands.add_parser("checkpoint", help="Sign every chain's head now")
        commands.add_parser("key", help="Print the public key checkpoints are signed with (to check them elsewhere)")
    elif kind == "config":
        parser.description = "The server's settings (network, backends, model tuning, agents) as one YAML document."
        export = commands.add_parser("export", help="Write every setting as YAML (to a file, or to standard output)")
        export.add_argument("file", nargs="?")
        load = commands.add_parser("import", help="Store a YAML (or JSON) settings document: only the parts it has, unless --replace")
        load.add_argument("file", help="The document, or - for standard input")
        load.add_argument("--replace", action="store_true", help="Every part: what the document leaves out goes back to empty")
    else:
        parser.description = "The database everything is kept in (AI_PROXY_DATABASE_URL; without it a SQLite file in logs/)."
        commands.add_parser("info", help="Which database, its version, size and what's in it")
        commands.add_parser("migrate", help="Bring the tables up to this version (agent-server does it on start too)")
        load = commands.add_parser("import", help="Import an older install's files (config/, users.json, logs/sessions, ...)")
        load.add_argument("--from", dest="root", help="The old install's folder (holding config/, certs/ and logs/); without it, this install's")
    args = parser.parse_args(argv)
    try:
        if kind == "vault":
            return _vault(args)
        if kind == "audit":
            return _audit(args)
        if kind == "config" and args.action == "export":
            text = config_io.dump(config_io.export(Settings.load()))
            if args.file:
                Path(args.file).write_text(text, encoding="utf-8")
                print(f"Wrote the settings to {args.file}.", file=sys.stderr)
            else:
                sys.stdout.write(text)
        elif kind == "config":
            text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
            changed = config_io.apply(Settings.load(), config_io.parse(text), replace=args.replace)
            print(f"Stored {', '.join(changed) or 'nothing'}. A running agent-server uses them after a restart (agents at once).")
        elif args.action == "info":
            settings = Settings.load()
            db = settings.open_database()
            sessions = len(db_sessions(db))
            print(f"Database: {redacted(db.url)}")
            print(f"Version: {migrate.version(db)} (this agent-server's: {migrate.latest()})")
            size = db.size()
            print(f"Size: {size / 1e6:.1f} MB" if size is not None else "Size: unknown")
            print(f"Sessions: {sessions}")
            print(f"Recordings kept: {f'{settings.retention_days} days' if settings.retention_days else 'forever'}")
        elif args.action == "migrate":
            settings = Settings.load(database=False)
            from .db import Database

            db = Database(settings.database_address)
            version = migrate.migrate(db)
            db.close()
            # Then everything a first start does once (seeding, the shipped agents, an older install's import), so
            # the server's processes, started with AI_PROXY_MIGRATE=0, find it done and never race to do it.
            settings = Settings.load()
            from . import retention

            seeded = retention.seed_from_environment(settings)
            settings.open_database().close()
            print(f"{redacted(db.url)} is at version {version}." + (" Seeded the retention policy." if seeded else ""))
        else:
            settings = Settings.load()
            db = settings.open_database()
            root = Path(args.root).resolve() if args.root else None
            with db.write() as connection:
                done = legacy.import_settings(connection, settings, root)
            sessions = legacy.import_sessions(db, (root / "logs" / "sessions") if root else settings.log_dir / "sessions")
            print(f"Imported {', '.join(done) or 'no settings'} and {sessions} sessions. Restart agent-server to use them.")
    except (OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def _audit(args: argparse.Namespace) -> int:
    """agent-server audit verify|checkpoint|key."""
    import json

    from . import blobs
    from .audit import Audit

    audit = Audit(Settings.load().open_database(), blobs=blobs.from_environment())
    if args.action == "key":
        key_id, public_key = audit.public_key()
        print(json.dumps({"key_id": key_id, "algorithm": "Ed25519", "public_key": public_key}))
        return 0
    if args.action == "checkpoint":
        record = audit.checkpoint()
        print("Signed a checkpoint of " + ", ".join(f"{tenant} at {seq}" for tenant, (seq, _) in record["heads"].items()) if record else "Nothing recorded since the last checkpoint.")
        return 0
    report = audit.verify(args.org)
    for tenant, chain in sorted(report["tenants"].items()):
        print(f"{tenant}: {chain['events']} events" + (f", from {chain['first']} to {chain['head']}" if chain["events"] else ""))
    print(f"Checkpoints: {report['checkpoints']}")
    for problem in report["problems"]:
        print(f"PROBLEM: {problem}", file=sys.stderr)
    print("The audit log is intact." if report["ok"] else "The audit log has problems (above).")
    return 0 if report["ok"] else 1


def _vault(args: argparse.Namespace) -> int:
    """agent-server vault key|status|rotate. Prints key material only for `key`, which makes a new one."""
    from sqlalchemy import func, select

    from .db import schema
    from .provider_accounts import ProviderAccounts, Vault, VaultError, new_key

    try:
        vault = Vault.from_environment()
    except (VaultError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.action == "key":
        print(new_key(args.version or vault.current + 1))
        print("Add it to AI_PROXY_VAULT_KEYS (keep older keys listed until `agent-server vault rotate` has run).", file=sys.stderr)
        return 0
    db = Settings.load().open_database()
    if args.action == "status":
        table = schema.provider_credentials
        with db.read() as connection:
            counts = dict(connection.execute(select(table.c.key_version, func.count()).group_by(table.c.key_version)).all())
        print(f"Configured key versions: {', '.join(str(item) for item in sorted(vault.keys)) or 'none (managed tokens are refused)'}")
        for version, count in sorted(counts.items()):
            missing = "" if version in vault.keys else " (that key is missing: they can't be decrypted)"
            print(f"Tokens under key {version}: {count}{missing}")
        return 0
    if not vault.configured:
        print("Set AI_PROXY_VAULT_KEYS first.", file=sys.stderr)
        return 1

    class Offline:
        machines: dict = {}

    try:
        done = ProviderAccounts(db, Offline(), vault).rotate()
    except VaultError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    db.close()
    print(f"Re-encrypted {done} tokens with key {vault.current}.")
    return 0


def db_sessions(db: object) -> list[str]:
    from sqlalchemy import select

    from .db import schema

    with db.read() as connection:  # type: ignore[attr-defined]
        return list(connection.execute(select(schema.sessions.c.id)).scalars())
