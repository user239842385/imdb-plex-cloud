import argparse
from collections import Counter
from pathlib import Path

from .core import ImportProblem, apply_plan, make_plan, private_dir, read_csv, read_json, restore_backup, validate_plan, write_json
from .plex import sign_in


def parser():
    root = argparse.ArgumentParser(description="Import IMDb personal ratings into Plex's online catalog, including titles outside your server.")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("plan", "apply", "restore"):
        sub = commands.add_parser(name)
        sub.add_argument("--state-dir", type=Path, default=Path("private-state"), help="Private local state folder; never publish it")
        sub.add_argument("--no-browser", action="store_true", help="Print the Plex sign-in URL without opening a browser (NAS/headless use)")
        sub.add_argument("--rate", type=float, default=2, help="Maximum API requests per second (0.1–4; default 2)")
        if name == "plan":
            sub.add_argument("csv", type=Path, help="IMDb Your Ratings CSV export")
        else:
            sub.add_argument("--yes", action="store_true", help="Explicitly authorize writes without an interactive confirmation")
        if name == "apply":
            sub.add_argument("--dry-run", action="store_true", help="Show the plan summary without authenticating or changing anything")
    return root


def progress(index, total):
    if index % 10 == 0 or index == total:
        print(f"Processed {index}/{total}", flush=True)


def confirm(args, word, message):
    if args.yes:
        return
    print(message)
    try:
        response = input(f"Type {word} to continue: ")
    except EOFError:
        raise ImportProblem("Interactive confirmation is required. Use --yes only when you intend to authorize writes.") from None
    if response != word:
        raise ImportProblem("Cancelled; no ratings changed.")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if not .1 <= args.rate <= 4:
            raise ImportProblem("--rate must be between 0.1 and 4 requests per second.")
        if args.command == "plan":
            directory = private_dir(args.state_dir)
            path = directory / "plan.json"
            if path.exists():
                raise ImportProblem("A plan already exists here. Use a new state folder for a new CSV; use apply to resume the existing import.")
            rows, excluded = read_csv(args.csv)
            if not rows:
                raise ImportProblem("No supported valid ratings were found in the CSV.")
            client = sign_in(not args.no_browser, args.rate)
            plan = make_plan(client, rows, excluded, progress)
            write_json(path, plan)
            counts = Counter(i["status"] for i in plan["items"] + plan["excluded"])
            print("Preview saved. No ratings changed.")
            for status, count in sorted(counts.items()):
                print(f"  {status}: {count}")
            print(f"Review the private plan in {path}. Then run apply using the same state folder.")
            return 2 if counts.get("lookup_failed") else 0
        plan = read_json(args.state_dir / "plan.json")
        validate_plan(plan)
        total = sum(i["status"] == "matched" for i in plan["items"])
        if args.command == "apply" and args.dry_run:
            print(f"Dry run: {total} matched ratings in the plan. No sign-in, writes, or state changes.")
            return 0
        if not total:
            raise ImportProblem("No matched titles are available to import.")
        if args.command == "apply":
            confirm(args, "IMPORT", f"Apply up to {total} personal ratings to your online Plex account. Existing scores may be replaced; previous scores are backed up first.")
        else:
            confirm(args, "RESTORE", "Restore previous ratings from this import's private backup. Ratings changed since the import are skipped.")
        client = sign_in(not args.no_browser, args.rate)
        if args.command == "apply":
            result = apply_plan(client, plan, args.state_dir, progress)
            print(f"Complete: {len(result['completed'])} completed; {len(result['failed'])} failed. Re-run apply to retry failures or resume.")
            return 2 if result["failed"] else 0
        result = restore_backup(client, args.state_dir, progress)
        print("Restore: " + "; ".join(f"{len(v)} {k}" for k, v in result.items()))
        return 2 if result["failed"] or result["conflicts"] else 0
    except (ImportProblem, OSError, UnicodeError, ValueError) as error:
        message = str(error) if isinstance(error, ImportProblem) else "Local file or data operation failed. Check the input and private state folder."
        print("Error: " + message)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted. Keep the state folder and rerun apply to resume.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
