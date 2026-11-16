"""Command dispatch and exit codes.

Exit codes (BUILD.md section 5):
    0  everything succeeded
    1  some items failed
    2  bad invocation or missing system dependency
"""

from __future__ import annotations

import argparse
import sys
import time

import psycopg

from .config import Config, ConfigError, check_dependencies
from .fetch import FetchError
from . import serve as _serve
from .pipeline import process_item
from .state import State, normalize_url
from .transcribe import Transcriber

STATUS_MARK = {"done": "[x]", "pending": "[ ]", "running": "[~]", "failed": "[!]"}


class RateLimited(Exception):
    """Stop the whole run — Instagram is rate-limiting us."""


def _looks_rate_limited(message: str) -> bool:
    low = message.lower()
    return "429" in low or "too many requests" in low


def _auth_hint(message: str) -> str | None:
    low = message.lower()
    if "login" in low or "log in" in low or "cookies" in low or "private" in low:
        return "   hint: this post may need a session -> REELSCRIBE_COOKIES=chrome reelscribe run"
    return None


# --------------------------------------------------------------------- commands


def _open_state(cfg: Config) -> State:
    return State.open(cfg.require_database_url())


def cmd_add(cfg: Config, urls: list[str]) -> int:
    state = _open_state(cfg)
    added = 0
    for url in urls:
        key = state.add(url)
        if key is None:
            print(f"already registered: {normalize_url(url)}")
        else:
            print(f"queued: {key}")
            added += 1
    print(f"\n{added} new link(s). Run `reelscribe run` to process.")
    return 0


def cmd_run(cfg: Config, only: str | None = None) -> int:
    try:
        check_dependencies()
        state = _open_state(cfg)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    lock = state.worker_lock()
    if not lock.acquire():
        print("another reelscribe worker is running", file=sys.stderr)
        return 1
    try:
        return _run_locked(cfg, state, only)
    finally:
        lock.release()


def _run_locked(cfg: Config, state: State, only: str | None) -> int:
    state.reset_stale_running()  # safe: we hold the worker lock

    if only is not None:
        key = normalize_url(only)
        state.add(only)
        state.claim(key)
        queue = [key]
    else:
        queue = state.to_process()

    if not queue:
        print("Nothing pending.")
        return 0

    print(f"Processing {len(queue)} item(s) with model '{cfg.model}'...\n", flush=True)
    transcriber = Transcriber(cfg.model, device=cfg.device, language=cfg.language)
    failures = 0
    unprocessed = list(queue)  # claimed ('running') but not yet finished

    try:
        for i, url in enumerate(queue):
            print(f"-> {url}", flush=True)
            try:
                fields = process_item(cfg, transcriber, url)
                state.mark_done(url, **fields)
                print(f"   done -> {fields['transcript_file'] or 'database'}\n", flush=True)
            except FetchError as exc:
                state.mark_failed(url, str(exc))
                if _looks_rate_limited(str(exc)):
                    unprocessed.remove(url)
                    left = len(queue) - i - 1
                    print(f"   RATE LIMITED. Stopping. {left} item(s) left unprocessed.\n",
                          file=sys.stderr)
                    return 1
                failures += 1
                print(f"   FAILED: {exc}", file=sys.stderr)
                hint = _auth_hint(str(exc))
                if hint:
                    print(hint, file=sys.stderr)
                print(file=sys.stderr)
            except Exception as exc:  # keep going through the rest of the queue
                state.mark_failed(url, str(exc))
                failures += 1
                print(f"   FAILED: {exc}\n", file=sys.stderr)
            unprocessed.remove(url)

            if cfg.delay and i < len(queue) - 1:
                time.sleep(cfg.delay)
    finally:
        state.release(unprocessed)  # rate limit / Ctrl-C: hand claimed rows back

    done = sum(1 for v in state.list_items() if v["status"] == "done")
    print(f"Finished. {done} transcript(s) total in the database")
    return 1 if failures else 0


def cmd_list(cfg: Config, failed_only: bool = False) -> int:
    items = _open_state(cfg).list_items(failed_only=failed_only)
    if not items and not failed_only:
        print("Nothing registered yet.")
        return 0
    for v in items:
        print(f"{STATUS_MARK[v['status']]} {v['url']}")
        if v["title"]:
            print(f"      {v['title']}")
        if v["last_error"]:
            error = v["last_error"].replace("\n", " ")
            if not failed_only and len(error) > 120:
                error = error[:117] + "..."
            print(f"      error: {error}")
    return 0


def cmd_retry(cfg: Config) -> int:
    n = _open_state(cfg).retry_failed()
    print(f"{n} item(s) reset to pending.")
    return 0


def cmd_clean(cfg: Config) -> int:
    state = _open_state(cfg)
    removed = 0
    for row in state.done_audio():
        audio = cfg.home / row["audio_path"]  # absolute paths pass through unchanged
        if audio.exists():
            audio.unlink()
            removed += 1
        state.clear_audio_path(row["url"])
    print(f"Deleted {removed} audio file(s). Transcripts untouched.")
    return 0


# --------------------------------------------------------------------- parser


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reelscribe")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add", help="register one or more links")
    a.add_argument("urls", nargs="+")

    r = sub.add_parser("run", help="process pending/failed items")
    r.add_argument("--only", metavar="URL", help="process just this link")

    ls = sub.add_parser("list", help="show all items")
    ls.add_argument("--failed", action="store_true", help="failures only")

    sub.add_parser("retry", help="reset failed items to pending")
    sub.add_parser("clean", help="delete audio for done items")
    sub.add_parser("serve", help="run the HTTP ingestion endpoint (needs REELSCRIBE_TOKEN)")
    return p


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        cfg = Config()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    try:
        return _dispatch(args, cfg, parser)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    except psycopg.Error as exc:
        print(f"database error: {exc}", file=sys.stderr)
        return 2


def _dispatch(args, cfg: Config, parser) -> int:
    if args.command == "add":
        return cmd_add(cfg, args.urls)
    if args.command == "run":
        return cmd_run(cfg, only=args.only)
    if args.command == "list":
        return cmd_list(cfg, failed_only=args.failed)
    if args.command == "retry":
        return cmd_retry(cfg)
    if args.command == "clean":
        return cmd_clean(cfg)
    if args.command == "serve":
        return _serve.run_server(cfg)
    parser.error(f"unknown command {args.command!r}")  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
