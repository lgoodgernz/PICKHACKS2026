"""
train_signals.py
----------------
Headless training script for the adaptive traffic signal model.

Runs the full traffic simulation in a tight loop — no Flask, no HTTP,
no browser required. Simulates an entire day of traffic repeatedly,
letting the RL agent accumulate experience and hill-climb signal timings
far faster than running the live server.

Usage:
    python train_signals.py                  # default: 3 training days
    python train_signals.py --days 10        # run 10 full simulated days
    python train_signals.py --days 5 --dt 2  # larger time steps (faster, less precise)
    python train_signals.py --hours 7 9 17   # only train on specific hours
    python train_signals.py --no-load        # start fresh, ignore existing model

The trained model is saved to signal_model.json and will be picked up
automatically when you restart the server in pretrained or adaptive mode.
"""

import argparse
import os
import sys
import time

import pandas as pd

# ── Make sure we can import sibling modules ──────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

import signal_model
import traffic_simulation as sim
from network import load_network


# ── Build the same hour→multiplier tables as app.py ─────────────────────────
CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "RollaReport.csv")

def _build_multipliers():
    df = pd.read_csv(CSV_PATH)
    df["Time"] = pd.to_datetime(df["Time"])
    df["hour"] = df["Time"].dt.hour

    hourly_speeds     = df.groupby("hour")["Speed [kmh]"].mean()
    hourly_freeflow   = df.groupby("hour")["Free flow speed [kmh]"].mean()
    hourly_congestion = df.groupby("hour")["Congestion level [%]"].mean()
    peak_congestion   = hourly_congestion.max()

    demand = {h: hourly_congestion[h] / peak_congestion for h in hourly_congestion.index}
    speed  = {h: hourly_speeds[h] / hourly_freeflow[h]  for h in hourly_speeds.index}
    return demand, speed


# ── Pretty progress bar ───────────────────────────────────────────────────────
def _bar(done, total, width=30):
    filled = int(width * done / max(total, 1))
    return f"[{'█' * filled}{'░' * (width - filled)}] {done}/{total}"


# ── Main training loop ────────────────────────────────────────────────────────
def train(days: int, dt: float, hours_to_train: list, fresh_start: bool):
    wall_start = time.time()

    # ── Signal mode must be adaptive ────────────────────────────────────────
    if fresh_start:
        print("\n🗑️  Fresh start — ignoring any existing model.\n")
        signal_model.node_ns_split.clear()
        signal_model.node_cycle.clear()
        signal_model.update_log.clear()
    else:
        print("\n📂 Loading existing model to continue training…")
        loaded = signal_model.load_model()
        if not loaded:
            print("   No saved model found — starting from scratch.\n")

    signal_model.SIGNAL_MODE = "adaptive"

    # ── Load road network (slow — only done once) ────────────────────────────
    print("🗺️  Loading road network (this may take ~30s the first time)…")
    t0 = time.time()
    G  = load_network()
    print(f"   Network loaded in {time.time() - t0:.1f}s  "
          f"({G.number_of_nodes()} nodes, {G.number_of_edges()} edges)\n")

    demand_mults, speed_mults = _build_multipliers()

    # Hours to simulate each day (default: all 24)
    hours = hours_to_train if hours_to_train else list(range(24))

    # Simulated seconds per hour we want to run
    # One real hour = 3600 sim-seconds.  We compress it: 600s gives the RL
    # agent enough ticks to accumulate meaningful wait samples per hour.
    SIM_SECONDS_PER_HOUR = 600

    total_steps   = days * len(hours) * int(SIM_SECONDS_PER_HOUR / dt)
    steps_done    = 0
    rl_updates_at_start = len(signal_model.update_log)

    print(f"🚦 Training for {days} day(s) × {len(hours)} hour(s) "
          f"= {days * len(hours)} hour-blocks")
    print(f"   dt={dt}s  |  {int(SIM_SECONDS_PER_HOUR/dt)} steps/hour-block  "
          f"|  {total_steps:,} total steps\n")

    for day in range(1, days + 1):
        day_wall = time.time()

        for hour in hours:
            v_mult = float(demand_mults.get(hour, 1.0))
            s_mult = float(speed_mults.get(hour,  1.0))

            hour_steps = int(SIM_SECONDS_PER_HOUR / dt)

            for step in range(hour_steps):
                sim.get_traffic_positions(
                    G,
                    speed_multiplier=s_mult,
                    volume_multiplier=v_mult,
                    dt=dt,
                )
                steps_done += 1

                # Print progress every 5% of total
                if steps_done % max(1, total_steps // 20) == 0:
                    pct      = steps_done / total_steps * 100
                    rl_total = len(signal_model.update_log)
                    elapsed  = time.time() - wall_start
                    eta      = (elapsed / steps_done) * (total_steps - steps_done)
                    print(f"  {_bar(steps_done, total_steps)}  "
                          f"{pct:5.1f}%  "
                          f"RL updates: {rl_total}  "
                          f"ETA: {eta:.0f}s")

        day_elapsed = time.time() - day_wall
        rl_this_day = len(signal_model.update_log) - rl_updates_at_start
        print(f"\n  ✅ Day {day}/{days} complete  "
              f"({day_elapsed:.1f}s wall)  "
              f"RL updates so far: {len(signal_model.update_log)}\n")

    # ── Final save ───────────────────────────────────────────────────────────
    print("💾 Saving final model…")
    signal_model.save_model()

    wall_elapsed  = time.time() - wall_start
    new_updates   = len(signal_model.update_log) - rl_updates_at_start
    signals_tuned = len(signal_model.node_ns_split)

    print(f"\n{'─'*55}")
    print(f"  Training complete in {wall_elapsed:.1f}s")
    print(f"  RL update rounds this session : {new_updates}")
    print(f"  Signals with learned timings  : {signals_tuned}")

    if signal_model.node_ns_split:
        avg_split = sum(signal_model.node_ns_split.values()) / signals_tuned
        avg_cycle = (sum(signal_model.node_cycle.values()) / len(signal_model.node_cycle)
                     if signal_model.node_cycle else signal_model.DEFAULT_CYCLE)
        print(f"  Avg NS split                  : {avg_split:.3f}  "
              f"({'N-heavy' if avg_split > 0.55 else 'S-heavy' if avg_split < 0.45 else 'balanced'})")
        print(f"  Avg cycle length              : {avg_cycle:.1f}s")

    print(f"{'─'*55}")
    print(f"\n  Model saved to: signal_model.json")
    print(f"  Set SIGNAL_MODE = \"pretrained\" in signal_model.py and")
    print(f"  restart the server to use the trained timings.\n")


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Headless trainer for the Rolla traffic signal RL model."
    )
    parser.add_argument(
        "--days", type=int, default=3,
        help="Number of full simulated days to train (default: 3)"
    )
    parser.add_argument(
        "--dt", type=float, default=1.0,
        help="Simulation time step in seconds (default: 1.0). "
             "Larger = faster training, less physical accuracy."
    )
    parser.add_argument(
        "--hours", type=int, nargs="+", metavar="H",
        help="Only train on specific hours, e.g. --hours 7 8 17 18. "
             "Default: all 24 hours."
    )
    parser.add_argument(
        "--no-load", action="store_true",
        help="Ignore any existing signal_model.json and train from scratch."
    )

    args = parser.parse_args()

    train(
        days=args.days,
        dt=args.dt,
        hours_to_train=args.hours or [],
        fresh_start=args.no_load,
    )
