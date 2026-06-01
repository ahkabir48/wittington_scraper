import csv
import json
import os
import re
import sqlite3
import sys
import time
import threading
import queue
import concurrent.futures
from datetime import datetime, timezone
from typing import Dict, Optional
from dotenv import load_dotenv

import requests

# load .env file if present
load_dotenv()

# CONFIG
CSV_PATH_DEFAULT = "companies.csv"
DB_PATH_DEFAULT = "enriched_companies.db"
REPORT_CSV = "enriched_companies.csv"

# Tavily config (set TAVILY_API_KEY in env)
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
TAVILY_URL = "https://api.tavily.com/search"
TAVILY_CONTEXT_CHARS = 5000
TAVILY_TIMEOUT = 30  # increased from 15s

# Fireworks / Kimi config (set FIREWORKS_API_KEY in env)
FIREWORKS_API_KEY = os.environ.get("FIREWORKS_API_KEY", "")
FIREWORKS_URL = "https://api.fireworks.ai/inference/v1/chat/completions"
FIREWORKS_MODEL = os.environ.get("FIREWORKS_MODEL", "accounts/fireworks/models/gpt-oss-20b")
FIREWORKS_TIMEOUT = 120  # increased from 60s

# performance tuning
COMMIT_INTERVAL = 50        # commit every N writes
POLITE_DELAY = 0.05         # per-worker delay between requests
WORKERS = 6                 # number of concurrent worker threads

# Use a global requests.Session for connection reuse (keep-alive)
SESSION = requests.Session()

# Default name-filter tokens
DEFAULT_NON_STARTUP_KEYWORDS = {
    "ventures", "venture", "investments", "investment", "capital", "llc", "university", "hospital", "clinic", "medical", "pharma", "pharmaceutical",
    "bank", "trust", "insurance", "insurer", "credit", "credit union", "group", "partners", "partner", "holdings", "holding", "realty", "real estate", "estate", "government", "gov", "department", "ministry",
    "agency", "association", "nonprofit", "non-profit", "charity", "school", "institute", "franchise", "consulting", "consultancy", "services"
}
NON_STARTUP_KEYWORDS = set(DEFAULT_NON_STARTUP_KEYWORDS)


SYSTEM_PROMPT = """Act as a deal screening analyst for Wittington Ventures.
Wittington Ventures is a $500M multi-stage investment platform that backs transformative
companies across the consumer, commerce, healthcare, and climate sectors. Backed by
Wittington Investments — the holding company of the Weston group (including Loblaw,
Shoppers Drug Mart, and Choice Properties) — the firm leverages proprietary industry
access, philanthropic research insights, and long-term patient capital to support founders
throughout their growth journey.

THESIS:
- Stage: Multi-stage (seed through growth). Prioritise Series A/B.
- Geography: North America primary; global teams with clear North American GTM considered.
- Exclusions: Non-startups (large enterprises, non-profits), or clear sector/geography mismatches.

SECTORS — any credible overlap with one sector is sufficient; do not Pass on sector grounds alone:
- Consumer: End customer is an individual/household. Includes DTC brands, CPG, food & beverage,
  personal care, apparel, pet, home goods, consumer fintech, media, fitness, travel.
- Commerce: Infrastructure powering how goods are bought, sold, and moved. Includes e-commerce
  enablement, retail tech, marketplaces, supply chain & logistics, payments, procurement,
  fulfilment, grocery tech, adtech tied to retail.
- Healthcare: Improving health outcomes, access, or delivery efficiency. Includes digital health,
  telehealth, mental health, diagnostics, pharmacy tech, benefits platforms, EHR/ops software,
  life sciences, women's health, senior care, preventive care.
- Climate/Climatetech: Reducing emissions or enabling the low-carbon transition. Includes clean
  energy, EV charging, sustainable ag & food-tech, carbon markets, green buildings, circular
  economy, water tech, sustainable materials, climate risk analytics.

DECISION GUIDANCE:
- "Pass": Clearly outside sector, geography, or stage — or not a startup.
- "Potential prospect": Some sector/thesis fit but missing key signals (stage unclear, limited
  traction info, geography ambiguous). Default here when uncertain.
- "Strong prospect": Clear sector fit. Stage, geography, and traction signals (if available) align with thesis. Compelling product or founder
- Do NOT invent facts.

OUTPUT (return ONLY this JSON):
{
  "score": "<Strong prospect | Potential prospect | Pass>",
  "screening_notes": "
  - Decision: [score]
  - Sector: [which sector and why]
  - Reason: [1 sentence on product / stage / geography / traction]
  - Uncertainties: [missing info, if any]"
}

RULES: No preamble. No extra text outside the JSON. Be concise.
"""


def is_probably_non_startup(name: str) -> bool:
    n = (name or "").lower()
    for kw in NON_STARTUP_KEYWORDS:
        if " " in kw:
            if kw in n:
                return True
        else:
            if re.search(r"\b" + re.escape(kw) + r"\b", n):
                return True
    return False


# DB helpers
def init_db(db_path: str = DB_PATH_DEFAULT):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute(
        """
    CREATE TABLE IF NOT EXISTS companies (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE,
        normalized_name TEXT,
        enrichment_json TEXT,
        score TEXT,
        screening_notes TEXT,
        last_updated TIMESTAMP
    )
    """
    )
    conn.commit()
    conn.close()


# CSV helpers
def read_companies_from_csv(path: str):
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("company") or row.get("name") or list(row.values())[0]
            if name:
                yield name.strip()


# Tavily search: return combined snippets + top url
def tavily_search(company_name: str, max_results: int = 5, timeout: int = TAVILY_TIMEOUT) -> Dict:
    """
    Call Tavily and return a sanitized context:
    - prefer high-signal pages (about, product, pricing, features, blog post)
    - de-prioritise support/faq/login/privacy/contact pages
    - build a concise combined_text (trimmed and cleaned) for the LLM
    """
    if not TAVILY_API_KEY:
        return {"results": [], "top_url": "", "combined_text": "", "error": "TAVILY_API_KEY not set"}

    headers = {"Content-Type": "application/json"}
    payload = {
        "api_key": TAVILY_API_KEY,
        "query": f"{company_name} website OR homepage OR \"{company_name}\" domain",
        "search_depth": "basic",
        "include_answer": False,
        "include_raw_content": False,
        "max_results": max_results,
    }
    try:
        r = SESSION.post(TAVILY_URL, json=payload, headers=headers, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {"results": [], "top_url": "", "combined_text": "", "error": str(e)}

    def score_result(item: Dict) -> float:
        title = (item.get("title") or "").lower()
        url = (item.get("url") or "").lower()
        content = (item.get("content") or "").lower()
        score = 0.0
        # negative signals (support/docs/legal/contact, login, sitemap)
        if any(x in url or x in title or x in content for x in ("support", "/faq", "faq", "privacy", "terms", "cookie", "login", "signin", "contact", "404", "sitemap")):
            score -= 2.0
        # positive signals (about, product, features, pricing, team, solutions, platform, ai, api)
        if any(x in title or x in content for x in ("about", "product", "features", "pricing", "platform", "solutions", "api", "team", "fund", "series", "seed", "ai", "automation")):
            score += 2.0
        # prefer homepage/root domain
        if re.match(r"^https?://[^/]+/?$", item.get("url","")):
            score += 1.0
        # longer content is slightly better
        score += min(len(content) / 1000.0, 1.0)
        return score

    results = []
    for item in data.get("results", [])[:max_results]:
        results.append({
            "title": item.get("title","") or "",
            "url": item.get("url","") or "",
            "content": (item.get("content") or "").strip()
        })

    # rank results and pick top 3 high-signal pages
    ranked = sorted(results, key=lambda it: score_result(it), reverse=True)
    top_results = [r for r in ranked if r["content"]][:3]

    # if top_results looks empty (all low-signal), fallback to first result(s)
    if not top_results and results:
        top_results = results[:min(3, len(results))]

    # clean and join snippets: remove long repeated header boilerplate and markdown headings
    def clean_snip(s: str) -> str:
        s = re.sub(r"\n{2,}", "\n\n", s)
        s = re.sub(r"(?m)^\s*#.*$", "", s)            # remove headings
        s = re.sub(r"(?i)cookie[-_ ]?policy", "", s)
        s = re.sub(r"(?m)^\s*(back to top|skip to content).*$", "", s)
        s = s.strip()
        return s

    snippets = []
    for r in top_results:
        sn = clean_snip(r["content"])
        if sn:
            # keep short excerpt (first 600-1000 chars) to avoid overwhelming prompt
            snippets.append(sn[:900])

    combined_text = "\n\n".join(snippets)[:TAVILY_CONTEXT_CHARS]
    top_url = top_results[0]["url"] if top_results else (results[0]["url"] if results else "")

    return {"results": results, "top_url": top_url, "combined_text": combined_text}


# Fireworks LLM call (synchronous). Expects the model to return the exact JSON per SYSTEM_PROMPT.
def fireworks_score(company_name: str, tavily_ctx: Dict, fireworks_key: Optional[str] = None, model: str = FIREWORKS_MODEL, timeout: int = FIREWORKS_TIMEOUT) -> Optional[Dict]:
    key = fireworks_key or FIREWORKS_API_KEY
    if not key:
        return None

    tavily_text = tavily_ctx.get("combined_text", "")[:TAVILY_CONTEXT_CHARS]
    top_url = tavily_ctx.get("top_url", "") or ""
    user_content = (
        f"Company name: {company_name}\n\n"
        f"Tavily top_url: {top_url}\n\n"
        f"Tavily snippets (truncated):\n{tavily_text}\n\n"
        "Return ONLY the JSON object exactly as specified in the system prompt."
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.0,
        "max_tokens": 800,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "application/json"}

    try:
        r = SESSION.post(FIREWORKS_URL, json=payload, headers=headers, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None

    choice = data.get("choices", [{}])[0]
    message = choice.get("message", {}) if isinstance(choice, dict) else {}
    raw = (message.get("content") or message.get("reasoning_content") or "") or data.get("output") or data.get("text") or ""
    if not raw:
        return None

    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        parsed = json.loads(cleaned)
        return parsed
    except Exception:
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            try:
                parsed = json.loads(m.group())
                return parsed
            except Exception:
                return None
        return None


# Save or update row in DB (no commit; writer thread will commit in batches)
def save_company(conn: sqlite3.Connection, name: str, enrichment: Dict, score: str, screening_notes: str):
    c = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    c.execute(
        """
    INSERT INTO companies (name, normalized_name, enrichment_json, score, screening_notes, last_updated)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(name) DO UPDATE SET
      enrichment_json=excluded.enrichment_json,
      score=excluded.score,
      screening_notes=excluded.screening_notes,
      last_updated=excluded.last_updated
    """,
        (name, name.lower(), json.dumps(enrichment, ensure_ascii=False), score, screening_notes, now),
    )
    # writer will commit


# Export CSV only (report UI is standalone)
def export_reports(conn: sqlite3.Connection, csv_path: str = REPORT_CSV):
    c = conn.cursor()
    rows = c.execute(
        "SELECT name, score, screening_notes, enrichment_json, last_updated FROM companies "
        "ORDER BY CASE score WHEN 'Strong prospect' THEN 1 WHEN 'Potential prospect' THEN 2 ELSE 3 END, last_updated DESC"
    ).fetchall()

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "score", "screening_notes", "enrichment_json", "last_updated"])
        for r in rows:
            name = r[0] if len(r) > 0 and r[0] is not None else ""
            score = r[1] if len(r) > 1 and r[1] is not None else ""
            screening_notes = r[2] if len(r) > 2 and r[2] is not None else ""
            enrichment_json = r[3] if len(r) > 3 and r[3] is not None else ""
            last_updated = r[4] if len(r) > 4 and r[4] is not None else ""
            writer.writerow([name, score, screening_notes, enrichment_json, last_updated])


def one_line(s: str) -> str:
    return " ".join(s.replace("\n", " ").split())


# writer thread: consumes write_queue and writes to DB, commits in batches
def writer_worker(db_path: str, write_q: "queue.Queue"):
    conn = sqlite3.connect(db_path)
    writes_since_commit = 0
    while True:
        item = write_q.get()
        if item is None:
            break
        name, enrichment, score, screening_notes = item
        try:
            save_company(conn, name, enrichment, score, screening_notes)
            writes_since_commit += 1
            if writes_since_commit >= COMMIT_INTERVAL:
                conn.commit()
                writes_since_commit = 0
        except Exception as e:
            print(f"Writer error for {name}: {e}")
        finally:
            write_q.task_done()
    if writes_since_commit:
        conn.commit()
    conn.close()


# worker: perform tavily + llm for a single name and push result to queue
def process_name(name: str, write_q: "queue.Queue"):
    tavily_ctx = tavily_search(name)
    llm_res = fireworks_score(name, tavily_ctx)
    if llm_res and isinstance(llm_res, dict) and llm_res.get("score"):
        score = llm_res.get("score")
        screening_notes = llm_res.get("screening_notes", "")
        enrichment = {"tavily": tavily_ctx, "llm": llm_res}
    else:
        score = ""
        screening_notes = "- Decision: Unknown\n- Reason: LLM not configured or returned invalid output\n- Uncertainties: provide FIREWORKS_API_KEY or inspect tavily context"
        enrichment = {"tavily": tavily_ctx}
    write_q.put((name, enrichment, score, screening_notes))
    time.sleep(POLITE_DELAY)


# Main pipeline — supports prepass-only, batching, caching and clear-db via CLI
def run(csv_path: str = CSV_PATH_DEFAULT, db_path: str = DB_PATH_DEFAULT, force: bool = False, prepass_only: bool = False, batch_size: int = 1000, workers: int = WORKERS):
    # ensure DB/table exists
    init_db(db_path)

    total = 0
    tavily_calls_planned = 0
    write_q = queue.Queue()
    writer = threading.Thread(target=writer_worker, args=(db_path, write_q), daemon=True)
    writer.start()

    to_process = []  # list of names to send to workers
    # iterate CSV and decide actions (auto-pass, skip cached, or queue)
    for name in read_companies_from_csv(csv_path):
        total += 1
        if not name:
            continue

        # check existing
        conn_check = sqlite3.connect(db_path)
        c = conn_check.cursor()
        existing = c.execute("SELECT enrichment_json, score, screening_notes FROM companies WHERE name = ?", (name,)).fetchone()
        conn_check.close()

        # If fully cached (has enrichment_json) and not forcing, skip
        if existing and not force and existing[0]:
            print(f"Skipping (cached): {name} -> {existing[1]}")
            continue

        # Pre-pass name filter: auto-pass obvious non-startups
        if is_probably_non_startup(name):
            matched = [kw for kw in NON_STARTUP_KEYWORDS if ((" " in kw and kw in name.lower()) or re.search(r"\b" + re.escape(kw) + r"\b", name.lower()))]
            screening_notes = (
                "- Decision: Pass\n"
                "- Reason: company name contains non-startup keywords (" + ", ".join(sorted(set(matched))) + ")\n"
                "- Uncertainties: verify entity type if needed"
            )
            enrichment = {"reason": "name_filter", "matched_keywords": sorted(set(matched))}
            write_q.put((name, enrichment, "Pass", screening_notes))
            print(f"Auto-pass (name filter): {name} -> matched: {matched}")
            continue

        if prepass_only:
            print(f"Prepass-only: left for full run: {name}")
            continue

        # queue for processing (stop when we've gathered batch_size items)
        to_process.append(name)
        tavily_calls_planned += 1
        if tavily_calls_planned >= batch_size:
            print(f"Planned batch_size reached ({batch_size}) — will process these names this run.")
            break

    if to_process:
        print(f"Starting processing of {len(to_process)} names with {workers} workers...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as exe:
            futures = [exe.submit(process_name, n, write_q) for n in to_process]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    fut.result()
                except Exception as e:
                    print("Worker error:", e)

    # signal writer to finish and wait
    write_q.put(None)
    writer.join()

    # export reports from fresh connection
    conn2 = sqlite3.connect(db_path)
    export_reports(conn2)
    conn2.close()

    if not prepass_only:
        print(f"Done. Processed ~{total} names. CSV -> {REPORT_CSV}")
    else:
        print(f"Prepass-only run complete. Scanned ~{total} names. Database updated for auto-passes.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Enrich companies from CSV and run AI review (Wittington).")
    parser.add_argument("--csv", "-c", default=CSV_PATH_DEFAULT, help="Path to companies CSV (default companies.csv). Must have header 'company' or similar.")
    parser.add_argument("--db", "-d", default=DB_PATH_DEFAULT, help="SQLite DB path.")
    parser.add_argument("--force", "-f", action="store_true", help="Re-run enrichment even if cached.")
    parser.add_argument("--prepass-only", action="store_true", help="Run only the name-based pre-filter and write auto-passes; do not call Tavily/LLM.")
    parser.add_argument("--clear-db", action="store_true", help="Delete the DB file before running (use with caution).")
    parser.add_argument("--batch-size", type=int, default=1000, help="Maximum number of Tavily searches to perform in a single run (default 1000).")
    parser.add_argument("--workers", type=int, default=WORKERS, help="Number of concurrent worker threads.")
    args = parser.parse_args()

    if args.clear_db and os.path.exists(args.db):
        try:
            os.remove(args.db)
            print(f"Deleted DB: {args.db}")
        except Exception as e:
            print(f"Failed to delete DB {args.db}: {e}")
            sys.exit(1)

    run(csv_path=args.csv, db_path=args.db, force=args.force, prepass_only=args.prepass_only, batch_size=args.batch_size, workers=args.workers)