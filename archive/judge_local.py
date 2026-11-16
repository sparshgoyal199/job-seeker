#!/usr/bin/env python3
"""
Local Zero-Shot Job Classifier using Hugging Face (100% Free & Offline).

Classifies job suitability strictly into YES or NO for Sparsh Goyal:
  - Role: Fresher / 0-2 yrs Python, AI/ML, or Backend Engineer
  - Location: India / Remote
  - Hard Exclusions: Senior/Lead, QA/Tester, Non-CS, 4+ yrs experience

Usage:
    python judge_local.py                 # Classify all unjudged jobs
    python judge_local.py --limit 10      # Test on 10 jobs
    python judge_local.py --rejudge       # Re-evaluate all jobs locally
"""

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure Windows terminal handles emojis cleanly
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

MODEL_NAME = "typeform/distilbert-base-uncased-mnli"

CANDIDATE_LABELS = [
    "suitable entry level fresher role for Python AI or backend developer in India",
    "unsuitable senior experienced QA non-technical or non-India job"
]

SUITABLE_LABEL = CANDIDATE_LABELS[0]

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def build_job_text(row, max_desc_chars: int = 1200) -> str:
    post_id, title, category, published, desc, apply_url, struct_json = row
    
    try:
        struct = json.loads(struct_json or "{}")
    except Exception:
        struct = {}
    
    parts = [f"Title: {title or 'Unspecified'}"]
    if "Experience Required" in struct:
        parts.append(f"Experience: {struct['Experience Required']}")
    if "Location" in struct:
        parts.append(f"Location: {struct['Location']}")
    if "Company" in struct:
        parts.append(f"Company: {struct['Company']}")
        
    cleaned_desc = (desc or "").strip()[:max_desc_chars]
    parts.append(f"Description:\n{cleaned_desc}")
    return "\n".join(parts)

def main():
    parser = argparse.ArgumentParser(description="Local Zero-Shot Job Classifier")
    parser.add_argument("--db", default="jobviareferral.db", help="Path to sqlite db")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of jobs to classify")
    parser.add_argument("--threshold", type=float, default=0.50, help="Confidence threshold for YES verdict")
    parser.add_argument("--rejudge", action="store_true", help="Re-classify already judged jobs")
    parser.add_argument("--batch-size", type=int, default=8, help="Batch size for local inference")
    args = parser.parse_args()

    db_path = Path(args.db)
    conn = sqlite3.connect(str(db_path))

    where = "fetch_status='ok'" if args.rejudge else "fetch_status='ok' AND verdict IS NULL"
    rows = conn.execute(
        f"""SELECT post_id, title, category, published_on, description_text,
                  apply_url, structured_fields
           FROM details WHERE {where} ORDER BY post_id"""
    ).fetchall()

    if args.limit:
        rows = rows[: args.limit]

    total_jobs = len(rows)
    print(f"\n🚀 Found {total_jobs} job(s) to classify using local model [{MODEL_NAME}]")
    if total_jobs == 0:
        print("Everything is already up to date!")
        conn.close()
        return

    print("Loading Hugging Face Zero-Shot pipeline into memory ...", flush=True)
    t0 = time.time()
    from transformers import pipeline
    classifier = pipeline("zero-shot-classification", model=MODEL_NAME, device=-1)
    print(f"✓ Model loaded in {time.time() - t0:.2f}s\n")

    counts = {"yes": 0, "no": 0}
    start_time = time.time()

    # Process in batches for fast vectorization
    batch_size = max(1, args.batch_size)
    for b_idx in range(0, total_jobs, batch_size):
        batch_rows = rows[b_idx : b_idx + batch_size]
        texts = [build_job_text(r) for r in batch_rows]
        
        # Batch inference
        results = classifier(
            texts,
            candidate_labels=CANDIDATE_LABELS,
            hypothesis_template="This job posting is a {}."
        )
        if not isinstance(results, list):
            results = [results]

        for r_row, res in zip(batch_rows, results):
            post_id, title = r_row[0], r_row[1]
            top_label = res["labels"][0]
            top_score = res["scores"][0]

            is_yes = (top_label == SUITABLE_LABEL and top_score >= args.threshold)
            verdict = "yes" if is_yes else "no"
            confidence = top_score if is_yes else (1.0 - top_score if top_label == SUITABLE_LABEL else top_score)
            reason = f"Zero-shot confidence: {confidence:.1%}"

            counts[verdict] += 1

            # Update DB immediately
            conn.execute(
                """UPDATE details SET
                       verdict=?, verdict_reason=?,
                       judged_at=?, judge_model=?
                   WHERE post_id=?""",
                (verdict, reason, now_iso(), f"local/{MODEL_NAME}", post_id)
            )

            status_sym = "✅ YES" if verdict == "yes" else "❌ NO "
            print(f"[{b_idx + batch_rows.index(r_row) + 1}/{total_jobs}] [{post_id}] {status_sym} ({confidence:.0%}) -> {(title or '')[:70]}")

        conn.commit()

    total_time = time.time() - start_time
    avg_speed = total_jobs / total_time if total_time > 0 else 0

    print("\n" + "=" * 65)
    print("LOCAL CLASSIFICATION SUMMARY")
    print("=" * 65)
    print(f"  Total Processed : {total_jobs} jobs in {total_time:.2f}s ({avg_speed:.1f} jobs/sec)")
    print(f"  YES (Matched)   : {counts['yes']}")
    print(f"  NO (Rejected)   : {counts['no']}")
    print(f"  Cost            : $0.00 (100% Free & Local)")

    db_totals = conn.execute(
        "SELECT verdict, count(*) FROM details GROUP BY verdict"
    ).fetchall()
    print("\nDatabase Totals:")
    for v, c in db_totals:
        print(f"  {v or 'unjudged'}: {c}")
    print("=" * 65)

    conn.close()

if __name__ == "__main__":
    main()
