# =============================================================
#  NORDIC CHASE - Atleta registration puller  (GitHub Actions)
#
#  Reads ATLETA_TOKEN from the environment, writes riders.json, and REFUSES
#  to publish a list that looks broken (see the sanity guard at the end).
#
#  v3 fixes a real bug: riders are keyed on NAME, not participant.id.
#  participant.id is the ACCOUNT THAT PAID. When one person buys entries
#  for friends, every one of those registrations carries the buyer's id,
#  so keying on it silently merged different riders and dropped names
#  from the list. Name + nationality identifies the rider; the account
#  does not.
#
#  Also in v3:
#    - reports proxy purchases (one account, several riders)
#    - reports true same-rider-same-race duplicates for payment checking
#    - flags odd-looking names for manual review (never auto-corrects)
#
#  PRIVACY: requests ONLY name / nationality / event / ticket.
#  Never email, phone, DOB or address - they cannot leak into riders.json.
# =============================================================

import json, os, sys, time, re, unicodedata
from collections import defaultdict, Counter
import requests

# ------------------------------------------------------------------
# 1. CONFIG
# ------------------------------------------------------------------
API_URL      = "https://atleta.cc/api/graphql"
PROJECT_IDS  = ["rdsx"]          # from the dashboard URL
PER_PAGE     = 100
ACTIVE_ONLY  = True
SLEEP_BETWEEN= 0.3
OUT_PATH     = "riders.json"

# City -> short code. Anything unlisted falls back to its first 3 letters.
CITY_CODES = {
    "berlin": "BER", "copenhagen": "CPH", "oslo": "OSL",
    "amsterdam": "AMS", "stockholm": "STO",
}

# ------------------------------------------------------------------
# 2. TOKEN  (GitHub Actions secret: ATLETA_TOKEN)
# ------------------------------------------------------------------
TOKEN = os.environ.get("ATLETA_TOKEN", "").strip()
if not TOKEN:
    sys.exit("ATLETA_TOKEN is not set. Add it under Settings -> Secrets and "
             "variables -> Actions.")
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


class GqlError(RuntimeError):
    pass


def gql(query, variables=None):
    r = requests.post(API_URL, headers=HEADERS,
                      json={"query": query, "variables": variables or {}}, timeout=30)
    if r.status_code in (401, 403):
        raise GqlError(f"HTTP {r.status_code} - token missing, wrong, or lacking scope.")
    try:
        body = r.json()
    except Exception:
        raise GqlError(f"HTTP {r.status_code}, non-JSON body: {r.text[:400]}")
    if "errors" in body:
        # Atleta puts the useful detail in extensions, not message.
        raise GqlError("GraphQL error:\n" + json.dumps(body["errors"], indent=2)[:1500])
    return body["data"]


# ------------------------------------------------------------------
# 3. SCHEMA PROBE
# ------------------------------------------------------------------
TYPE_FIELDS_Q = """
query($n: String!) {
  __type(name: $n) { name fields { name args { name } } }
}
"""

def type_fields(name):
    t = gql(TYPE_FIELDS_Q, {"n": name}).get("__type")
    return {f["name"]: [a["name"] for a in (f.get("args") or [])]
            for f in t["fields"]} if t else {}

def pick(avail, cands):
    return next((c for c in cands if c in avail), None)

print("Probing schema ...")
reg_fields     = type_fields("Registration")
project_fields = type_fields("Project")
event_fields   = type_fields("Event")
ticket_fields  = type_fields("Ticket")
if not reg_fields:
    raise SystemExit("Could not introspect Registration. Check token scope.")

reg_args = project_fields.get("registrations", [])
F_FIRST  = pick(reg_fields, ["first_name", "firstName"])
F_LAST   = pick(reg_fields, ["last_name", "lastName"])
F_FULL   = pick(reg_fields, ["full_name", "fullName"])
F_NAT    = pick(reg_fields, ["nationality"])
F_NATIOC = pick(reg_fields, ["nationality_ioc", "nationalityIoc"])
F_CREATED= pick(reg_fields, ["created_at", "createdAt"])
F_REGNO  = pick(reg_fields, ["registration_number", "registrationNumber"])
EV_LABEL = pick(event_fields, ["title", "name", "label"])
TK_LABEL = pick(ticket_fields, ["title", "name", "label"])
ARG_PAGE   = pick(reg_args, ["page"])
ARG_PER    = pick(reg_args, ["per_page", "perPage", "first", "limit"])
ARG_ACTIVE = pick(reg_args, ["active"])

sel = [f for f in (F_FIRST, F_LAST, F_FULL, F_NAT, F_NATIOC, F_CREATED, F_REGNO) if f]
if "event" in reg_fields:
    sel.append("event { id" + (f" {EV_LABEL}" if EV_LABEL else "") + " }")
if "ticket" in reg_fields:
    sel.append("ticket { id" + (f" {TK_LABEL}" if TK_LABEL else "") + " }")
if "participant" in reg_fields:
    sel.append("participant { id }")

ca = []
if ARG_PER:  ca.append(f"{ARG_PER}: $per")
if ARG_PAGE: ca.append(f"{ARG_PAGE}: $page")
if ARG_ACTIVE and ACTIVE_ONLY: ca.append(f"{ARG_ACTIVE}: true")
call = f"registrations({', '.join(ca)})" if ca else "registrations"

REG_QUERY = f"""
query($id: ID!, $page: Int, $per: Int) {{
  project(id: $id) {{
    {call} {{
      current_page last_page total
      data {{
{chr(10).join('        ' + s for s in sel)}
      }}
    }}
  }}
}}
"""

# ------------------------------------------------------------------
# 4. FETCH
# ------------------------------------------------------------------
def fetch_project(pid):
    rows, page = [], 1
    while True:
        blk = gql(REG_QUERY, {"id": pid, "page": page, "per": PER_PAGE})["project"]["registrations"]
        rows.extend(blk["data"])
        last = blk.get("last_page") or 1
        print(f"    page {blk.get('current_page', page)}/{last}  "
              f"({len(blk['data'])} rows of {blk.get('total')})")
        if page >= last:
            break
        page += 1
        time.sleep(SLEEP_BETWEEN)
    return rows

raw = []
for pid in PROJECT_IDS:
    if " " in pid or pid.startswith("PASTE_"):
        raise SystemExit(f"{pid!r} looks like a project NAME. Use the ID from the dashboard URL.")
    print(f"\nProject {pid}")
    raw.extend(fetch_project(pid))
print(f"\n{len(raw)} registration rows fetched.")

# ------------------------------------------------------------------
# 5. LABELS
#    Atleta stores e.g.
#      event  : "Nordic Chase 2027 - Copenhagen to Oslo | Chase 2"
#      ticket : "Gravel Ticket - Copenhagen to Oslo"
#    -> chase 2, CPH-OSL, Gravel
# ------------------------------------------------------------------
def code_for(city):
    c = city.strip().lower()
    return CITY_CODES.get(c, re.sub(r"[^A-Z]", "", c.upper())[:3] or "???")

def parse_race(row):
    ev = ((row.get("event") or {}).get(EV_LABEL) or "") if EV_LABEL else ""
    tk = ((row.get("ticket") or {}).get(TK_LABEL) or "") if TK_LABEL else ""
    blob = f"{ev} {tk}"

    # Single digit only - otherwise "Nordic Chase 2027" reads as chase 2027.
    m = re.search(r"\b(?:chase\s*|c)([1-9])\b", blob, re.I)
    chase = int(m.group(1)) if m else None

    m = re.search(r"([A-Za-zÀ-ÿ]+)\s+to\s+([A-Za-zÀ-ÿ]+)", blob)
    if m:
        a, b = m.group(1), m.group(2)
        route, rcode = f"{a} to {b}", f"{code_for(a)}-{code_for(b)}"
    else:
        route, rcode = ev.strip() or "Unknown", "???"

    m = re.search(r"\b(road|gravel)\b", blob, re.I)
    disc = m.group(1).capitalize() if m else None

    label = " ".join(p for p in [
        f"Chase {chase}" if chase else None,
        rcode if rcode != "???" else route,
        disc,
    ] if p)
    return {"chase": chase, "code": rcode, "route": route,
            "discipline": disc, "label": label}

# ------------------------------------------------------------------
# 6. GROUP PER RIDER
# ------------------------------------------------------------------
def norm(s):
    if not s: return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", s.lower())).strip()

def tidy(s):
    if not s: return ""
    s = s.strip()
    if s.isupper() or s.islower():
        return ("-".join(p.capitalize() for p in s.split("-")) if "-" in s
                else " ".join(p.capitalize() for p in s.split()))
    return s

people, dupes = {}, []
accounts = defaultdict(set)          # participant id -> rider names bought under it

def split_full(n):
    """Fallback when Atleta gave us only a full name."""
    parts = n.split()
    if len(parts) < 2:
        return (parts[0] if parts else "Unknown"), ""
    for i, p in enumerate(parts[1:], 1):
        if p[:1].islower() or p in ("De","Van","Der","Den","Du","Le","La",
                                    "Von","Di","Da","Del","Dos","Af"):
            return " ".join(parts[:i]), " ".join(parts[i:])
    return parts[0], " ".join(parts[1:])

for row in raw:
    first = tidy(row.get(F_FIRST) or "")
    last  = tidy(row.get(F_LAST) or "")
    if not (first or last):
        first, last = split_full(tidy(row.get(F_FULL) or "") or "Unknown")
    name = f"{first} {last}".strip() or "Unknown"
    nat = (row.get(F_NAT) or "").upper() or None
    ioc = (row.get(F_NATIOC) or "").upper() or None
    acct = (row.get("participant") or {}).get("id")

    # THE RIDER is the key. Not the account that paid.
    key = f"{norm(name)}|{nat or ''}"
    if acct:
        accounts[acct].add(name)

    p = people.setdefault(key, {
        "name": name, "first": first, "last": last,
        "nationality": nat, "nationality_ioc": ioc,
        "races": [], "_accounts": set(), "_regnos": [],
    })
    if acct:
        p["_accounts"].add(acct)
    p["_regnos"].append(row.get(F_REGNO))

    race = parse_race(row)
    if any(r["label"] == race["label"] for r in p["races"]):
        dupes.append((name, race["label"], row.get(F_REGNO), row.get(F_CREATED)))
    else:
        p["races"].append(race)

    if not p["nationality"] and nat: p["nationality"] = nat
    if not p["nationality_ioc"] and ioc: p["nationality_ioc"] = ioc

riders = list(people.values())
for p in riders:
    p["races"].sort(key=lambda r: (r["chase"] or 99, r["discipline"] or ""))
    p["race_count"] = len(p["races"])
riders.sort(key=lambda p: (-p["race_count"], norm(p["name"])))

# Accounts that paid for more than one rider = proxy purchases.
proxies = {a: sorted(n) for a, n in accounts.items() if len(n) > 1}

# ------------------------------------------------------------------
# 7. REPORT
# ------------------------------------------------------------------
print(f"\n{len(riders)} unique riders from {len(raw)} registrations")

if proxies:
    print(f"\n{len(proxies)} account(s) bought entries for more than one rider:")
    for a, names in proxies.items():
        print(f"     account {a}: {', '.join(names)}")
    print("     -> these riders share ONE email on the account. Check each has")
    print("        their own contact details, or they will never get the manual.")

if dupes:
    print(f"\n!! {len(dupes)} same-rider-same-race registrations.")
    print("   Either a double payment, or two different people with the same")
    print("   name and nationality. Check in Atleta:")
    for n, lbl, regno, created in dupes:
        print(f"     {n:<28} {lbl:<26} reg#{regno}  {created}")
else:
    print("\nNo same-rider-same-race registrations.")

odd = [p["name"] for p in riders
       if re.search(r"[a-z][A-Z]", p["name"][1:])
       or re.search(r"\b(\w+)\b.*\b\1\b", p["name"], re.I)]
if odd:
    print(f"\n{len(odd)} name(s) worth eyeballing before this goes public:")
    for n in odd:
        print(f"     {n}")

per = Counter()
for p in riders:
    for r in p["races"]:
        per[r["label"]] += 1
print("\nPer edition")
for lbl, n in sorted(per.items()):
    print(f"  {n:4d}  {lbl}")

bychase = defaultdict(int)
for p in riders:
    for r in p["races"]:
        bychase[(r["chase"], r["code"])] += 1
print("\nPer chase")
for (c, code), n in sorted(bychase.items(), key=lambda x: (x[0][0] or 99)):
    print(f"  Chase {c}  {code:<9} {n}")

print("\nRaces per rider:", dict(sorted(Counter(p['race_count'] for p in riders).items())))
acc = sum(1 for p in riders if p["_accounts"])
print(f"Riders linked to an Atleta account: {acc}  |  without one: {len(riders)-acc}")

nat = Counter(p["nationality"] or "??" for p in riders)
print(f"\nNationalities: {len(nat)}")
print("  " + "  ".join(f"{c}:{n}" for c, n in nat.most_common(15)))

multi = [p for p in riders if p["race_count"] > 1]
print(f"\nMulti-chase riders ({len(multi)}):")
for p in multi:
    print(f"  {p['name']:<28} {p['nationality']}  "
          f"{' + '.join(r['label'] for r in p['races'])}")

# ------------------------------------------------------------------
# 8. WRITE
# ------------------------------------------------------------------
payload = {
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "total_riders": len(riders),
    "total_registrations": len(raw),
    "duplicate_registrations": len(dupes),
    "per_edition": dict(sorted(per.items())),
    "proxy_purchases": len(proxies),
    "riders": [{k: v for k, v in p.items() if not k.startswith("_")} for p in riders],
}
# ------------------------------------------------------------------
# 9. SANITY GUARD
#    A transient API fault that returns few or no rows must NOT be allowed
#    to wipe the public start list. Compare against the file we published
#    last time and refuse to overwrite it with something implausible.
# ------------------------------------------------------------------
FLOOR = float(os.environ.get("MIN_RETAIN_RATIO", "0.8"))

if not riders:
    sys.exit("REFUSING TO WRITE: zero riders returned.")

if os.path.exists(OUT_PATH):
    try:
        prev = json.load(open(OUT_PATH, encoding="utf-8"))
        was = int(prev.get("total_riders") or 0)
    except Exception:
        was = 0
    if was and len(riders) < was * FLOOR:
        sys.exit(f"REFUSING TO WRITE: rider count fell from {was} to "
                 f"{len(riders)} ({len(riders)/was:.0%} of previous, floor "
                 f"{FLOOR:.0%}). Keeping the published file. If this drop is "
                 f"real, re-run with MIN_RETAIN_RATIO lowered.")
    if was:
        print(f"\nSanity guard: {was} -> {len(riders)} riders, ok.")

with open(OUT_PATH, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=False, indent=1)
print(f"\nWrote {OUT_PATH}: {len(riders)} riders, {len(raw)} registrations.")