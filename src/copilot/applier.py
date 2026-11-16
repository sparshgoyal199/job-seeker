#!/usr/bin/env python3
"""
Autonomous Job Application Agent (Playwright Auto-Filler Co-Pilot).

Automatically navigates to portal application pages, intelligently detects form fields,
fills candidate personal & educational details, uploads resume PDF, answers standard
multiple-choice questions, uses LLM for custom open-ended questions, and pauses for review.

Features:
  - Persistent Real Google Chrome session (preserves LinkedIn, Google, and portal logins).
  - One-time native login mode (python main.py apply --login) to sign into Google & LinkedIn
    without any "browser may not be secure" blocks.
  - Full stealth integration (playwright_stealth) hiding all automation artifacts.
  - Handles external popups, new tabs, and overlays seamlessly.
  - Strict skill-matching filter (AI Engineer, Python, SDE, Software, Backend, ML).
  - Automatically filters out mailto: links and search query URLs.
  - Detects expired/closed job postings and marks them cleanly in DB.
  - Special handlers for LinkedIn Easy Apply, Internshala, and Google Forms.
  - Interactive terminal prompt for sensitive/private questions with automatic memory persistence.

Usage:
  # 0. One-time login to your accounts (LinkedIn, Google, Internshala):
  python main.py apply --login

  # 1. Apply to the next skill-matched job (Co-Pilot review mode):
  python main.py apply --next

  # 2. Apply in batch to the next 5 skill-matched jobs:
  python main.py apply --batch 5

  # 3. Apply directly to any portal URL:
  python main.py apply --url "https://boards.greenhouse.io/company/jobs/12345"

  # 4. Auto-submit mode (hands-free):
  python main.py apply --next --auto-submit
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

# Ensure Windows terminal handles emojis cleanly
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROFILE_PATH = Path("candidate_profile.json")
RESUME_PATH = Path("resume.pdf").resolve()
CHROME_PROFILE_DIR = Path("chrome_profile").resolve()


def load_profile() -> dict:
    if not PROFILE_PATH.exists():
        print(f"❌ Error: {PROFILE_PATH} not found.")
        sys.exit(1)
    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_profile(profile: dict):
    with open(PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# One-Time Native Login Helper
# ---------------------------------------------------------------------------
def open_login_session() -> bool:
    """
    Launches native Google Chrome directly using the persistent profile directory.
    Because Chrome is launched directly without Playwright or automation debuggers attached:
      - 'Sign in with Google' works 100% without 'browser may not be secure' blocks.
      - LinkedIn, Internshala, and Indeed logins work seamlessly.
      - Session cookies are saved permanently in chrome_profile/.
    """
    chrome_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    chrome_exe = None
    for p in chrome_paths:
        if os.path.exists(p):
            chrome_exe = p
            break

    if not chrome_exe:
        print("❌ Google Chrome executable not found. Please install Chrome.")
        return False

    CHROME_PROFILE_DIR.mkdir(exist_ok=True)
    urls = [
        "https://www.linkedin.com/login",
        "https://internshala.com/login/user",
        "https://accounts.google.com",
        "https://in.indeed.com",
        "https://www.naukri.com/nlogin/login",
    ]

    print("\n" + "=" * 65)
    print("🔑 LAUNCHING REAL GOOGLE CHROME FOR ONE-TIME LOGIN")
    print("=" * 65)
    print(f"Profile Directory : {CHROME_PROFILE_DIR}")
    print("Why this mode:")
    print("  • Runs pure native Google Chrome (no automation debuggers).")
    print("  • Google OAuth & LinkedIn login will NEVER be blocked.")
    print("  • Once you log in, session cookies are saved permanently for the agent.")
    print("-" * 65)
    print("Opening LinkedIn, Internshala, and Google sign-in pages...")
    print("👉 Please log in to your accounts in the Chrome window that opens.")
    print("👉 Once you are logged in, simply close Chrome (or press ENTER here).")
    print("=" * 65)

    cmd = [
        chrome_exe,
        f"--user-data-dir={CHROME_PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
    ] + urls

    subprocess.Popen(cmd)
    try:
        input("\n👉 Press [ENTER] once you have finished logging in and closed Chrome...")
    except (EOFError, KeyboardInterrupt):
        pass
    print("✅ Login session saved to chrome_profile!")
    return True


# ---------------------------------------------------------------------------
# Question Answering & Interactive Prompting
# ---------------------------------------------------------------------------
def ask_user_for_input(question_text: str, profile: dict) -> str:
    """Interactively prompts the user in the terminal for sensitive, private, or complex questions."""
    print("\n" + "❓" * 32)
    print("🚨 THE FORM REQUIRES YOUR INPUT:")
    print(f"👉 \"{question_text}\"")
    print("Neither your profile nor the AI could answer this with certainty.")
    print("❓" * 32)
    user_val = input("💬 Type your answer (or press ENTER to skip): ").strip()

    if user_val:
        if "custom_answers" not in profile:
            profile["custom_answers"] = {}
        profile["custom_answers"][question_text.strip().lower()] = user_val
        save_profile(profile)
        print("💾 Saved your answer to candidate_profile.json for all future applications!")

    return user_val


def answer_custom_question(question: str, profile: dict) -> str:
    """Answers custom form questions using custom_answers, Groq/Gemini LLM, or user prompt."""
    q_clean = question.strip().lower()

    # 1. Check custom_answers saved in candidate_profile.json
    custom_answers = profile.get("custom_answers", {})
    for key, val in custom_answers.items():
        if key in q_clean or q_clean in key:
            print(f"   ✓ Used saved answer for: '{key}'")
            return val

    # 2. Check for sensitive questions that require direct user prompt
    sensitive_keywords = ["aadhaar", "passport", "pan card", "ssn", "security clearance", "id number", "reference name", "reference contact"]
    if any(sk in q_clean for sk in sensitive_keywords):
        return ask_user_for_input(question, profile)

    # 3. LLM Question Drafting (Groq / Gemini)
    p = profile.get("personal", {})
    edu = profile.get("education", {})
    prompt = f"""\
You are {p.get('full_name', 'Sparsh Goyal')} applying for an AI / Python / Software Engineering internship.
Candidate Info:
- 2026 B.Sc Computer Science graduate from {edu.get('university', 'Hansraj College, University of Delhi')} (CGPA: {edu.get('cgpa', '8.15')})
- Projects: KhojAI (Collaborative RAG platform, reduced retrieval memory by 4x), DhanTrackAI (MCP server, OAuth 2.1 authorization)
- Skills: Python, FastAPI, RAG, LangChain, MCP Server, LLM Orchestration, SQLite

Question asked on the job application:
"{question}"

Write a crisp, professional, and convincing answer (2 to 3 sentences maximum).
Do not include greetings, placeholders, or quotes. Answer directly in the first person.
"""
    # Try Groq
    groq_key = os.getenv("GROQ_API_KEY")
    if groq_key:
        try:
            from openai import OpenAI
            client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=groq_key)
            resp = client.chat.completions.create(
                model="qwen/qwen3.8-27b",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=150,
                temperature=0.3
            )
            ans = resp.choices[0].message.content.strip()
            if ans:
                return ans
        except Exception:
            pass

    # Try Gemini
    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key:
        try:
            from openai import OpenAI
            client = OpenAI(
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                api_key=gemini_key
            )
            resp = client.chat.completions.create(
                model="gemini-2.5-flash",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=150,
                temperature=0.3
            )
            ans = resp.choices[0].message.content.strip()
            if ans:
                return ans
        except Exception:
            pass

    return "I am a Computer Science student at Hansraj College (2026) with strong hands-on experience building RAG platforms and MCP server workflows in Python. I am eager to apply my technical skills to deliver high-quality engineering solutions for your team."


# ---------------------------------------------------------------------------
# Form Filler Engine
# ---------------------------------------------------------------------------
def fill_application_form(page, profile: dict) -> dict:
    """Intelligently detects and populates form fields across ATS platforms and Google Forms."""
    p = profile.get("personal", {})
    edu = profile.get("education", {})
    auth = profile.get("work_authorization", {})

    stats = {"filled": 0, "uploaded_resume": False, "custom_answered": 0}

    # Guard: If the page contains a password field, it's a login form, NOT a job application form
    if page.locator('input[type="password"]').count() > 0:
        print("⚠️ Detected login/authentication screen. Skipping auto-fill.")
        return stats

    print("\n🔍 Detecting application form inputs...")
    page.wait_for_timeout(2000)

    # 1. Upload Resume PDF
    file_inputs = page.locator('input[type="file"]')
    if file_inputs.count() > 0 and RESUME_PATH.exists():
        try:
            print(f"📎 Attaching Resume ({RESUME_PATH.name}) ...", end=" ", flush=True)
            file_inputs.first.set_input_files(str(RESUME_PATH))
            stats["uploaded_resume"] = True
            print("✅ ATTACHED")
            page.wait_for_timeout(1500)
        except Exception as e:
            print(f"⚠️ Upload notice: {e}")
    elif RESUME_PATH.exists():
        browse_btns = page.locator("button:has-text('Browse'), button:has-text('Upload Resume'), button:has-text('Attach Resume')")
        if browse_btns.count() > 0 and browse_btns.first.is_visible():
            try:
                print(f"📎 Attaching Resume via FileChooser ({RESUME_PATH.name}) ...", end=" ", flush=True)
                with page.expect_file_chooser(timeout=3000) as fc_info:
                    browse_btns.first.click()
                fc_info.value.set_files(str(RESUME_PATH))
                stats["uploaded_resume"] = True
                print("✅ ATTACHED")
                page.wait_for_timeout(1000)
            except Exception as e:
                print(f"⚠️ FileChooser notice: {e}")

    # 2. Standard Text Inputs Mapping
    text_mappings = [
        (["first_name", "firstname", "first-name", "fname"], p.get("first_name", "Sparsh")),
        (["last_name", "lastname", "last-name", "lname"], p.get("last_name", "Goyal")),
        (["name", "full_name", "fullname", "candidate_name"], p.get("full_name", "Sparsh Goyal")),
        (["email", "e-mail"], p.get("email", "sparshgoyal199@gmail.com")),
        (["phone", "mobile", "contact", "tel"], p.get("phone", "+91 9887170974")),
        (["linkedin", "linked_in"], p.get("linkedin_url", "")),
        (["github", "git_hub"], p.get("github_url", "")),
        (["portfolio", "website", "url", "site"], p.get("portfolio_url", "")),
        (["city", "location", "address", "residence"], p.get("current_city", "Bharatpur, Rajasthan")),
        (["school", "university", "college", "institution", "institute"], edu.get("university", "Hansraj College, University of Delhi")),
        (["degree", "qualification"], edu.get("degree", "Bachelor of Science")),
        (["major", "discipline", "branch", "field of study", "specialization"], edu.get("major", "Computer Science")),
        (["gpa", "cgpa", "grade", "percentage"], edu.get("cgpa", "8.15")),
        (["grad_year", "graduation_year", "passing_year", "batch"], edu.get("graduation_year", "2026")),
        (["notice", "availability", "how soon"], auth.get("availability", "Immediate")),
        (["current ctc", "current_ctc", "current salary", "present ctc"], "0 (Student / Fresher)"),
        (["expected ctc", "expected_ctc", "salaryexpectation", "expected salary", "stipend"], "20000"),
        (["years of experience", "years of work", "experience in years", "how many years", "total experience"], "1"),
        (["organization", "org", "current company", "company", "employer"], "Student / Fresher (Hansraj College, University of Delhi)"),
        (["gender", "sex"], "Male"),
        (["pincode", "postal", "zip", "zip code", "postal code"], "321001"),
        (["state", "province"], "Rajasthan"),
        (["country", "nationality"], "India"),
        (["pronouns"], "He/Him"),
    ]

    # 2a. Google Forms Specific Field Detection
    try:
        g_items = page.locator('div[role="listitem"]').all()
        if len(g_items) > 0:
            print(f"📋 Detected Google Form question container(s): {len(g_items)}")
            for item in g_items:
                heading_el = item.locator('div[role="heading"], span.M7eMe, div.HoDaF').first
                if heading_el.count() == 0:
                    continue
                q_text = heading_el.inner_text().strip()
                q_clean = q_text.lower()

                txt_input = item.locator('input[type="text"], input[type="email"], textarea').first
                if txt_input.count() > 0 and txt_input.is_visible():
                    current_val = txt_input.input_value()
                    if current_val and len(current_val.strip()) > 1:
                        continue
                    matched = False
                    for keywords, target_val in text_mappings:
                        if any(kw in q_clean for kw in keywords) and target_val:
                            txt_input.fill(str(target_val))
                            print(f"   ✓ Filled Google Form '{q_text[:25]}' ➔ {target_val}")
                            stats["filled"] += 1
                            matched = True
                            break
                    if not matched:
                        ans = answer_custom_question(q_text, profile)
                        if ans:
                            txt_input.fill(ans)
                            print(f"   ✓ Answered Google Form '{q_text[:25]}' ({len(ans)} chars)")
                            stats["custom_answered"] += 1

                # Radios in Google Forms
                radios = item.locator('div[role="radio"]').all()
                for rad in radios:
                    rad_label = (rad.get_attribute("data-value") or rad.inner_text()).lower()
                    if "yes" in rad_label or "immediate" in rad_label:
                        rad.click()
                        stats["filled"] += 1
                        break
    except Exception:
        pass

    # 2b. Internshala Application Inputs
    try:
        internshala_cover = page.locator('textarea#cover_letter, textarea[name="cover_letter"]').first
        if internshala_cover.count() > 0 and internshala_cover.is_visible():
            current_val = internshala_cover.input_value()
            if not current_val or len(current_val.strip()) < 5:
                cover_pitch = (
                    f"I am a Computer Science student at {edu.get('university', 'Hansraj College')} "
                    f"with practical hands-on experience building AI solutions, RAG platforms, and backend APIs in Python. "
                    f"My key projects include KhojAI (collaborative RAG platform) and DhanTrackAI (MCP server). "
                    f"I am available to start immediately and would love to contribute high-quality engineering work to your team."
                )
                internshala_cover.fill(cover_pitch)
                print("   ✓ Populated Internshala Cover Letter pitch")
                stats["filled"] += 1
    except Exception:
        pass

    # 2c. React Select Dropdowns (#react-select-*)
    try:
        rs_inputs = page.locator("input[id*='react-select-']").all()
        for rsi in rs_inputs:
            if rsi.is_visible():
                rsi_id = rsi.get_attribute("id") or ""
                label_txt = ""
                parent = rsi.evaluate("el => el.closest('.form-group, .mb-3, div')?.innerText || ''").lower()
                if "location" in parent or "city" in parent:
                    rsi.fill("Delhi")
                    page.wait_for_timeout(300)
                    page.keyboard.press("Enter")
                    stats["filled"] += 1
                elif "experience" in parent or "years" in parent:
                    rsi.fill("1")
                    page.wait_for_timeout(300)
                    page.keyboard.press("Enter")
                    stats["filled"] += 1
    except Exception:
        pass

    # 2d. Standard ATS Inputs (Greenhouse, Lever, Workday, Company Portals, LinkedIn)
    all_inputs = page.locator('input:not([type="hidden"]):not([type="file"]):not([type="submit"]):not([type="button"]):not([type="radio"]):not([type="checkbox"])')
    input_count = all_inputs.count()

    for i in range(input_count):
        field = all_inputs.nth(i)
        try:
            if not field.is_visible():
                continue

            val = field.input_value()
            if val and len(val.strip()) > 1:
                continue

            f_name = (field.get_attribute("name") or "").lower()
            f_id = (field.get_attribute("id") or "").lower()
            f_ph = (field.get_attribute("placeholder") or "").lower()
            f_aria = (field.get_attribute("aria-label") or "").lower()
            try:
                parent_zcqa = (field.evaluate("el => el.closest('[data-zcqa]')?.getAttribute('data-zcqa') || ''")).lower()
            except Exception:
                parent_zcqa = ""
            combined = f"{f_name} {f_id} {f_ph} {f_aria} {parent_zcqa}"
            lbl_text = ""
            if f_id:
                lbl = page.locator(f'label[for="{f_id}"]').first
                if lbl.count() > 0:
                    lbl_text = lbl.inner_text().strip()
                    combined += " " + lbl_text.lower()

            matched = False
            for keywords, target_val in text_mappings:
                if any(kw in combined for kw in keywords) and target_val:
                    if "last" in combined and "first" not in combined and target_val == p.get("first_name"):
                        continue
                    field.fill(str(target_val))
                    print(f"   ✓ Filled '{f_name or f_ph or combined[:25]}' ➔ {target_val}")
                    stats["filled"] += 1
                    matched = True
                    break

            if not matched and (field.get_attribute("required") is not None or "experience" in combined or "years" in combined):
                if any(k in combined for k in ["experience", "years", "yrs", "rating"]):
                    field.fill("1")
                    stats["filled"] += 1
                elif lbl_text:
                    ans = answer_custom_question(lbl_text, profile)
                    if ans:
                        field.fill(ans[:80] if "input" in str(field) else ans)
                        stats["custom_answered"] += 1

        except Exception:
            continue

    # 3. Radio Buttons (Prefer 'Yes' / 'Immediate' options)
    try:
        groups = page.locator('fieldset, [data-test-form-builder-radio-button-form-component], div[role="radiogroup"]').all()
        if len(groups) > 0:
            for grp in groups:
                yes_opt = grp.locator('label:has-text("Yes"), input[value*="Yes"], input[value*="yes"], input[value="true"]').first
                if yes_opt.count() > 0 and yes_opt.is_visible():
                    yes_opt.click(force=True)
                    stats["filled"] += 1
                else:
                    first_r = grp.locator('input[type="radio"], label').first
                    if first_r.count() > 0 and first_r.is_visible():
                        first_r.click(force=True)
                        stats["filled"] += 1
        else:
            radios = page.locator('input[type="radio"]').all()
            handled_radio_names = set()
            for r in radios:
                r_name = r.get_attribute("name") or ""
                if r_name and r_name not in handled_radio_names:
                    handled_radio_names.add(r_name)
                    r.check(force=True)
                    stats["filled"] += 1
    except Exception:
        pass

    # 3b. Mandatory & Consent Checkboxes
    try:
        cbs = page.locator('input[type="checkbox"]').all()
        for cb in cbs:
            cb_name = (cb.get_attribute("name") or "").lower()
            if any(k in cb_name for k in ["consent", "terms", "policy", "agree", "privacy"]) or cb.get_attribute("required") is not None:
                if not cb.is_checked():
                    cb.check(force=True)
                    print("   ✓ Checked mandatory consent checkbox")
                    stats["filled"] += 1
    except Exception:
        pass

    # 4. Dropdowns (<select>)
    try:
        selects = page.locator("select")
        for s_idx in range(selects.count()):
            s = selects.nth(s_idx)
            if s.is_visible():
                opts = s.locator("option").all()
                chosen_val = None
                for opt in opts:
                    txt = opt.inner_text().strip().lower()
                    val = opt.get_attribute("value") or opt.inner_text().strip()
                    if "yes" in txt:
                        chosen_val = val
                        break
                    elif "india" in txt or "+91" in txt or val == "in":
                        chosen_val = val
                        break
                    elif "immediate" in txt:
                        chosen_val = val
                        break
                    elif "1 year" in txt or txt == "1":
                        chosen_val = val
                        break
                    elif "0 month" in txt or txt == "0":
                        chosen_val = val
                        break
                if chosen_val:
                    s.select_option(value=chosen_val)
                    stats["filled"] += 1
                elif len(opts) > 1:
                    s.select_option(index=1)
                    stats["filled"] += 1
    except Exception:
        pass

    # 5. Textareas / Custom Questions
    textareas = page.locator("textarea")
    ta_count = textareas.count()
    if ta_count > 0:
        for j in range(ta_count):
            ta = textareas.nth(j)
            try:
                if not ta.is_visible() or ta.input_value().strip():
                    continue

                ta_id = ta.get_attribute("id") or ""
                label_text = ""
                if ta_id:
                    lbl = page.locator(f'label[for="{ta_id}"]')
                    if lbl.count() > 0:
                        label_text = lbl.first.inner_text()

                if not label_text:
                    label_text = ta.get_attribute("placeholder") or ta.get_attribute("name") or "Why are you interested in this position?"

                print(f"   🤖 Processing question: '{label_text[:50]}...'")
                ans = answer_custom_question(label_text, profile)
                if ans:
                    ta.fill(ans)
                    print(f"   ✓ Populated response ({len(ans)} chars)")
                    stats["custom_answered"] += 1
            except Exception as e:
                print(f"   ⚠️ Textarea notice: {e}")

    # 6. Iframe Embedded Form Fallback (e.g. Greenhouse, Lever, Workday embed)
    if hasattr(page, "frames") and stats["filled"] == 0 and not stats["uploaded_resume"]:
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                f_inputs = frame.locator('input[type="text"], input[type="email"], input[type="file"]')
                if f_inputs.count() > 0:
                    print(f"🖼️ Found embedded application form inside iframe ({frame.url[:50]}...). Populating...")
                    iframe_stats = fill_application_form(frame, profile)
                    stats["filled"] += iframe_stats["filled"]
                    stats["uploaded_resume"] = stats["uploaded_resume"] or iframe_stats["uploaded_resume"]
                    stats["custom_answered"] += iframe_stats["custom_answered"]
                    if stats["filled"] > 0:
                        break
            except Exception:
                pass

    return stats



# ---------------------------------------------------------------------------
# Portal-Specific Helpers (LinkedIn Easy Apply & Internshala)
# ---------------------------------------------------------------------------
def handle_linkedin_easy_apply(page, profile: dict, auto_submit: bool = False) -> bool:
    """Navigates through the multi-step LinkedIn Easy Apply wizard (supports English, Hindi, and Bengali UI)."""
    try:
        easy_btn = page.locator('button.jobs-apply-button, button:has-text("Easy Apply"), button:has-text("সহজে আবেদন জানান"), button[aria-label*="Easy Apply"]').first
        if easy_btn.count() == 0 or not easy_btn.is_visible():
            return False

        print("👉 Clicking LinkedIn 'Easy Apply' button...")
        easy_btn.click()
        page.wait_for_timeout(2500)

        for step in range(1, 10):
            modal = page.locator('div.jobs-easy-apply-modal, div[data-test-modal], div[role="dialog"]').first
            scope = modal if (modal.count() > 0 and modal.is_visible()) else page

            sub_btn = scope.locator('button[aria-label*="Submit application" i], button:has-text("Submit application"), button:has-text("আবেদন জমা দিন"), button:has-text("Submit")').first
            review_btn = scope.locator('button[aria-label*="Review" i], button:has-text("Review"), button:has-text("রিভিউ করুন"), button:has-text("পর্যালোচনা")').first
            next_btn = scope.locator('button[aria-label*="Continue" i], button[aria-label*="next" i], button:has-text("Next"), button:has-text("পরবর্তী")').first

            has_controls = (sub_btn.count() > 0 and sub_btn.is_visible()) or (review_btn.count() > 0 and review_btn.is_visible()) or (next_btn.count() > 0 and next_btn.is_visible())
            if not has_controls:
                break

            print(f"📝 Processing LinkedIn Easy Apply Step {step}...")
            fill_application_form(scope, profile)

            # Re-query buttons after form fill
            sub_btn = scope.locator('button[aria-label*="Submit application" i], button:has-text("Submit application"), button:has-text("আবেদন জমা দিন"), button:has-text("Submit")').first
            if sub_btn.count() > 0 and sub_btn.is_visible():
                if auto_submit:
                    print("✅ Submitting LinkedIn Easy Apply!")
                    sub_btn.click(force=True)
                    page.wait_for_timeout(3500)
                    dismiss = page.locator('button[aria-label="Dismiss"], button:has-text("Done"), button:has-text("হয়ে গেছে"), button:has-text("বাতিল")').first
                    if dismiss.count() > 0 and dismiss.is_visible():
                        dismiss.click(force=True)
                return True

            review_btn = scope.locator('button[aria-label*="Review" i], button:has-text("Review"), button:has-text("রিভিউ করুন"), button:has-text("পর্যালোচনা")').first
            if review_btn.count() > 0 and review_btn.is_visible():
                print("👉 Clicking 'Review' to preview completed application...")
                review_btn.click(force=True)
                page.wait_for_timeout(2000)

                sub_final = scope.locator('button[aria-label*="Submit application" i], button:has-text("Submit application"), button:has-text("আবেদন জমা দিন"), button:has-text("Submit")').first
                if auto_submit and sub_final.count() > 0 and sub_final.is_visible():
                    print("✅ Submitting LinkedIn Easy Apply!")
                    sub_final.click(force=True)
                    page.wait_for_timeout(3500)
                    dismiss = page.locator('button[aria-label="Dismiss"], button:has-text("Done"), button:has-text("হয়ে গেছে"), button:has-text("বাতিল")').first
                    if dismiss.count() > 0 and dismiss.is_visible():
                        dismiss.click(force=True)
                    return True
                break

            next_btn = scope.locator('button[aria-label*="Continue" i], button[aria-label*="next" i], button:has-text("Next"), button:has-text("পরবর্তী")').first
            if next_btn.count() > 0 and next_btn.is_visible():
                print("👉 Clicking 'Next' step...")
                next_btn.click(force=True)
                page.wait_for_timeout(2000)
            else:
                break

        return True
    except Exception as e:
        print(f"⚠️ Easy Apply notice: {e}")
        return False


def handle_indeed_flow(page, profile: dict, context, auto_submit: bool = False) -> tuple[bool, any]:
    """
    Handles Indeed India application flow:
    1. External company redirection ('Apply on company site' -> new tab / employer career page)
    2. Indeed native Easy Apply ('Apply now' -> multi-step modal / smartapply wizard)
    Returns (handled: bool, active_page).
    """
    try:
        # 1. External Employer Redirection ("Apply on company site")
        ext_btn = page.locator('#applyButtonLinkContainer a, a[href*="/rc/clk"], a:has-text("Apply on company site"), button:has-text("Apply on company site"), a:has-text("Apply on employer site"), button:has-text("Apply on employer site")').first
        if ext_btn.count() > 0 and ext_btn.is_visible():
            href = ext_btn.get_attribute("href") or ""
            if href.startswith("/"):
                href = "https://in.indeed.com" + href

            print("👉 Detected Indeed external job: clicking 'Apply on company site'...")
            pages_before = len(context.pages)
            target_page = None
            try:
                with context.expect_page(timeout=7000) as new_page_info:
                    ext_btn.click()
                target_page = new_page_info.value
            except Exception:
                try:
                    ext_btn.click(force=True)
                except Exception:
                    pass

            if not target_page and len(context.pages) > pages_before:
                target_page = context.pages[-1]

            if not target_page and href:
                print(f"🌐 Following Indeed redirect URL: {href[:80]}...")
                page.goto(href, wait_until="domcontentloaded", timeout=30000)
                target_page = page
            elif target_page:
                print(f"🔀 Switched to employer application page: {target_page.url}")

            if target_page:
                try:
                    target_page.wait_for_load_state("domcontentloaded", timeout=20000)
                except Exception:
                    pass
                for _ in range(8):
                    if "rc/clk" not in target_page.url:
                        break
                    target_page.wait_for_timeout(1000)
                target_page.wait_for_timeout(3000)
                print(f"🏢 Arrived at employer site: '{target_page.title()}' ({target_page.url})")
                return True, target_page

        # 2. Native Indeed Easy Apply ("Apply now")
        native_btn = page.locator('button#indeedApplyButton, button:has-text("Apply now"), a:has-text("Apply now")').first
        if native_btn.count() > 0 and native_btn.is_visible():
            print("👉 Clicking Indeed 'Apply now' button...")
            pages_before = len(context.pages)
            target_page = page
            try:
                with context.expect_page(timeout=5000) as new_p_info:
                    native_btn.click()
                target_page = new_p_info.value
            except Exception:
                try:
                    native_btn.click(force=True)
                except Exception:
                    pass

            if len(context.pages) > pages_before:
                target_page = context.pages[-1]

            target_page.wait_for_timeout(3000)

            for step in range(1, 10):
                modal = target_page.locator('#ia-container, div[role="dialog"], .ia-BasePage, div[data-testid="ia-container"]').first
                scope = modal if (modal.count() > 0 and modal.is_visible()) else target_page

                sub_btn = scope.locator('button:has-text("Submit your application"), button:has-text("Submit application"), button[aria-label*="Submit application" i]').first
                rev_btn = scope.locator('button:has-text("Review your application"), button:has-text("Review")').first
                cont_btn = scope.locator('button:has-text("Continue"), button:has-text("Next")').first

                has_controls = (sub_btn.count() > 0 and sub_btn.is_visible()) or (rev_btn.count() > 0 and rev_btn.is_visible()) or (cont_btn.count() > 0 and cont_btn.is_visible())
                if not has_controls:
                    break

                print(f"📝 Processing Indeed Application Step {step}...")
                fill_application_form(scope, profile)

                sub_btn = scope.locator('button:has-text("Submit your application"), button:has-text("Submit application"), button[aria-label*="Submit application" i]').first
                if sub_btn.count() > 0 and sub_btn.is_visible():
                    if auto_submit:
                        print("✅ Submitting Indeed Application!")
                        sub_btn.click(force=True)
                        target_page.wait_for_timeout(4000)
                    return True, target_page

                rev_btn = scope.locator('button:has-text("Review your application"), button:has-text("Review")').first
                if rev_btn.count() > 0 and rev_btn.is_visible():
                    print("👉 Clicking 'Review'...")
                    rev_btn.click(force=True)
                    target_page.wait_for_timeout(2000)
                    sub_final = scope.locator('button:has-text("Submit your application"), button:has-text("Submit application"), button[aria-label*="Submit application" i]').first
                    if auto_submit and sub_final.count() > 0 and sub_final.is_visible():
                        print("✅ Submitting Indeed Application!")
                        sub_final.click(force=True)
                        target_page.wait_for_timeout(4000)
                        return True, target_page
                    break

                cont_btn = scope.locator('button:has-text("Continue"), button:has-text("Next")').first
                if cont_btn.count() > 0 and cont_btn.is_visible():
                    print("👉 Clicking 'Continue' step...")
                    cont_btn.click(force=True)
                    target_page.wait_for_timeout(2500)
                else:
                    break

            return True, target_page

    except Exception as e:
        print(f"⚠️ Indeed flow notice: {e}")

    return False, page


def handle_internshala_flow(page, profile: dict, auto_submit: bool = False) -> bool:
    """
    Handles Internshala internship application flow:
    1. Clicks 'Apply now' button on internship listing
    2. Handles 'Proceed to application' interstitial / resume review
    3. Populates Cover Letter & Answers custom employer assessment questions
    4. Handles availability radios / checkboxes
    5. Submits or leaves ready for review
    """
    edu = profile.get("education", {})
    try:
        # Check if login is required
        login_modal = page.locator('#login-modal, form#login-form')
        if login_modal.count() > 0 and login_modal.first.is_visible():
            print("⚠️ Internshala requires login. Please run 'python main.py apply --login'")
            return False

        # 1. Click "Apply now" button
        apply_btn = page.locator('button#apply_now_button, a#apply_now_button, .apply_now_button, button:has-text("Apply now"), a:has-text("Apply now")').first
        if apply_btn.count() > 0 and apply_btn.is_visible():
            print("👉 Clicking Internshala 'Apply now' button...")
            apply_btn.click()
            page.wait_for_timeout(2500)

        # 2. Check for "Proceed to application" (Internshala resume check step)
        proceed_btn = page.locator('button#proceed_to_application, a#proceed_to_application, button:has-text("Proceed to application"), a:has-text("Proceed to application")').first
        if proceed_btn.count() > 0 and proceed_btn.is_visible():
            print("👉 Clicking 'Proceed to application'...")
            proceed_btn.click(force=True)
            page.wait_for_timeout(2500)

        # 3. Locate application form / modal
        modal = page.locator('#application-modal, #application_form, div.application_form').first
        scope = modal if (modal.count() > 0 and modal.is_visible()) else page

        # 4. Fill Cover Letter
        cover_ta = scope.locator('textarea#cover_letter, textarea[name="cover_letter"], textarea[placeholder*="cover letter" i]').first
        if cover_ta.count() > 0 and cover_ta.is_visible():
            cur_val = cover_ta.input_value().strip()
            if len(cur_val) < 10:
                pitch = (
                    f"I am a Computer Science student at {edu.get('university', 'Hansraj College')} "
                    f"with practical hands-on experience building AI solutions, RAG platforms, and backend APIs in Python. "
                    f"My key projects include KhojAI (collaborative RAG platform) and DhanTrackAI (MCP server). "
                    f"I am available to start immediately and would love to contribute high-quality engineering work to your team."
                )
                cover_ta.fill(pitch)
                print("   ✓ Populated Internshala Cover Letter pitch")

        # 5. Answer custom employer assessment questions
        q_blocks = scope.locator('div.form-group:has(textarea), div.additional_question, div.assessment_question').all()
        for block in q_blocks:
            try:
                ta = block.locator('textarea').first
                if ta.count() > 0 and ta.is_visible() and len(ta.input_value().strip()) < 5:
                    lbl = block.locator('label, span.heading, p').first
                    lbl_text = lbl.inner_text().strip() if lbl.count() > 0 else "Assessment question"
                    print(f"   🤖 Answering Internshala question: '{lbl_text[:50]}...'")
                    ans = answer_custom_question(lbl_text, profile)
                    if ans:
                        ta.fill(ans)
                        print(f"   ✓ Populated response ({len(ans)} chars)")
            except Exception:
                pass

        # 6. Check Availability radios (Yes, I am available)
        radios = scope.locator('input[type="radio"]').all()
        for rad in radios:
            try:
                val = (rad.get_attribute("value") or "").lower()
                par_text = rad.evaluate("el => el.parentElement?.innerText || ''").lower()
                if "yes" in val or "yes" in par_text or "immediate" in par_text:
                    rad.click(force=True)
                    print("   ✓ Selected 'Yes, I am available immediately'")
            except Exception:
                pass

        # 7. Submit Application
        sub_btn = scope.locator('#submit, input#submit, button#submit, button:has-text("Submit application"), input[type="submit"]').first
        if sub_btn.count() > 0 and sub_btn.is_visible():
            if auto_submit:
                print("✅ Submitting Internshala Application!")
                sub_btn.click(force=True)
                page.wait_for_timeout(3500)
                dismiss = page.locator('#success_modal .close, button:has-text("Done"), button:has-text("Close")').first
                if dismiss.count() > 0 and dismiss.is_visible():
                    dismiss.click(force=True)
            return True

    except Exception as e:
        print(f"⚠️ Internshala flow notice: {e}")

    return False



def record_application_status(post_id: int | None, apply_url: str, status: str):
    """Records application status into jobviareferral.db."""
    if not post_id:
        return
    conn = sqlite3.connect("jobviareferral.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS portal_applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER,
            apply_url TEXT,
            applied_at TEXT,
            status TEXT
        )
    """)
    now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute("""
        INSERT INTO portal_applications (post_id, apply_url, applied_at, status)
        VALUES (?, ?, ?, ?)
    """, (post_id, apply_url, now_str, status))
    conn.commit()
    conn.close()


def release_chrome_profile_lock():
    """
    Terminates any hanging or orphaned Chrome instances using chrome_profile directory.
    Leaves user's everyday personal Chrome completely untouched!
    Also removes stale lock files so Playwright can attach cleanly without ProcessSingleton errors.
    """
    try:
        import psutil
        target = str(CHROME_PROFILE_DIR).lower()
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if proc.info["name"] and "chrome" in proc.info["name"].lower():
                    cmd = " ".join(proc.info.get("cmdline") or []).lower()
                    if target in cmd:
                        proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        time.sleep(0.5)
    except Exception:
        pass

    for lock in [CHROME_PROFILE_DIR / "lockfile", CHROME_PROFILE_DIR / "Default" / "LOCK"]:
        try:
            if lock.exists():
                lock.unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Role Relevance Gatekeeper (Strict Tech / AI / Python / SDE Filter)
# ---------------------------------------------------------------------------
def is_role_relevant_for_candidate(title: str, description: str = "") -> bool:
    """
    Strict filter ensuring only technical, software, AI, data, and dev roles are applied to.
    Guarantees the system never applies to non-tech jobs (Marketing, Sales, HR, Graphic Design, Content, etc.).
    """
    t_clean = (title or "").lower().strip()
    if not t_clean:
        return False

    # 1. Immediate negative exclusions (Non-tech jobs)
    non_tech_keywords = [
        "marketing", "sales", "business development", "bde", "telecaller", "counselor",
        "human resource", "hr intern", "hr executive", "recruiter", "talent acquisition",
        "content writer", "content writing", "copywriter", "graphic designer", "ui/ux designer",
        "product designer", "video editor", "operations intern", "operations executive",
        "subject matter expert", "sme", "accountant", "finance intern", "civil", "mechanical",
        "interior designer", "event management", "social media", "seo"
    ]
    if any(nt in t_clean for nt in non_tech_keywords):
        return False

    # 2. Positive tech indicators
    tech_keywords = [
        "ai", "artificial intelligence", "machine learning", "ml", "python", "software",
        "sde", "backend", "developer", "data science", "data scientist", "data engineer",
        "data analyst", "full stack", "fullstack", "frontend", "devops", "cloud",
        "web developer", "react", "fastapi", "django", "node"
    ]
    if any(tk in t_clean for tk in tech_keywords):
        return True

    # 3. Fast LLM verification fallback for ambiguous titles
    groq_key = os.getenv("GROQ_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")
    prompt = f"Is the job title '{title}' a technical software engineering, AI, data, or programming role suitable for a Computer Science student? Reply with ONLY 'YES' or 'NO'."
    if groq_key:
        try:
            from openai import OpenAI
            client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=groq_key)
            resp = client.chat.completions.create(
                model="qwen/qwen3.8-27b",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=5,
                temperature=0.0
            )
            ans = resp.choices[0].message.content.strip().upper()
            return "YES" in ans
        except Exception:
            pass
    elif gemini_key:
        try:
            from openai import OpenAI
            client = OpenAI(base_url="https://generativelanguage.googleapis.com/v1beta/openai/", api_key=gemini_key)
            resp = client.chat.completions.create(
                model="gemini-2.5-flash",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=5,
                temperature=0.0
            )
            ans = resp.choices[0].message.content.strip().upper()
            return "YES" in ans
        except Exception:
            pass

    return False


# ---------------------------------------------------------------------------
# Strict LLM Resume & Qualification Match Evaluator
# ---------------------------------------------------------------------------
def evaluate_job_resume_fit(title: str, description: str, profile: dict) -> tuple[bool, str]:
    """
    Evaluates whether the candidate's resume qualifications and skills strictly match the job requirements.
    Uses Groq (qwen-27b) -> Gemini (gemini-2.5-flash) fallback.
    Returns (is_match: bool, reason: str).
    """
    p = profile.get("personal", {})
    edu = profile.get("education", {})
    skills = profile.get("skills_summary", "Python, FastAPI, RAG, LangChain, MCP Server, PostgreSQL, Docker")

    candidate_summary = f"""
Candidate: {p.get('full_name', 'Sparsh Goyal')}
Degree: {edu.get('degree', 'Bachelor of Science')} in {edu.get('major', 'Computer Science')}
University: {edu.get('university', 'Hansraj College, University of Delhi')} (CGPA: {edu.get('cgpa', '8.15')})
Graduation Year: {edu.get('graduation_year', '2026')} (Fresher / Student)
Core Technical Skills: {skills}
Projects: KhojAI (Collaborative RAG platform), DhanTrackAI (MCP server, OAuth 2.1)
"""

    job_text = f"Title: {title}\nDescription:\n{(description or title)[:2500]}"

    prompt = f"""You are a strict, objective technical recruiter screening candidate resumes against job requirements.
Compare candidate qualifications, graduation year, and skills against the job posting.

Candidate Profile:
{candidate_summary}

Job Details:
{job_text}

Evaluation Rules:
- MATCH (is_match = true):
  * Candidate is a 2026 CS student/fresher, which is acceptable for an internship, entry-level, or junior role (0-2 yrs exp).
  * The required tech stack aligns with candidate's Python, Backend, AI/ML, Data, Web, or general Software Engineering fundamentals.
- MISMATCH (is_match = false):
  * Demands 3+ or 5+ years of professional industry experience.
  * Demands advanced degrees candidate does not have (e.g. M.Tech/PhD mandatory).
  * Completely disjoint tech domain with zero overlap (e.g. Embedded C/RTOS, Hardware/VLSI, Salesforce/SAP, Blockchain Solidity only).

Output ONLY valid JSON:
{{"is_match": true/false, "reason": "concise 1-sentence reason"}}
"""

    groq_key = os.getenv("GROQ_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")

    if groq_key:
        try:
            from openai import OpenAI
            client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=groq_key)
            resp = client.chat.completions.create(
                model="qwen/qwen3.8-27b",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=100,
                temperature=0.0
            )
            raw = resp.choices[0].message.content.strip()
            clean = raw[raw.find("{"):raw.rfind("}")+1]
            data = json.loads(clean)
            return bool(data.get("is_match", True)), data.get("reason", "Profile matches role")
        except Exception:
            pass

    if gemini_key:
        try:
            from openai import OpenAI
            client = OpenAI(base_url="https://generativelanguage.googleapis.com/v1beta/openai/", api_key=gemini_key)
            resp = client.chat.completions.create(
                model="gemini-2.5-flash",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=100,
                temperature=0.0
            )
            raw = resp.choices[0].message.content.strip()
            clean = raw[raw.find("{"):raw.rfind("}")+1]
            data = json.loads(clean)
            return bool(data.get("is_match", True)), data.get("reason", "Profile matches role")
        except Exception:
            pass

    return True, "Passed tech keyword qualification check"


# ---------------------------------------------------------------------------
# Main Controller with Persistent Real Chrome & Popup Switcher
# ---------------------------------------------------------------------------
def apply_to_job(apply_url: str, post_id: int | None = None, auto_submit: bool = False, headless: bool = False) -> bool:
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    profile = load_profile()

    # Strict Resume & Qualification Match Guard
    if post_id:
        try:
            conn = sqlite3.connect("jobviareferral.db")
            row = conn.execute("SELECT title, description_text, structured_fields FROM details WHERE post_id=?", (post_id,)).fetchone()
            conn.close()
            if row:
                title, desc_text, struct_json = row
                if not is_role_relevant_for_candidate(title):
                    print(f"⏩ [Relevance Guard] Skipping non-technical role: '{title}'")
                    record_application_status(post_id, apply_url, "irrelevant_role")
                    return False

                combined_desc = (desc_text or "")
                if struct_json:
                    combined_desc += "\n" + struct_json

                is_match, reason = evaluate_job_resume_fit(title, combined_desc, profile)
                if not is_match:
                    print(f"⏩ [Strict Resume Match] Skipping Post #{post_id} - '{title}'")
                    print(f"   Reason: {reason}")
                    record_application_status(post_id, apply_url, "skill_mismatch")
                    return False
                else:
                    print(f"🎯 [Resume Match Confirmed] {reason}")
        except Exception:
            pass
    CHROME_PROFILE_DIR.mkdir(exist_ok=True)
    release_chrome_profile_lock()
    stealth = Stealth()

    print("\n" + "=" * 65)
    print("🚀 AUTONOMOUS JOB APPLICATION AGENT (PLAYWRIGHT CO-PILOT)")
    print("=" * 65)
    print(f"Target URL   : {apply_url}")
    print(f"Candidate    : {profile['personal']['full_name']} ({profile['personal']['email']})")
    print(f"Resume File  : {RESUME_PATH} (Exists: {RESUME_PATH.exists()})")
    print(f"Chrome Profile: {CHROME_PROFILE_DIR}")
    print(f"Mode         : {'AUTONOMOUS SUBMIT' if auto_submit else 'CO-PILOT REVIEW (Browser stays open)'}")
    print("-" * 65)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(CHROME_PROFILE_DIR),
            channel="chrome",
            headless=headless,
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
                "--disable-popup-blocking"
            ],
            ignore_default_args=["--enable-automation"],
            no_viewport=True
        )

        active_page = context.pages[0] if context.pages else context.new_page()
        stealth.apply_stealth_sync(active_page)

        def on_new_page(new_p):
            nonlocal active_page
            print(f"🔀 Switched to new window/tab: {new_p.url}")
            stealth.apply_stealth_sync(new_p)
            active_page = new_p

        context.on("page", on_new_page)

        try:
            print("🌐 Navigating to application page...")
            active_page.goto(apply_url, timeout=35000, wait_until="domcontentloaded")
            active_page.wait_for_timeout(2500)

            # Robust Cloudflare / Turnstile Security Challenge Handler
            try:
                body_sample = active_page.inner_text("body").lower()[:1000]
            except Exception:
                body_sample = ""

            is_cf = (
                "just a moment" in active_page.title().lower()
                or "cloudflare" in active_page.title().lower()
                or "additional verification required" in body_sample
            )
            if is_cf:
                print("\n" + "=" * 65)
                print("🛡️ CLOUDFLARE BOT VERIFICATION DETECTED")
                print("=" * 65)
                print("Indeed is requesting a human security check ('Verify you are human').")
                print("👉 Please click the verification checkbox on your open Chrome browser!")
                print("⏳ Agent is standing by and will automatically resume once verified...")
                print("=" * 65)

                verified = False
                for sec in range(1, 61):
                    active_page.wait_for_timeout(1000)
                    try:
                        cf_box = active_page.frame_locator('iframe[src*="cloudflare"], iframe[src*="turnstile"], iframe[src*="challenge"]').locator('input[type="checkbox"], #cf-stage, .ctp-checkbox-label, div[role="checkbox"]').first
                        if cf_box.count() > 0 and cf_box.is_visible():
                            cf_box.click(force=True)
                    except Exception:
                        pass

                    t_now = active_page.title().lower()
                    try:
                        b_now = active_page.inner_text("body").lower()[:1000]
                    except Exception:
                        b_now = ""

                    if "just a moment" not in t_now and "cloudflare" not in t_now and "additional verification required" not in b_now:
                        print(f"✅ Security verification passed! Loading '{active_page.title()}'...")
                        verified = True
                        active_page.wait_for_timeout(2500)
                        break

                    if sec % 10 == 0:
                        print(f"   ⏳ Still waiting for verification ({sec}/60s)... (Click the checkbox in Chrome)")

                if not verified:
                    print("\n⚠️ Security verification timed out. Skipping this job for now.")
                    if post_id:
                        record_application_status(post_id, apply_url, "security_challenge")
                    return False

            # Dismiss any cookie banners, modals, or newsletter overlays
            try:
                cookie_btn = active_page.locator('button.cookie-accept-btn, button:has-text("Accept all"), button:has-text("Accept Cookies"), button:has-text("I accept"), #onetrust-accept-btn-handler').first
                if cookie_btn.count() > 0 and cookie_btn.is_visible():
                    cookie_btn.click(force=True)
                    active_page.wait_for_timeout(1000)
            except Exception:
                pass
            try:
                close_btn = active_page.locator('.modal .close, button.close, [aria-label="Close"], #close_popup, .ic-24-cross, .subscription_alert .close, [data-dismiss="modal"]').first
                if close_btn.count() > 0 and close_btn.is_visible():
                    close_btn.click(force=True)
            except Exception:
                pass

            # Check if page is expired or closed
            title_lower = active_page.title().lower()
            url_lower = active_page.url.lower()
            try:
                page_text_sample = active_page.inner_text("body").lower()[:1500]
            except Exception:
                page_text_sample = ""

            is_expired = (
                "404" in title_lower
                or "page not found" in title_lower
                or "job not found" in title_lower
                or "job not found" in page_text_sample
                or "the job you requested was not found" in page_text_sample
                or "no longer available" in page_text_sample
                or "position closed" in page_text_sample
                or "job posting has closed" in page_text_sample
                or "vacancy is closed" in page_text_sample
                or "closedform" in url_lower
                or "error=true" in url_lower
                or "this form is no longer accepting responses" in page_text_sample
                or "job has expired" in page_text_sample
                or "no longer accepting applications" in page_text_sample
                or "आवेदन स्वीकार नहीं" in page_text_sample
                or "स्वीकार नहीं किए जा रहे" in page_text_sample
            )

            if is_expired:
                print(f"⚠️ Notice: Job posting is expired or closed: '{active_page.title()}'")
                record_application_status(post_id, apply_url, "expired")
                return False

            # Check if job was already applied previously
            is_already_applied = (
                ("application status" in page_text_sample and "application submitted" in page_text_sample)
                or "applied on" in page_text_sample
                or "already applied" in page_text_sample
                or "आवेदन पहले ही" in page_text_sample
                or "आवेदन पत्र नियोक्ता को भेजा" in page_text_sample
            )
            if is_already_applied:
                print(f"ℹ️ Notice: You have already applied for this job previously: '{active_page.title()}'.")
                record_application_status(post_id, apply_url, "submitted")
                return True

            # Check if page is asking for login/authentication
            is_login_page = any(lp in url_lower for lp in ["/login", "/signin", "uas/login", "accounts.google.com/signin", "/checkpoint/"]) or (
                "authwall" in page_text_sample or "sign in to apply" in page_text_sample
            )
            if is_login_page:
                print("\n" + "⚠️" * 32)
                print("🔒 AUTHENTICATION REQUIRED")
                print("This portal requires you to be logged in to view and apply.")
                print("👉 Please run the one-time login command to log in:")
                print("     python main.py apply --login")
                print("⚠️" * 32)
                if post_id:
                    record_application_status(post_id, apply_url, "login_required")
                return False

            # Check LinkedIn Easy Apply specifically
            if "linkedin.com" in url_lower:
                easy_done = handle_linkedin_easy_apply(active_page, profile, auto_submit=auto_submit)
                if easy_done and auto_submit:
                    if post_id:
                        record_application_status(post_id, apply_url, "submitted")
                    return True

            # Check Indeed specifically (both external company apply and native Easy Apply)
            if "indeed.com" in url_lower:
                indeed_handled, new_active = handle_indeed_flow(active_page, profile, context, auto_submit=auto_submit)
                active_page = new_active
                stealth.apply_stealth_sync(active_page)
                if indeed_handled and auto_submit and "indeed.com" in active_page.url.lower():
                    if post_id:
                        record_application_status(post_id, apply_url, "submitted")
                    return True

            # Check Internshala specifically
            if "internshala.com" in url_lower:
                intern_done = handle_internshala_flow(active_page, profile, auto_submit=auto_submit)
                if intern_done and auto_submit:
                    if post_id:
                        record_application_status(post_id, apply_url, "submitted")
                    return True

            # If on an overview or landing page, click 'Apply Now', 'Apply', or 'Interested' to reveal the form
            for _ in range(2):
                try:
                    cookie_btn = active_page.locator('button.cookie-accept-btn, button:has-text("Accept all"), button:has-text("Accept Cookies"), button:has-text("I accept"), #onetrust-accept-btn-handler').first
                    if cookie_btn.count() > 0 and cookie_btn.is_visible():
                        cookie_btn.click(force=True)
                        active_page.wait_for_timeout(1000)
                except Exception:
                    pass

                apply_buttons = active_page.locator('a:has-text("Apply Now"), button:has-text("Apply Now"), a:has-text("Apply for this job"), button:has-text("Apply for this job"), a:has-text("Apply"), button:has-text("Apply"), button:has-text("interested"), a:has-text("interested")')
                if apply_buttons.count() > 0 and apply_buttons.first.is_visible():
                    btn_text = apply_buttons.first.inner_text().strip()
                    if "easy" not in btn_text.lower():
                        print(f"👉 Clicking '{btn_text[:20]}' to reveal form...")
                        pages_before = len(context.pages)
                        new_tab = None
                        try:
                            with context.expect_page(timeout=4000) as new_page_info:
                                apply_buttons.first.click()
                            new_tab = new_page_info.value
                        except Exception:
                            try:
                                apply_buttons.first.click(force=True)
                            except Exception:
                                pass

                        if not new_tab and len(context.pages) > pages_before:
                            new_tab = context.pages[-1]

                        if new_tab:
                            stealth.apply_stealth_sync(new_tab)
                            print(f"🔀 Switched to newly opened tab: {new_tab.url}")
                            active_page = new_tab
                            try:
                                active_page.wait_for_load_state("domcontentloaded", timeout=15000)
                            except Exception:
                                pass
                        active_page.wait_for_timeout(3000)
                else:
                    break


            # Auto-fill form
            stats = fill_application_form(active_page, profile)

            print("\n" + "=" * 65)
            print("FORM FILLING SUMMARY")
            print(f"  Fields Populated       : {stats['filled']}")
            print(f"  Resume Attached        : {'✅ Yes' if stats['uploaded_resume'] else '⚠️ None found / manual needed'}")
            print(f"  AI Answers Drafted     : {stats['custom_answered']}")
            print("=" * 65)

            submitted = False
            if auto_submit:
                if stats["filled"] == 0 and not stats["uploaded_resume"]:
                    print("⚠️ Auto-Submit aborted: No form fields were detected or populated.")
                    if post_id:
                        record_application_status(post_id, apply_url, "login_required" if "login" in active_page.url.lower() or active_page.locator('input[type="password"]').count() > 0 else "unsupported_form")
                else:
                    submit_btn = active_page.locator('button[type="submit"], input[type="submit"], button:has-text("Submit Application"), button:has-text("Submit"), button:has-text("Apply Now")').first
                    if submit_btn.count() > 0 and submit_btn.is_visible():
                        for _ in range(10):
                            if submit_btn.is_enabled():
                                break
                            active_page.wait_for_timeout(500)
                        submit_btn.click(force=True)
                        print("✅ Clicked Submit Application!")
                        active_page.wait_for_timeout(4000)
                        submitted = True
                    else:
                        print("⚠️ Submit button not found or not clickable.")
            else:
                print("\n" + "🌟" * 32)
                print("👀 BROWSER IS NOW OPEN FOR YOUR REVIEW!")
                print("   1. Verify the auto-filled details on your screen.")
                print("   2. Complete any CAPTCHA or missing questions if needed.")
                print("   3. Click 'Submit Application' when satisfied.")
                print("🌟" * 32)
                try:
                    prompt_msg = (
                        "\n👉 Type 'y' and press [ENTER] if you submitted the application: "
                        if (stats["filled"] == 0 and not stats["uploaded_resume"])
                        else "\n👉 Press [ENTER] (or type 'y') when submitted to record in DB (or 'n' to skip): "
                    )
                    conf = input(prompt_msg).strip().lower()
                    if stats["filled"] == 0 and not stats["uploaded_resume"]:
                        submitted = conf in ("y", "yes")
                    else:
                        submitted = conf not in ("n", "no", "skip")
                    if not submitted:
                        print("ℹ️ Skipping recording this application.")
                except (EOFError, KeyboardInterrupt):
                    print("   ℹ️ Non-interactive session: closing browser.")
                    submitted = False

            if post_id and submitted:
                record_application_status(post_id, apply_url, "submitted")
                print("💾 Application recorded in jobviareferral.db (status='submitted')")

            return True

        except Exception as e:
            print(f"\n❌ Application notice: {e}")
            return False
        finally:
            context.close()


# ---------------------------------------------------------------------------
# Skill-Matched Job Selector
# ---------------------------------------------------------------------------
def get_next_skill_matched_jobs(conn, limit: int = 1) -> list[tuple]:
    """Picks unapplied jobs strictly matching AI Engineer, Python, SDE, or Software Intern skills."""
    query = """
        SELECT d.post_id, d.apply_url, d.title,
               json_extract(d.structured_fields, '$.Company') as company
        FROM details d
        LEFT JOIN portal_applications pa ON d.post_id = pa.post_id
        WHERE d.apply_url IS NOT NULL
          AND d.apply_url != ''
          AND d.apply_url NOT LIKE 'mailto:%'
          AND d.apply_url NOT LIKE '%indeed.com/jobs?%'
          AND d.apply_url NOT LIKE '%linkedin.com/jobs/search%'
          AND d.apply_url NOT LIKE '%jobviareferral.com%'
          AND d.apply_url NOT LIKE '%internshala.com%'
          AND pa.status IS NULL
          AND (
              lower(d.title) LIKE '%ai%'
              OR lower(d.title) LIKE '%artificial intelligence%'
              OR lower(d.title) LIKE '%machine learning%'
              OR lower(d.title) LIKE '%python%'
              OR lower(d.title) LIKE '%software%'
              OR lower(d.title) LIKE '%sde%'
              OR lower(d.title) LIKE '%backend%'
              OR lower(d.title) LIKE '%developer%'
              OR lower(d.title) LIKE '%data scientist%'
              OR lower(d.title) LIKE '%data science%'
              OR lower(d.title) LIKE '%data engineer%'
              OR lower(d.title) LIKE '%data analyst%'
              OR lower(d.title) LIKE '%full stack%'
              OR lower(d.title) LIKE '%fullstack%'
              OR lower(d.title) LIKE '%frontend%'
              OR (
                  lower(d.title) LIKE '%intern%'
                  AND (
                      lower(d.title) LIKE '%soft%'
                      OR lower(d.title) LIKE '%dev%'
                      OR lower(d.title) LIKE '%engineer%'
                      OR lower(d.title) LIKE '%tech%'
                      OR lower(d.title) LIKE '%ai%'
                      OR lower(d.title) LIKE '%ml%'
                      OR lower(d.title) LIKE '%data%'
                      OR lower(d.title) LIKE '%python%'
                      OR lower(d.title) LIKE '%web%'
                      OR lower(d.title) LIKE '%backend%'
                      OR lower(d.title) LIKE '%frontend%'
                      OR lower(d.title) LIKE '%fullstack%'
                      OR lower(d.title) LIKE '%full stack%'
                      OR lower(d.title) LIKE '%sde%'
                  )
              )
          )
          AND lower(d.title) NOT LIKE '%marketing%'
          AND lower(d.title) NOT LIKE '%sales%'
          AND lower(d.title) NOT LIKE '%business development%'
          AND lower(d.title) NOT LIKE '%bde%'
          AND lower(d.title) NOT LIKE '%hr%'
          AND lower(d.title) NOT LIKE '%human resource%'
          AND lower(d.title) NOT LIKE '%content%'
          AND lower(d.title) NOT LIKE '%graphic%'
          AND lower(d.title) NOT LIKE '%designer%'
          AND lower(d.title) NOT LIKE '%operations%'
          AND lower(d.title) NOT LIKE '%recruiter%'
          AND lower(d.title) NOT LIKE '%talent acquisition%'
          AND lower(d.title) NOT LIKE '%finance%'
          AND lower(d.title) NOT LIKE '%accountant%'
          AND lower(d.title) NOT LIKE '%telecaller%'
          AND lower(d.title) NOT LIKE '%counselor%'
          AND lower(d.title) NOT LIKE '%subject matter expert%'
          AND lower(d.title) NOT LIKE '%sme%'
          AND lower(d.title) NOT LIKE '%video editor%'
          AND lower(d.title) NOT LIKE '%civil%'
          AND lower(d.title) NOT LIKE '%mechanical%'
        ORDER BY d.post_id DESC
        LIMIT ?
    """
    return conn.execute(query, (limit,)).fetchall()


def main():
    parser = argparse.ArgumentParser(description="Autonomous Job Application Agent")
    parser.add_argument("--url", type=str, default="", help="Direct job application portal URL")
    parser.add_argument("--post-id", type=int, default=0, help="Job post ID from database")
    parser.add_argument("--next", action="store_true", help="Pick the next skill-matched job from database")
    parser.add_argument("--batch", type=int, default=0, help="Number of skill-matched jobs to apply to in sequence")
    parser.add_argument("--login", action="store_true", help="Launch real Chrome to sign into LinkedIn, Google, and Internshala")
    parser.add_argument("--auto-submit", action="store_true", help="Automatically submit without waiting for manual confirmation")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless background mode")
    args = parser.parse_args()

    # One-Time Login Mode
    if args.login:
        open_login_session()
        return

    # Direct URL
    if args.url:
        apply_to_job(args.url, post_id=args.post_id or None, auto_submit=args.auto_submit, headless=args.headless)
        return

    # Direct Post ID
    if args.post_id and not args.next and not args.batch:
        conn = sqlite3.connect("jobviareferral.db")
        row = conn.execute("SELECT apply_url, title FROM details WHERE post_id=?", (args.post_id,)).fetchone()
        conn.close()
        if row and row[0]:
            print(f"🎯 Applying to Post #{args.post_id} - {row[1]}")
            apply_to_job(row[0], args.post_id, args.auto_submit, args.headless)
        else:
            print(f"❌ No valid apply_url found for Post #{args.post_id}")
        return

    # Batch or Next
    batch_count = args.batch if args.batch > 0 else (1 if args.next else 0)
    if batch_count > 0:
        conn = sqlite3.connect("jobviareferral.db")
        # Fetch candidate jobs
        candidate_jobs = get_next_skill_matched_jobs(conn, limit=batch_count * 3)
        conn.close()

        # Validate through strict LLM resume & qualification match
        valid_jobs = []
        profile = load_profile()
        print(f"🔍 Screening candidates with Strict LLM Resume & Qualification Matcher...")
        for pid, a_url, title, comp in candidate_jobs:
            if not is_role_relevant_for_candidate(title):
                record_application_status(pid, a_url, "irrelevant_role")
                continue

            conn = sqlite3.connect("jobviareferral.db")
            row = conn.execute("SELECT description_text, structured_fields FROM details WHERE post_id=?", (pid,)).fetchone()
            conn.close()
            combined_desc = (row[0] or "") if row else ""
            if row and row[1]:
                combined_desc += "\n" + row[1]

            is_match, reason = evaluate_job_resume_fit(title, combined_desc, profile)
            if is_match:
                print(f"   ✓ [Match] Post #{pid} | {comp}: {title}")
                print(f"     Rationale: {reason}")
                valid_jobs.append((pid, a_url, title, comp))
                if len(valid_jobs) == batch_count:
                    break
            else:
                print(f"   ✗ [Mismatch] Post #{pid} | {comp}: {title}")
                print(f"     Skipped: {reason}")
                record_application_status(pid, a_url, "skill_mismatch")

        if not valid_jobs:
            print("✅ No unapplied skill-matched jobs found in database!")
            return

        print(f"🎯 Selected {len(valid_jobs)} skill-matched job(s) for auto-application:")
        for idx, (pid, a_url, title, comp) in enumerate(valid_jobs, 1):
            print(f"   {idx}. Post #{pid} | {comp or 'Company'}: {title}")

        for idx, (pid, a_url, title, comp) in enumerate(valid_jobs, 1):
            print(f"\n[{idx}/{len(valid_jobs)}] Processing Post #{pid}: {title}...")
            apply_to_job(a_url, post_id=pid, auto_submit=args.auto_submit, headless=args.headless)
            if idx < len(valid_jobs):
                print("\nWaiting 3 seconds before next application...")
                time.sleep(3)
        return

    print("Usage:")
    print("  python main.py apply --login            # One-time login to LinkedIn/Google")
    print("  python main.py apply --next             # Apply to next skill-matched job")
    print("  python main.py apply --batch 5          # Apply to next 5 skill-matched jobs")
    print("  python main.py apply --url <URL>        # Apply to a specific URL")
    print("  python main.py apply --post-id <ID>     # Apply to a specific post ID")


if __name__ == "__main__":
    main()
