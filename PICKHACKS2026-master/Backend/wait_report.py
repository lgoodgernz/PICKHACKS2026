"""
wait_report.py
--------------
Reads wait_stats.json written by traffic_simulation.py and prints a live report.

    python wait_report.py               # print once and exit
    python wait_report.py --watch       # refresh every 10 seconds
    python wait_report.py --watch --interval 5
"""

import argparse
import json
import os
import time

WAIT_STATS_PATH = os.path.join(os.path.dirname(__file__), "wait_stats.json")

RESET = "\033[0m"
BOLD  = "\033[1m"
DIM   = "\033[2m"

def _wait_color(avg_s: float) -> str:
    if avg_s < 10:   return "\033[92m"   # green
    if avg_s < 18:   return "\033[93m"   # yellow
    return               "\033[91m"      # red


def load_stats():
    if not os.path.exists(WAIT_STATS_PATH):
        return None
    try:
        with open(WAIT_STATS_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def print_report():
    data = load_stats()

    if data is None:
        print(f"\n  Waiting for {WAIT_STATS_PATH} ...")
        print("  (Make sure app.py is running and traffic has started)\n")
        return

    sim_time   = data.get("sim_time", 0)
    mode       = data.get("signal_mode", "?").upper()
    rl_updates = data.get("rl_updates", 0)
    splits     = {int(k): float(v) for k, v in data.get("node_splits", {}).items()}
    cycles     = {int(k): float(v) for k, v in data.get("node_cycles", {}).items()}
    raw_stats  = data.get("wait_stats", {})
    stats      = {int(k): v for k, v in raw_stats.items()}

    print(f"\n{BOLD}{'═'*80}{RESET}")
    print(f"{BOLD}  🚦 Signal Wait-Time Report   sim time: {sim_time:.0f}s   mode: {mode}{RESET}")
    print(f"{BOLD}{'═'*80}{RESET}")

    if not stats:
        print("  No completed waits recorded yet.\n")
        return

    rows = []
    for node_id, s in stats.items():
        avg      = s["total"] / s["count"] if s["count"] else 0.0
        ns_split = splits.get(node_id, 0.5)
        cycle    = cycles.get(node_id, 45.0)
        rows.append((node_id, avg, s["max"], s["count"], ns_split, cycle))
    rows.sort(key=lambda r: r[1], reverse=True)

    # Header
    print(f"  {BOLD}{'Node ID':<18} {'Avg':>7} {'Max':>7} {'Vehs':>6}  "
          f"{'Cycle':>6}  {'Split':>6}  Green Balance{RESET}")
    print(f"  {'-'*18} {'-'*7} {'-'*7} {'-'*6}  {'-'*6}  {'-'*6}  {'-'*20}")

    for node_id, avg, mx, cnt, ns_split, cycle in rows[:20]:
        color  = _wait_color(avg)
        ns_pct = int(round(ns_split * 100))
        ew_pct = 100 - ns_pct
        ns_bar = "N" * int(round(ns_split * 10))
        ew_bar = "E" * (10 - len(ns_bar))
        split_bar = f"[{BOLD}{ns_bar}{RESET}{ew_bar}] {ns_pct}%N/{ew_pct}%E"
        print(f"  {str(node_id):<18} "
              f"{color}{avg:>6.1f}s{RESET} "
              f"{mx:>6.1f}s "
              f"{cnt:>6,}  "
              f"{cycle:>5.0f}s  "
              f"{ns_split:>6.3f}  "
              f"{split_bar}")

    if len(rows) > 20:
        print(f"  {DIM}... and {len(rows) - 20} more signals{RESET}")

    # City-wide totals
    overall_total = sum(s["total"] for s in stats.values())
    overall_count = sum(s["count"] for s in stats.values())
    overall_avg   = overall_total / overall_count if overall_count else 0
    overall_max   = max(s["max"]   for s in stats.values())
    avg_cycle     = (sum(cycles.values()) / len(cycles)) if cycles else 45.0

    print(f"  {'-'*18} {'-'*7} {'-'*7} {'-'*6}  {'-'*6}")
    color = _wait_color(overall_avg)
    print(f"  {BOLD}{'CITY-WIDE TOTAL':<18}{RESET} "
          f"{color}{overall_avg:>6.1f}s{RESET} "
          f"{overall_max:>6.1f}s "
          f"{overall_count:>6,}  "
          f"{DIM}avg cycle={avg_cycle:.1f}s{RESET}")

    # RL summary
    if splits:
        split_vals = list(splits.values())
        avg_split  = sum(split_vals) / len(split_vals)
        diverged   = sum(1 for s in split_vals if abs(s - 0.5) > 0.1)
        converged  = sum(1 for s in split_vals if abs(s - 0.5) <= 0.02)

        if cycles:
            min_c = min(cycles.values())
            max_c = max(cycles.values())
            cycle_range = f"cycle range={min_c:.0f}s–{max_c:.0f}s"
        else:
            cycle_range = ""

        if mode == "ADAPTIVE":
            print(f"\n  🤖 RL  {len(splits)} signals | avg split={avg_split:.3f} | "
                  f"{diverged} optimized | {converged} converged | "
                  f"{rl_updates} updates | {cycle_range}")
        elif mode == "PRETRAINED":
            print(f"\n  📂 Pretrained  {len(splits)} signals | avg split={avg_split:.3f} | {cycle_range}")

    mtime = os.path.getmtime(WAIT_STATS_PATH)
    age_s = time.time() - mtime
    print(f"  {DIM}(wait_stats.json last updated {age_s:.0f}s ago){RESET}")
    print(f"{BOLD}{'═'*80}{RESET}\n")


def main():
    parser = argparse.ArgumentParser(description="Live signal wait-time report.")
    parser.add_argument("--watch",    action="store_true", help="Continuously refresh")
    parser.add_argument("--interval", type=float, default=10.0, help="Refresh interval in seconds")
    args = parser.parse_args()

    if args.watch:
        print(f"Watching wait_stats.json (refresh every {args.interval}s) — Ctrl-C to stop")
        try:
            while True:
                print("\033[2J\033[H", end="")
                print_report()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        print_report()


if __name__ == "__main__":
    main()
