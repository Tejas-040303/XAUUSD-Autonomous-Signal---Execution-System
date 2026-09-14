"""Archiver entry point (P0).

Builds the corpus spec §28 requires and keeps history coverage gap-free. It does
not parse signals, does not decide anything, and cannot place an order — none of
that code exists yet, and the archive has to come first because the parser must
be developed against real screenshots rather than invented examples.

Three modes:

    python3 -m xauusd.run_archiver --config config/local.yaml check
    python3 -m xauusd.run_archiver --config config/local.yaml backfill
    python3 -m xauusd.run_archiver --config config/local.yaml live

`check` touches no network and needs no credentials: it validates the config,
migrates both databases, and reports what coverage exists. Run it first — it is
the cheapest way to find out that `pip_size` is still unset or that the status
chat collides with a source.

`backfill` walks history. `live` backfills first, then listens, because
connecting a listener without closing the restart gap leaves a hole exactly
where the last shutdown was.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from xauusd.archive import store
from xauusd.archive.media import MediaLimits
from xauusd.archive.ranges import IdRange, gaps, max_scanned
from xauusd.clock import Clock, SystemClock
from xauusd.config.schema import Config, ConfigError, load_config
from xauusd.db import migrate as migrations
from xauusd.db.store import Role, connect
from xauusd.notify import outbox
from xauusd.telegram import backfill as backfill_mod

ARCHIVER_CHAT_KIND = "ARCHIVER_STATUS"


@dataclass(slots=True)
class Runtime:
    """Everything a mode needs, opened once."""

    config: Config
    clock: Clock
    archive: sqlite3.Connection
    trading: sqlite3.Connection
    limits: MediaLimits
    media_root: Path

    def close(self) -> None:
        self.archive.close()
        self.trading.close()


def open_runtime(config_path: Path, clock: Clock, *, apply_migrations: bool) -> Runtime:
    """Load config, open both databases, bring the schema up to date.

    `apply_migrations=False` for anything that is not explicitly a migration
    step: booting must never alter the ledger's shape as a side effect, so the
    live modes verify and refuse rather than silently upgrading.
    """
    config = load_config(config_path)

    archive = connect(config.database.archive_path, Role.ARCHIVE, config.database.busy_timeout_ms)
    trading = connect(config.database.trading_path, Role.TRADING, config.database.busy_timeout_ms)

    for conn, role in ((archive, Role.ARCHIVE), (trading, Role.TRADING)):
        if apply_migrations:
            migrations.migrate(conn, role)
        else:
            migrations.verify(conn, role)
    migrations.integrity_check(trading)

    limits = MediaLimits(
        max_bytes=config.telegram.max_image_bytes,
        max_pixels=config.telegram.max_image_pixels,
        size_mismatch_tolerance=float(config.archive.size_mismatch_tolerance),
    )
    media_root = config.archive.media_root
    media_root.mkdir(parents=True, exist_ok=True)

    return Runtime(
        config=config,
        clock=clock,
        archive=archive,
        trading=trading,
        limits=limits,
        media_root=media_root,
    )


def _coverage_report(runtime: Runtime) -> list[str]:
    lines: list[str] = []
    for source in runtime.config.telegram.sources:
        scanned = store.load_scan_ranges(runtime.archive, source.chat_id)
        top = max_scanned(scanned)
        archived = runtime.archive.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id = ?", (source.chat_id,)
        ).fetchone()[0]
        with_media = runtime.archive.execute(
            "SELECT COUNT(*) FROM media WHERE chat_id = ? AND state = 'stored'",
            (source.chat_id,),
        ).fetchone()[0]
        unresolved = runtime.archive.execute(
            "SELECT COUNT(*) FROM media WHERE chat_id = ? AND state IN ('pending','failed')",
            (source.chat_id,),
        ).fetchone()[0]
        incomparable = runtime.archive.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id = ? AND content_hash IS NULL",
            (source.chat_id,),
        ).fetchone()[0]

        lines.append(f"  chat {source.chat_id} ({source.kind})")
        lines.append(f"    scanned ranges   {len(scanned)}  top id {top if top else '-'}")
        if top is not None:
            holes = gaps(
                scanned, IdRange(runtime.config.archive.backfill_oldest_message_id, top)
            )
            lines.append(
                f"    gaps below top   {len(holes)}"
                + (f"  e.g. {holes[0].lo}-{holes[0].hi}" if holes else "")
            )
        lines.append(f"    messages         {archived}")
        lines.append(f"    media stored     {with_media}   unresolved {unresolved}")
        lines.append(f"    no fingerprint   {incomparable}  (not comparable for dedupe)")
        if source.kind == "group" and not source.allowed_sender_ids:
            lines.append("    !! group with no allowed_sender_ids — any member could post a signal")
    return lines


def mode_check(runtime: Runtime) -> int:
    """Validate and report. No network, no credentials."""
    config = runtime.config
    print("config          OK")
    print(f"  symbol        {config.symbol.name}  pip_size={config.symbol.pip_size}")

    # The pip value decides whether this account size is viable at all, so it is
    # reported as arithmetic rather than as a number to be glanced at.
    pip_value_per_min_lot = config.symbol.pip_size * config.symbol.contract_size * config.symbol.volume_min
    worst_case = pip_value_per_min_lot * config.risk.max_sl_pips
    print(
        f"  risk check    {config.symbol.volume_min} lots x {config.risk.max_sl_pips} pips "
        f"= {worst_case} vs daily_loss_limit {config.risk.daily_loss_limit}"
    )
    if worst_case > config.risk.daily_loss_limit:
        print(
            "  !! the SMALLEST placeable trade exceeds the whole daily loss limit, so every "
            "signal would be rejected. Verify pip_size against the broker contract spec."
        )
    elif worst_case * Decimal(
        config.risk.max_daily_trades_morning + config.risk.max_daily_trades_evening
    ) > config.risk.daily_loss_limit:
        print(
            "  .. a full day of maximum-SL losses exceeds daily_loss_limit; the kill switch "
            "will bind before the trade caps do."
        )

    print(f"databases       archive={config.database.archive_path} trading={config.database.trading_path}")
    print(f"  schema        archive v{migrations.current_version(runtime.archive)} "
          f"trading v{migrations.current_version(runtime.trading)}")
    print(f"media root      {runtime.media_root}")
    print(f"status chat     {config.telegram.status_chat_id}")
    print("coverage")
    for line in _coverage_report(runtime):
        print(line)

    pending = outbox.pending_count(runtime.trading)
    failed = outbox.failed_messages(runtime.trading, limit=5)
    age = outbox.oldest_pending_age_seconds(runtime.trading, runtime.clock.now_utc())
    print(f"outbox          pending={pending}" + (f" oldest={age:.0f}s" if age else ""))
    if failed:
        # Surfaced rather than buried: a failed row means the operator was never
        # told something, and a clean-looking startup would hide that.
        print(f"  !! {len(failed)} message(s) never delivered:")
        for message in failed:
            print(f"     #{message.id} {message.kind} after {message.attempts} attempts")
    return 0


def _build_reader(runtime: Runtime):
    """Connect a telethon client. Interactive on first run."""
    from xauusd.config.secrets import load_telegram_secrets
    from xauusd.telegram.ingest import TelethonReader, build_client

    secrets = load_telegram_secrets(require_bot_token=False)
    client = build_client(
        secrets.api_id, secrets.api_hash, runtime.config.telegram.session_path
    )
    client.start()
    return client, TelethonReader(client, runtime.clock)


def mode_backfill(runtime: Runtime) -> int:
    client, reader = _build_reader(runtime)
    try:
        return _do_backfill(runtime, reader)
    finally:
        client.disconnect()


def _do_backfill(runtime: Runtime, reader) -> int:
    config = runtime.config
    worst = 0
    for source in config.telegram.sources:
        report = backfill_mod.run(
            reader,
            runtime.archive,
            source.chat_id,
            clock=runtime.clock,
            limits=runtime.limits,
            media_root=runtime.media_root,
            oldest_message_id=config.archive.backfill_oldest_message_id,
            batch_size=config.archive.backfill_batch_size,
            max_windows=config.archive.backfill_max_windows_per_run,
        )
        summary = (
            f"chat {source.chat_id}: {report.messages_new} new / {report.messages_seen} seen, "
            f"{report.edits_recorded} edits, {report.media_stored} media, "
            f"{report.windows_scanned}/{report.windows_planned} windows, "
            f"{len(report.remaining_gaps)} gaps left"
        )
        print(summary)
        if report.stopped_early:
            print(f"  stopped early: {report.stopped_early}")
            worst = 1

        recovered = backfill_mod.retry_unresolved_media(
            reader,
            runtime.archive,
            source.chat_id,
            clock=runtime.clock,
            limits=runtime.limits,
            media_root=runtime.media_root,
        )
        if recovered:
            print(f"  recovered {recovered} previously unresolved attachment(s)")

        outbox.enqueue(
            runtime.trading,
            chat_id=config.telegram.status_chat_id,
            kind=ARCHIVER_CHAT_KIND,
            body=summary + (f" — stopped: {report.stopped_early}" if report.stopped_early else ""),
            at=runtime.clock.now_utc(),
            priority=outbox.Priority.STATUS,
            collapse_key=f"backfill:{source.chat_id}",
        )
    return worst


def mode_live(runtime: Runtime) -> int:
    """Close the restart gap, then listen.

    Backfill first, always. A listener connected without closing the gap leaves
    a hole exactly where the last shutdown was — which is the morning session
    that was missed, not an arbitrary range.
    """
    from xauusd.telegram.ingest import LiveArchiver

    client, reader = _build_reader(runtime)
    try:
        _do_backfill(runtime, reader)

        chat_ids = [s.chat_id for s in runtime.config.telegram.sources]
        live = LiveArchiver(
            client,
            runtime.archive,
            chat_ids=chat_ids,
            clock=runtime.clock,
            limits=runtime.limits,
            media_root=runtime.media_root,
            media_poll_seconds=float(runtime.config.archive.media_poll_seconds),
            on_message=lambda msg, res: print(
                f"[{msg.chat_id}/{msg.message_id}] "
                f"{'new' if res.inserted else 'seen'}"
                f"{' +edit' if res.edit_recorded else ''}"
                f"{f' +{len(res.downloads)} media' if res.downloads else ''}"
            ),
            on_error=lambda src, exc: print(f"!! {src}: {type(exc).__name__}: {exc}", file=sys.stderr),
        )
        live.register()
        print(f"listening on {chat_ids} — Ctrl-C to stop")

        async def _serve() -> None:
            stop = asyncio.Event()
            drain = asyncio.ensure_future(live.drain_media(stop))
            try:
                await client.run_until_disconnected()
            finally:
                stop.set()
                await drain

        client.loop.run_until_complete(_serve())
        return 0
    except KeyboardInterrupt:
        print("\nstopped")
        return 0
    finally:
        client.disconnect()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m xauusd.run_archiver",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("mode", choices=("check", "backfill", "live"))
    parser.add_argument("--config", type=Path, required=True, help="path to config YAML")
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="apply pending schema migrations (otherwise a pending migration is an error)",
    )
    args = parser.parse_args(argv)

    clock = SystemClock()
    try:
        runtime = open_runtime(
            args.config, clock, apply_migrations=args.migrate or args.mode == "check"
        )
    except ConfigError as exc:
        print(f"configuration error:\n{exc}", file=sys.stderr)
        return 2

    try:
        return {"check": mode_check, "backfill": mode_backfill, "live": mode_live}[args.mode](
            runtime
        )
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
