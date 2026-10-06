#!/usr/bin/env python3
"""EVE Structure Intelligence

Part 1: bash calculator (shield + armor phases)
Part 2: intel layer (zKillboard + ESI) -> effort score, loot range, active hours

Standard library only. No pip installs needed.
"""

import json
import statistics
import time
import urllib.error
import urllib.request
from collections import Counter

# ================================================================
# CONFIG
# ================================================================

# zKillboard and ESI both ask for a real contact in the User-Agent.
USER_AGENT = "EveStructureIntel/0.2 (contact: YOUR_EMAIL_OR_CHARACTER_NAME)"
ESI = "https://esi.evetech.net/latest"
ZKB = "https://zkillboard.com/api"
ZKB_DELAY = 1.5          # seconds between zKillboard calls (be polite)
MAX_KILLMAILS_TO_FETCH = 30   # ESI killmail lookups for the activity profile

# ================================================================
# DATA  (move to JSON later if you like)
# ================================================================

# ship: (low_dps, high_dps)
SHIPS = {
    "Zirnitra": (10_000, 60_000),
    "Leshak": (700, 2_000),
    "Ikitursa": (300, 1_200),
    "Kikimora": (300, 800),
    "Oracle": (400, 2_000),
    "Jackdaw": (250, 700),
}

# VERIFY these against current game data; they change with patches.
# To add a structure, copy the Astrahus block and change the numbers.
STRUCTURES = {
    "Astrahus": {
        "type_id": 35832,   # used to find similar structure kills on zKillboard
        "shield_hp": 3_600_000,
        "shield_resistance": 0.20,
        "armor_hp": 1_800_000,
        "armor_resistance": 0.20,
        "armor_damage_cap": 5_000,
        "repair_threshold": 500,
    },
}

# ================================================================
# INPUT HELPERS
# ================================================================


def ask_int(prompt, lo=None, hi=None):
    """Keep asking until the user enters a valid integer in range."""
    while True:
        raw = input(prompt).strip()
        try:
            value = int(raw)
        except ValueError:
            print("Please enter a whole number.")
            continue
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            print(f"Please enter a number between {lo} and {hi}.")
            continue
        return value


def ask_yes_no(prompt):
    return input(prompt).strip().lower().startswith("y")


# ================================================================
# FORMATTING
# ================================================================


def format_time(seconds):
    if seconds is None:
        return "never (no damage applied)"
    total_minutes = int(seconds // 60)
    return f"{total_minutes // 60} hours and {total_minutes % 60} minutes"


def fmt_isk(value):
    value = float(value)
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= limit:
            return f"{value / limit:.2f}{suffix}"
    return f"{value:.0f}"


# ================================================================
# PART 1: BASH CALCULATOR
# ================================================================


def build_fleet():
    fleet = {}
    names = list(SHIPS)
    print("BUILD YOUR FLEET")
    print("-----------------")
    while True:
        print()
        for i, name in enumerate(names, start=1):
            print(f"{i}. {name}")
        print(f"{len(names) + 1}. Done")

        choice = ask_int("Choose a ship: ", 1, len(names) + 1)
        if choice == len(names) + 1:
            if not fleet:
                print("Add at least one ship first.")
                continue
            return fleet
        ship = names[choice - 1]
        qty = ask_int(f"How many {ship}s? ", 1)
        fleet[ship] = fleet.get(ship, 0) + qty


def fleet_dps(fleet):
    low = sum(SHIPS[s][0] * q for s, q in fleet.items())
    high = sum(SHIPS[s][1] * q for s, q in fleet.items())
    return low, high


def calc_phase(hp, resistance, low_dps, high_dps, damage_cap=None):
    """Time to chew through one phase.

    Resistance is applied ONCE: applied_dps = raw_dps * (1 - resistance),
    then time = hp / applied_dps. (EHP = hp / (1 - res) gives the same answer;
    just don't do both.) An optional cap limits applied DPS per second.
    """
    def applied(dps):
        a = dps * (1 - resistance)
        return min(a, damage_cap) if damage_cap is not None else a

    applied_low, applied_high = applied(low_dps), applied(high_dps)
    return {
        "ehp": hp / (1 - resistance),
        "applied_low": applied_low,
        "applied_high": applied_high,
        "fastest": hp / applied_high if applied_high > 0 else None,
        "slowest": hp / applied_low if applied_low > 0 else None,
    }


def print_bash_report(name, s, fleet, low_dps, high_dps):
    shield = calc_phase(s["shield_hp"], s["shield_resistance"], low_dps, high_dps)
    armor = calc_phase(
        s["armor_hp"], s["armor_resistance"], low_dps, high_dps, s["armor_damage_cap"]
    )
    paused = shield["applied_low"] >= s["repair_threshold"]

    print()
    print("================================")
    print(" EVICTION FLEET")
    print("================================")
    for ship, qty in fleet.items():
        print(f"{ship:<12} {qty:>4}")
    print(f"\nLOW DPS:   {low_dps:,.0f}")
    print(f"HIGH DPS:  {high_dps:,.0f}")

    print()
    print("================================")
    print(f" {name.upper()} BASH ESTIMATE")
    print("================================")

    print(
        f"SHIELD: {format_time(shield['fastest'])} - "
        f"{format_time(shield['slowest'])} (fastest to slowest)"
    )
    print(
        f"  {s['shield_hp']:,.0f} HP, {s['shield_resistance']:.0%} resist, "
        f"{shield['applied_low']:,.0f}-{shield['applied_high']:,.0f} applied DPS"
    )
    print(
        f"  Repair timer: {'PAUSED' if paused else 'NOT PAUSED'} "
        f"(needs {s['repair_threshold']:,.0f} applied DPS, low case is "
        f"{shield['applied_low']:,.0f})"
    )

    print(
        f"\nARMOR:  {format_time(armor['fastest'])} - "
        f"{format_time(armor['slowest'])} (fastest to slowest)"
    )
    print(
        f"  {s['armor_hp']:,.0f} HP, {s['armor_resistance']:.0%} resist, "
        f"{armor['applied_low']:,.0f}-{armor['applied_high']:,.0f} applied DPS "
        f"(cap {s['armor_damage_cap']:,.0f})"
    )

    return shield, armor


# ================================================================
# PART 2: INTEL LAYER
# ================================================================


def http_json(url, payload=None):
    """GET (or POST if payload given) and return parsed JSON, or None on failure."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (420, 429, 502, 503, 504) and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            print(f"  [warn] HTTP {e.code} from {url}")
            return None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print(f"  [warn] request failed ({e}) for {url}")
            return None
    return None


def resolve_corp(name):
    """Corp name -> (id, name) via ESI public /universe/ids/."""
    result = http_json(f"{ESI}/universe/ids/", [name])
    corps = (result or {}).get("corporations") or []
    return (corps[0]["id"], corps[0]["name"]) if corps else (None, None)


def zkb_get(path):
    time.sleep(ZKB_DELAY)
    return http_json(f"{ZKB}/{path}/") or []


def fetch_killmail(entry):
    return http_json(f"{ESI}/killmails/{entry['killmail_id']}/{entry['zkb']['hash']}/")


def type_names(type_ids):
    """Resolve type IDs to names via ESI /universe/names/."""
    ids = sorted(set(type_ids))
    if not ids:
        return {}
    result = http_json(f"{ESI}/universe/names/", ids) or []
    return {r["id"]: r["name"] for r in result}


def busiest_window(hour_counts, width=4):
    counts = [hour_counts.get(h, 0) for h in range(24)]
    start = max(range(24), key=lambda h: sum(counts[(h + i) % 24] for i in range(width)))
    return start, (start + width) % 24


def score_effort(losses, kills):
    """Heuristic 0-100 'how hard is this corp to bash' score. Tune the tiers."""
    values = [k["zkb"].get("totalValue", 0) for k in losses]
    median_loss = statistics.median(values) if values else 0

    # Up to 40 points for recent kill activity (50+ kills = max)
    activity_pts = min(len(kills), 50) / 50 * 40

    # Up to 60 points for how expensive the ships they lose are
    tiers = [(20e6, 0), (100e6, 15), (500e6, 30), (2e9, 45), (float("inf"), 60)]
    value_pts = next(pts for limit, pts in tiers if median_loss < limit)

    score = round(activity_pts + value_pts)
    label = (
        "TRIVIAL" if score < 20 else
        "EASY" if score < 45 else
        "MODERATE" if score < 70 else
        "HARD"
    )
    return score, label, median_loss


def loot_range(structure_type_id):
    """Dropped-value stats from recent kills of the same structure type."""
    kills = zkb_get(f"losses/shipID/{structure_type_id}")[:50]
    dropped = [k["zkb"].get("droppedValue", 0) for k in kills]
    if not dropped:
        return None
    return {
        "samples": len(dropped),
        "min": min(dropped),
        "median": statistics.median(dropped),
        "max": max(dropped),
    }


def gather_intel(structure_name, structure, owner_name):
    print("\nLooking up owner...")
    corp_id, corp_name = resolve_corp(owner_name)
    if not corp_id:
        print("  Couldn't resolve that corporation name. Check spelling/network.")
        return

    print(f"  Found: {corp_name} (id {corp_id})")
    print("Fetching their recent losses and kills from zKillboard...")
    losses = zkb_get(f"losses/corporationID/{corp_id}")[:50]
    kills = zkb_get(f"kills/corporationID/{corp_id}")[:50]

    if not losses and not kills:
        print("  No killboard activity found. Could be a new/quiet corp (or an API issue).")
        effort = None
    else:
        effort = score_effort(losses, kills)

    # Activity profile + most-lost ships (needs ESI killmail lookups)
    hours, lost_ships = Counter(), Counter()
    sample = (losses + kills)[:MAX_KILLMAILS_TO_FETCH]
    loss_ids = {k["killmail_id"] for k in losses}
    for entry in sample:
        km = fetch_killmail(entry)
        if not km:
            continue
        hours[int(km["killmail_time"][11:13])] += 1
        if entry["killmail_id"] in loss_ids:
            lost_ships[km["victim"]["ship_type_id"]] += 1
    names = type_names(lost_ships.keys())

    print("Estimating loot from similar structure kills...")
    loot = loot_range(structure["type_id"])

    # ---- report ----
    print()
    print("================================")
    print(" STRUCTURE INTELLIGENCE")
    print("================================")
    print(f"Structure: {structure_name}")
    print(f"Owner:     {corp_name}")

    if effort:
        score, label, median_loss = effort
        print(f"\nEFFORT:    {label} ({score}/100)")
        print(f"  {len(kills)} recent kills, {len(losses)} recent losses, "
              f"median loss {fmt_isk(median_loss)} ISK")
    if lost_ships:
        top = ", ".join(
            f"{names.get(t, t)} x{n}" for t, n in lost_ships.most_common(5)
        )
        print(f"  Most lost: {top}")

    if hours:
        start, end = busiest_window(hours)
        print(f"\nACTIVE:    ~{start:02d}:00-{end:02d}:00 EVE time "
              f"(from {sum(hours.values())} killmails)")

    if loot:
        print(f"\nLOOT:      {fmt_isk(loot['min'])} - {fmt_isk(loot['max'])} ISK "
              f"(median {fmt_isk(loot['median'])})")
        print(f"  Based on dropped value of {loot['samples']} recent "
              f"{structure_name} kills. Includes everything that dropped, "
              f"and says nothing about THIS structure's fit.")
    else:
        print("\nLOOT:      no comparable kills found")

    print("\nNote: heuristics only. A quiet killboard doesn't prove an empty structure.")


# ================================================================
# MAIN
# ================================================================


def main():
    print("================================")
    print(" EVE STRUCTURE INTELLIGENCE")
    print("================================\n")

    fleet = build_fleet()
    low_dps, high_dps = fleet_dps(fleet)

    print("\nSTRUCTURE")
    print("---------")
    names = list(STRUCTURES)
    for i, n in enumerate(names, start=1):
        print(f"{i}. {n}")
    name = names[ask_int("Choose a structure: ", 1, len(names)) - 1]
    structure = STRUCTURES[name]

    print_bash_report(name, structure, fleet, low_dps, high_dps)

    print()
    if ask_yes_no("Run structure intel on the owner? (y/n) "):
        owner = input("Owner corporation name: ").strip()
        if owner:
            gather_intel(name, structure, owner)


if __name__ == "__main__":
    main()
