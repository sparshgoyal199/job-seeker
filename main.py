#!/usr/bin/env python3
"""
Master Unified CLI for Job Intelligence & Outreach System.

Commands:
  uv run python main.py                     # Runs full daily pipeline (SAFE: preview only)
  uv run python main.py --enrich-linkedin   # Full pipeline + LinkedIn/X-Ray decision-maker discovery
  uv run python main.py --send-emails       # Full pipeline + automatically send cold emails
  uv run python main.py sync                # Ingest new jobs (WordPress + LinkedIn + Internshala + Indeed)
  uv run python main.py judge               # Run Binary LLM evaluation on unjudged jobs
  uv run python main.py enrich              # Discover decision-makers & emails (--dry-run or live)
  uv run python main.py mail                # Cold email sender (--dry-run, --test-to, or live)
  uv run python main.py apply               # Autonomous Playwright portal co-pilot (--next)
  uv run python main.py dashboard           # Open interactive HTML dashboard & export CSV
"""

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Ensure Windows terminal handles emojis cleanly
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PYTHON_EXEC = sys.executable

LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(exist_ok=True)
TIMESTAMP_STR = datetime.now().strftime("%Y%m%d_%H%M%S")
RUN_LOG_PATH = LOGS_DIR / f"daily_run_{TIMESTAMP_STR}.log"
LATEST_LOG_PATH = LOGS_DIR / "latest_run.log"


def run_step(step_name: str, cmd: list[str], log_file=None) -> bool:
    """Executes a subprocess command, streams live output, and mirrors to log_file."""
    # Ensure Python runs unbuffered so output streams to terminal in real time
    if cmd and "python" in cmd[0].lower() and "-u" not in cmd:
        cmd = [cmd[0], "-u"] + cmd[1:]

    time_str = datetime.now().strftime("%H:%M:%S")
    header = (
        f"\n{'=' * 65}\n"
        f"[{time_str}] STEP: {step_name}\n"
        f"Running: {' '.join(cmd)}\n"
        f"{'=' * 65}\n"
    )
    print(header, flush=True)
    if log_file:
        log_file.write(header)
        log_file.flush()

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
        env=env
    )

    for line in iter(process.stdout.readline, ""):
        sys.stdout.write(line)
        sys.stdout.flush()
        if log_file:
            log_file.write(line)
            log_file.flush()

    process.stdout.close()
    returncode = process.wait()

    if returncode != 0:
        err_msg = f"\n⚠️  Step '{step_name}' exited with code {returncode}\n"
        print(err_msg, flush=True)
        if log_file:
            log_file.write(err_msg)
            log_file.flush()
        return False
    return True


def run_pipeline(send_emails: bool = False, enrich_linkedin: bool = True, skip_multi_portal: bool = False,
                 multi_portal_limit: int = 40, enrich_limit: int = 50, skip_enrich: bool = False):
    """Executes the master daily pipeline in sequence with persistent logging."""
    with open(RUN_LOG_PATH, "w", encoding="utf-8", errors="replace") as log_file:
        start_time = datetime.now()
        start_msg = (
            f"{'=' * 65}\n"
            f"🚀 Daily Job Intelligence & Outreach Pipeline\n"
            f"Started at : {start_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Log file   : {RUN_LOG_PATH.resolve()}\n"
            f"{'=' * 65}\n"
        )
        print(start_msg, flush=True)
        log_file.write(start_msg)
        log_file.flush()

        # Step 1a: Incremental Sync from REST API
        ok = run_step(
            "1a/6 Sync New Jobs from WordPress REST API (Incremental)",
            [PYTHON_EXEC, "-m", "src.ingestion.wordpress_api", "--incremental"],
            log_file
        )
        if not ok:
            print("Stopping daily run due to sync failure.")
            sys.exit(1)

        # Step 1b: Ingest Open Hiring Postings from Multi-Portals (LinkedIn, Internshala, Indeed)
        if not skip_multi_portal:
            portal_limit = str(multi_portal_limit)
            run_step(
                "1b/6 Ingest Open Hiring Postings from Multi-Portals (LinkedIn, Internshala, Indeed)",
                [PYTHON_EXEC, "-m", "src.ingestion.multi_portal", "--limit", portal_limit],
                log_file
            )

        # Step 2: Strict Binary LLM Evaluation (Only for unjudged jobs)
        ok = run_step(
            "2/6 Evaluate Unjudged Jobs with Strict Binary LLM",
            [PYTHON_EXEC, "-m", "src.evaluation.llm_judge"],
            log_file
        )
        if not ok:
            print("Stopping daily run due to evaluation failure.")
            sys.exit(1)

        # Step 3: Extract Recruiter Emails from YES Jobs (Directly in Post)
        ok = run_step(
            "3/6 Extract Direct Recruiter Emails from Verified YES Jobs",
            [PYTHON_EXEC, "-m", "src.enrichment.direct_contacts"],
            log_file
        )
        if not ok:
            print("Stopping daily run due to contact extraction failure.")
            sys.exit(1)

        # Step 4: Discover Emails for WordPress Portal Jobs (4-Step Free Waterfall)
        if not skip_enrich:
            enrich_cmd = [PYTHON_EXEC, "-m", "src.enrichment.portal_contacts", "--limit", str(enrich_limit)]
            run_step(
                "4/6 Discover Hidden Recruiter Emails for WordPress Jobs (4-Step Waterfall)",
                enrich_cmd,
                log_file
            )

        # Step 4b: Discover Decision-Maker Emails via LinkedIn & X-Ray
        if enrich_linkedin:
            conn_q = sqlite3.connect("jobviareferral.db")
            pending_in_queue = conn_q.execute(
                "SELECT COUNT(*) FROM hr_contacts WHERE mail_status='pending'"
            ).fetchone()[0]
            conn_q.close()

            if pending_in_queue >= 200:
                queue_msg = (
                    f"\n{'=' * 65}\n"
                    f"📦 QUEUE STATUS: {pending_in_queue} pending emails already waiting in queue.\n"
                    f"   Skipping Step 4b enrichment for today to conserve search & Apify quota.\n"
                    f"   Step 5 will send up to 200 emails from these existing pending contacts.\n"
                    f"{'=' * 65}\n"
                )
                print(queue_msg, flush=True)
                if log_file:
                    log_file.write(queue_msg)
                    log_file.flush()
            else:
                needed = 200 - pending_in_queue
                # Each company yields ~4-8 contacts; default to max 10 companies per run (~1-2 min)
                target_comps = min(enrich_limit or 10, max(3, (needed + 3) // 4))
                run_step(
                    f"4b/6 Discover Decision-Maker LinkedIn Profiles & Emails ({pending_in_queue} waiting, targeting ~{needed} more)",
                    [PYTHON_EXEC, "-m", "src.enrichment.linkedin_leads", "--limit", str(target_comps), "--method", "auto"],
                    log_file
                )

        # Step 5: Cold Email Outreach (Safety-controlled, max 200/day)
        if send_emails:
            ok = run_step(
                "5/6 Send Pending Cold Emails via Gmail SMTP (Daily Limit: 200)",
                [PYTHON_EXEC, "-m", "src.outreach.mailer", "--limit", "200"],
                log_file
            )
        else:
            safety_msg = (
                f"\n{'=' * 65}\n"
                f"[{datetime.now().strftime('%H:%M:%S')}] STEP: 5/6 Cold Email Outreach (SAFETY MODE: Skipped)\n"
                f"ℹ️  No emails were sent. To preview emails without sending:\n"
                f"     python main.py mail --dry-run\n"
                f"   To send pending emails when you feel confident:\n"
                f"     python main.py --send-emails\n"
                f"{'=' * 65}\n"
            )
            print(safety_msg, flush=True)
            log_file.write(safety_msg)
            log_file.flush()

        # Step 6: Export & Open Dashboard
        run_step(
            "6/6 Export Matches & Open Interactive Dashboard",
            [PYTHON_EXEC, "-m", "src.reporting.dashboard", "--open"],
            log_file
        )

        end_time = datetime.now()
        duration = end_time - start_time
        summary_msg = (
            f"\n{'=' * 65}\n"
            f"✨ Daily routine completed successfully in {duration.total_seconds():.1f}s!\n"
            f"📝 Session Log : {RUN_LOG_PATH.resolve()}\n"
            f"🔗 Latest Log  : {LATEST_LOG_PATH.resolve()}\n"
            f"{'=' * 65}\n"
        )
        print(summary_msg, flush=True)
        log_file.write(summary_msg)
        log_file.flush()

    try:
        shutil.copyfile(RUN_LOG_PATH, LATEST_LOG_PATH)
    except Exception:
        pass


def run_pipeline_cli():
    """CLI handler for running the daily pipeline with flags."""
    parser = argparse.ArgumentParser(description="Master Daily Job Intelligence & Outreach Pipeline")
    parser.add_argument("--send-emails", action="store_true", help="Send pending cold emails via Gmail SMTP")
    parser.add_argument("--enrich-linkedin", action="store_true", default=True, help="Discover decision-makers via LinkedIn & X-Ray (enabled by default)")
    parser.add_argument("--no-linkedin", action="store_false", dest="enrich_linkedin", help="Skip LinkedIn decision-maker discovery")
    parser.add_argument("--skip-multi-portal", action="store_true", help="Skip external portals (LinkedIn, Internshala, Indeed)")
    parser.add_argument("--multi-portal-limit", type=int, default=40, help="Max jobs per portal (default: 40)")
    parser.add_argument("--enrich-limit", type=int, default=50, help="Max companies to enrich per run (default: 50)")
    parser.add_argument("--skip-enrich", action="store_true", help="Skip recruiter email discovery for WordPress jobs")
    args = parser.parse_args()

    run_pipeline(
        send_emails=args.send_emails,
        enrich_linkedin=args.enrich_linkedin,
        skip_multi_portal=args.skip_multi_portal,
        multi_portal_limit=args.multi_portal_limit,
        enrich_limit=args.enrich_limit,
        skip_enrich=args.skip_enrich
    )


def main():
    # If called with pipeline flags directly, or without args, default to running the pipeline
    known_commands = {"run", "sync", "judge", "enrich", "mail", "apply", "dashboard", "leads", "linkedin-leads"}
    first_arg = sys.argv[1].lower() if len(sys.argv) > 1 else ""

    if not first_arg or first_arg.startswith("-"):
        run_pipeline_cli()
        return

    cmd = sys.argv[1].lower()
    sub_args = sys.argv[2:]

    if cmd == "run":
        sys.argv = [sys.argv[0]] + sub_args
        run_pipeline_cli()

    elif cmd == "sync":
        from src.ingestion.multi_portal import main as multi_main
        from src.ingestion.wordpress_api import main as wp_main
        print("🌐 Syncing WordPress REST API...")
        sys.argv = [sys.argv[0]] + [a for a in sub_args if a != "--multi-only"]
        wp_main()
        print("\n🌐 Syncing Multi-Portal AI Internships (LinkedIn, Internshala, Indeed)...")
        multi_main()

    elif cmd == "judge":
        from src.evaluation.llm_judge import main as judge_main
        sys.argv = [sys.argv[0]] + sub_args
        judge_main()

    elif cmd in ("enrich", "leads", "linkedin-leads"):
        from src.enrichment.linkedin_leads import main as leads_main
        sys.argv = [sys.argv[0]] + sub_args
        leads_main()

    elif cmd == "mail":
        from src.outreach.mailer import main as mail_main
        sys.argv = [sys.argv[0]] + sub_args
        mail_main()

    elif cmd == "apply":
        from src.copilot.applier import main as apply_main
        sys.argv = [sys.argv[0]] + sub_args
        apply_main()

    elif cmd == "dashboard":
        from src.reporting.dashboard import main as dash_main
        sys.argv = [sys.argv[0]] + (sub_args if sub_args else ["--open"])
        dash_main()

    else:
        print(f"Unknown command: '{cmd}'. Available commands: {', '.join(known_commands)}")
        print("Usage:")
        print("  python main.py                    # Run full pipeline")
        print("  python main.py leads --limit 5    # Discover HRs & Tech Leaders at skill-matched companies")
        print("  python main.py --send-emails      # Run full pipeline + send emails")
        print("  python main.py mail --dry-run     # Preview cold emails")
        print("  python main.py dashboard          # Open interactive dashboard")


if __name__ == "__main__":
    main()
