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

from .config import Config, ConfigError, check_dependencies
from .fetch import FetchError
from . import serve as _serve
from .pipeline import process_item
from .state import State, normalize_url
from .transcribe import Transcriber

STATUS_MARK = {"done": "[x]", "pending": "[ ]", "failed": "[!]"}


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


def cmd_add(cfg: Config, urls: list[str]) -> int:
    state = State.load(cfg.state_file)
    added = 0
    for url in urls:
        key = state.add(url)
        if key is None:
            print(f"already registered: {normalize_url(url)}")
        else:
            print(f"queued: {key}")
            added += 1
    state.save()
    print(f"\n{added} new link(s). Run `reelscribe run` to process.")
    return 0


def cmd_run(cfg: Config, only: str | None = None) -> int:
    try:
        check_dependencies()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    state = State.load(cfg.state_file)

    if only is not None:
        key = normalize_url(only)
        if key not in state.items:
            state.add(only)
        queue = [key]
    else:
        queue = state.to_process()

    if not queue:
        print("Nothing pending.")
        return 0

    print(f"Processing {len(queue)} item(s) with model '{cfg.model}'...\n", flush=True)
    transcriber = Transcriber(cfg.model, device=cfg.device, language=cfg.language)
    failures = 0

    for i, url in enumerate(queue):
        print(f"-> {url}", flush=True)
        try:
            fields = process_item(cfg, transcriber, url)
            state.mark_done(url, **fields)
            print(f"   done -> {fields['transcript']}\n", flush=True)
        except FetchError as exc:
            if _looks_rate_limited(str(exc)):
                state.mark_failed(url, str(exc))
                state.save()
                left = len(queue) - i - 1
                print(f"   RATE LIMITED. Stopping. {left} item(s) left unprocessed.\n",
                      file=sys.stderr)
                return 1
            state.mark_failed(url, str(exc))
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

        state.save()  # persist after every item

        if cfg.delay and i < len(queue) - 1:
            time.sleep(cfg.delay)

    done = sum(1 for v in state.items.values() if v["status"] == "done")
    print(f"Finished. {done} transcript(s) total in {cfg.transcript_dir}")
    return 1 if failures else 0


def cmd_list(cfg: Config, failed_only: bool = False) -> int:
    state = State.load(cfg.state_file)
    if not state.items:
        print("Nothing registered yet.")
        return 0
    for url, v in state.items.items():
        if failed_only and v["status"] != "failed":
            continue
        print(f"{STATUS_MARK[v['status']]} {url}")
        if v.get("title"):
            print(f"      {v['title']}")
        if v.get("error"):
            error = v["error"].replace("\n", " ")
            if not failed_only and len(error) > 120:
                error = error[:117] + "..."
            print(f"      error: {error}")
    return 0


def cmd_retry(cfg: Config) -> int:
    state = State.load(cfg.state_file)
    n = state.retry_failed()
    state.save()
    print(f"{n} item(s) reset to pending.")
    return 0


def cmd_clean(cfg: Config) -> int:
    from pathlib import Path

    state = State.load(cfg.state_file)
    removed = 0
    for v in state.items.values():
        if v["status"] != "done":
            continue
        audio = v.get("audio")
        if audio and Path(audio).exists():
            Path(audio).unlink()
            removed += 1
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
