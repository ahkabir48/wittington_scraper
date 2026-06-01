import csv, sqlite3, json, sys, re
CSV = "companies.csv"
DB = "enriched_companies.db"

def iter_csv_names(path):
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            name = (row.get("company") or row.get("name") or list(row.values())[0] or "").strip()
            if name:
                yield name

def extract_reason_from_notes(notes: str) -> str:
    if not notes:
        return ""
    m = re.search(r'(?mi)^[\-\*\s]*Reason:\s*(.+)$', notes, flags=re.M)
    if m:
        return m.group(1).strip()
    m2 = re.search(r'(?i)Reason:\s*([^\n\r]+)', notes)
    if m2:
        return m2.group(1).strip()
    return ""

conn = sqlite3.connect(DB)
c = conn.cursor()

total = 0
not_processed = []
llm_error = []
processed_with_llm = 0
processed_total = 0
processed_prepass_pass = []
processed_other = []

for name in iter_csv_names(CSV):
    total += 1
    row = c.execute("SELECT enrichment_json, screening_notes, score FROM companies WHERE name = ?", (name,)).fetchone()
    if not row or not row[0]:
        not_processed.append(name)
        continue

    processed_total += 1
    enf = row[0] or ""
    notes = (row[1] or "").strip()
    score = (row[2] or "").strip()

    # detect prepass 'Pass' entries (either score == 'Pass' or enrichment.reason == 'name_filter')
    is_prepass = False
    try:
        parsed = json.loads(enf)
        if isinstance(parsed, dict):
            if parsed.get("reason") == "name_filter":
                is_prepass = True
    except Exception:
        # simple string check fallback
        if '"reason": "name_filter"' in enf or "'reason': 'name_filter'" in enf:
            is_prepass = True

    if score == "Pass" and is_prepass:
        processed_prepass_pass.append(name)
        # still count as processed but not "with llm"
        continue
    elif score == "Pass" and not is_prepass:
        # Could be legitimate Pass from LLM; still treat as processed_other
        processed_other.append(name)
        # continue to check llm presence below

    # detect whether enrichment contains llm field
    has_llm = False
    try:
        parsed = parsed if 'parsed' in locals() and isinstance(parsed, dict) else json.loads(enf)
        has_llm = isinstance(parsed, dict) and "llm" in parsed
    except Exception:
        has_llm = '"llm"' in enf

    if has_llm:
        processed_with_llm += 1
    else:
        processed_other.append(name)

    # Extract Reason: bullet and check for LLM-related text (case-insensitive, substring match)
    reason_text = extract_reason_from_notes(notes).lower()
    if reason_text:
        if "llm not configured" in reason_text or "returned invalid" in reason_text or ("llm" in reason_text and "invalid" in reason_text):
            llm_error.append(name)
    else:
        notes_lc = notes.lower()
        if "llm not configured" in notes_lc or "returned invalid" in notes_lc or ("decision: unknown" in notes_lc and "llm" in notes_lc):
            llm_error.append(name)

conn.close()

print(f"Total in CSV: {total}")
print(f"Processed (any enrichment_json): {processed_total}")
print(f"Processed with llm field: {processed_with_llm}")
print(f"Not processed (no enrichment_json): {len(not_processed)}")
print(f"Processed but LLM error / missing LLM output (reason match): {len(llm_error)}")
print(f"Processed prepass 'Pass' entries: {len(processed_prepass_pass)}")
print(f"Processed other (no llm / non-prepass passes / misc): {len(processed_other)}")
print()

def show_sample(lst, label):
    print(label + " (up to 20 samples):")
    for x in lst[:20]:
        print("  ", x)
    if len(lst) > 20:
        print("  ... +", len(lst)-20, "more")
    print()

show_sample(not_processed, "Not processed")
show_sample(llm_error, "Processed but LLM error (reason match)")
show_sample(processed_prepass_pass, "Processed prepass 'Pass' (auto name-filter)")
show_sample(processed_other, "Processed other (no llm / non-prepass)")