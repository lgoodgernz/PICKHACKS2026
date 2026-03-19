"""
signal_model.py
---------------
Owns all traffic signal state and RL learning logic.

Each signalized intersection independently learns two parameters:
  - ns_split:     fraction of its cycle given to N-S traffic  (rest goes to E-W)
  - cycle_length: total green+red cycle duration in seconds

The RL agent hill-climbs both parameters every RL_UPDATE_INTERVAL sim-seconds:
  - Split  nudges toward whichever direction has the longer average wait
  - Cycle  shrinks when both directions are waiting well under the current
           half-cycle (wasted green time) and grows when either direction
           routinely waits longer than a full cycle (severe congestion)

Modes:
  "fixed"      -> 50/50 split, fixed SIGNAL_CYCLE for every node, no learning
  "adaptive"   -> learns split + cycle per node, saves to signal_model.json
  "pretrained" -> loads signal_model.json, learning frozen
"""

import json
import os

# ============================
# Configuration
# ============================

SIGNAL_MODE = "pretrained"   # "fixed" | "adaptive" | "pretrained"

# Default cycle used at startup and in fixed mode
DEFAULT_CYCLE  = 45      # seconds  (shorter than before → lower theoretical floor)

# Per-node cycle bounds
MIN_CYCLE      = 20      # never shorter than this
MAX_CYCLE      = 90      # never longer than this

# Split bounds
MIN_SPLIT      = 0.15
MAX_SPLIT      = 1.0 - MIN_SPLIT

# Learning rates
SPLIT_LR       = 0.15    # how aggressively to shift the NS/EW split
CYCLE_LR       = 3.0     # seconds to add/subtract from cycle per update

# How often (sim-seconds) each node re-evaluates its policy
RL_UPDATE_INTERVAL = 30  # faster learning: was 60

# Save cadence
RL_SAVE_EVERY  = 5       # save every N update rounds (0 = every round)

SIGNAL_MODEL_PATH = os.path.join(os.path.dirname(__file__), "signal_model.json")

# ============================
# Per-node learned parameters
# ============================

# { node_id: float }  NS green fraction (0.5 = 50/50)
node_ns_split:    dict = {}

# { node_id: float }  cycle length in seconds
node_cycle:       dict = {}

# { node_id: { "ns": [wait_s,...], "ew": [wait_s,...] } }
node_pending_waits: dict = {}

# { node_id: float }  sim-time of last RL update
node_last_rl_update: dict = {}

# ============================
# Per-vehicle wait tracking
# ============================

# { vehicle_id: (node_id, sim_time_started) }
_vehicle_wait_start: dict = {}

# { vehicle_id: "ns" | "ew" }
_vehicle_wait_dir: dict = {}

# ============================
# Logging
# ============================

# [(sim_t, node_id, old_split, new_split, old_cycle, new_cycle, ns_avg, ew_avg)]
update_log:    list = []
update_rounds: int  = 0


# ============================
# Helpers
# ============================

def _node_cycle(node_id) -> float:
    """Return the current learned cycle for a node, defaulting to DEFAULT_CYCLE."""
    return node_cycle.get(node_id, DEFAULT_CYCLE)


def _node_split(node_id) -> float:
    """Return the current learned NS split for a node, defaulting to 0.5."""
    return node_ns_split.get(node_id, 0.5)


# ============================
# Save / Load
# ============================

def save_model():
    """Write learned splits and cycle lengths to signal_model.json."""
    if not node_ns_split and not node_cycle:
        return
    payload = {
        "signal_mode":      SIGNAL_MODE,
        "default_cycle":    DEFAULT_CYCLE,
        "total_rl_updates": len(update_log),
        "splits": {str(k): v for k, v in node_ns_split.items()},
        "cycles": {str(k): v for k, v in node_cycle.items()},
    }
    with open(SIGNAL_MODEL_PATH, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"  💾 [signal_model] Saved {len(node_ns_split)} signals → {SIGNAL_MODEL_PATH}")


def load_model() -> bool:
    """Load learned parameters from signal_model.json."""
    global node_ns_split, node_cycle
    if not os.path.exists(SIGNAL_MODEL_PATH):
        return False
    try:
        with open(SIGNAL_MODEL_PATH) as f:
            payload = json.load(f)
        node_ns_split = {int(k): float(v) for k, v in payload.get("splits", {}).items()}
        node_cycle    = {int(k): float(v) for k, v in payload.get("cycles", {}).items()}
        total = payload.get("total_rl_updates", "?")
        avg_c = (sum(node_cycle.values()) / len(node_cycle)) if node_cycle else DEFAULT_CYCLE
        print(f"\n  ✅ [signal_model] Loaded {len(node_ns_split)} signals from {SIGNAL_MODEL_PATH}")
        print(f"     {total} RL updates | avg cycle={avg_c:.1f}s\n")
        return True
    except Exception as e:
        print(f"  ⚠️  [signal_model] Load failed: {e} — starting fresh\n")
        return False


def reset():
    """Clear per-session accumulators (keep learned splits/cycles across hour changes)."""
    _vehicle_wait_start.clear()
    _vehicle_wait_dir.clear()
    node_pending_waits.clear()


# ============================
# Green-light query
# ============================

def is_green_for_edge(u, v, key, G, current_timer: float) -> bool:
    """Return True if the signal at node v is green for traffic arriving from u."""
    if G.nodes[v].get("control") != "signal":
        return True

    u_d = G.nodes[u]
    v_d = G.nodes[v]
    is_ns = abs(u_d["y"] - v_d["y"]) > abs(u_d["x"] - v_d["x"])

    if SIGNAL_MODE in ("adaptive", "pretrained"):
        # Initialise on first encounter
        if v not in node_ns_split:
            node_ns_split[v] = 0.5
        if v not in node_cycle:
            node_cycle[v] = DEFAULT_CYCLE
        ns_frac = node_ns_split[v]
        cycle   = node_cycle[v]
    else:
        ns_frac = 0.5
        cycle   = DEFAULT_CYCLE

    ns_green_s = cycle * ns_frac
    cycle_pos  = current_timer % cycle

    return (cycle_pos < ns_green_s) if is_ns else (cycle_pos >= ns_green_s)


# ============================
# Wait tracking
# ============================

def edge_direction(u, v, G) -> str:
    u_d = G.nodes[u]
    v_d = G.nodes[v]
    return "ns" if abs(u_d["y"] - v_d["y"]) > abs(u_d["x"] - v_d["x"]) else "ew"


def record_wait_start(vehicle_id: int, node_id, direction: str, signal_timer: float):
    if vehicle_id not in _vehicle_wait_start:
        _vehicle_wait_start[vehicle_id] = (node_id, signal_timer)
        _vehicle_wait_dir[vehicle_id]   = direction


def record_wait_end(vehicle_id: int, signal_timer: float):
    """
    Called when a vehicle clears a signal.
    Returns wait_s (float) or None.
    Also feeds the wait into the RL accumulator.
    """
    if vehicle_id not in _vehicle_wait_start:
        return None
    node_id, start_t = _vehicle_wait_start.pop(vehicle_id)
    direction        = _vehicle_wait_dir.pop(vehicle_id, "ns")
    wait_s           = signal_timer - start_t
    if wait_s <= 0:
        return None

    if SIGNAL_MODE == "adaptive":
        pending = node_pending_waits.setdefault(node_id, {"ns": [], "ew": []})
        pending[direction].append(wait_s)

    return wait_s


# ============================
# RL update
# ============================

def maybe_rl_update(signal_timer: float):
    """
    Hill-climbing RL for split AND cycle length.

    Split update:
      gradient = (ns_avg - ew_avg) / (ns_avg + ew_avg)   ∈ [-1, 1]
      new_split = old_split + SPLIT_LR * gradient

    Cycle update:
      overall_avg = mean of all completed waits this window
      ideal_half  = current_cycle / 2
      If avg wait << ideal_half  → cycle is too long, shrink it
      If avg wait >> ideal_half  → queues aren't clearing, grow it
      delta = CYCLE_LR * sign(overall_avg - ideal_half * 0.6)
    """
    global update_rounds

    if SIGNAL_MODE != "adaptive":
        return

    updated = []

    for node_id, pending in list(node_pending_waits.items()):
        last = node_last_rl_update.get(node_id, 0)
        if signal_timer - last < RL_UPDATE_INTERVAL:
            continue

        ns_waits = pending.get("ns", [])
        ew_waits = pending.get("ew", [])
        all_waits = ns_waits + ew_waits

        if len(all_waits) < 3:   # need meaningful sample
            continue

        ns_avg      = sum(ns_waits) / len(ns_waits) if ns_waits else 0.0
        ew_avg      = sum(ew_waits) / len(ew_waits) if ew_waits else 0.0
        overall_avg = sum(all_waits) / len(all_waits)
        total_dir   = ns_avg + ew_avg

        # ---- Split update ----
        old_split = _node_split(node_id)
        if total_dir > 0:
            gradient  = (ns_avg - ew_avg) / total_dir
            new_split = old_split + SPLIT_LR * gradient
            new_split = max(MIN_SPLIT, min(MAX_SPLIT, new_split))
        else:
            new_split = old_split

        # ---- Cycle update ----
        old_cycle  = _node_cycle(node_id)
        ideal_half = old_cycle / 2.0

        # Target: avg wait should be ~40% of half-cycle (efficient but not too tight)
        target_wait = ideal_half * 0.4

        if overall_avg < target_wait:
            # Waits are short → cycle is wastefully long → shrink
            cycle_delta = -CYCLE_LR
        elif overall_avg > ideal_half * 1.1:
            # Waits exceed half the cycle → queues not clearing → grow
            cycle_delta = +CYCLE_LR
        else:
            cycle_delta = 0.0

        new_cycle = max(MIN_CYCLE, min(MAX_CYCLE, old_cycle + cycle_delta))

        # Commit
        node_ns_split[node_id]       = new_split
        node_cycle[node_id]          = new_cycle
        node_last_rl_update[node_id] = signal_timer
        node_pending_waits[node_id]  = {"ns": [], "ew": []}

        changed = abs(new_split - old_split) > 0.001 or abs(new_cycle - old_cycle) > 0.1
        if changed:
            update_log.append((signal_timer, node_id,
                                old_split, new_split,
                                old_cycle, new_cycle,
                                ns_avg, ew_avg))
            updated.append((node_id,
                             old_split, new_split,
                             old_cycle, new_cycle,
                             ns_avg, ew_avg))

    if updated:
        update_rounds += 1
        print(f"\n  🤖 [RL @ t={signal_timer:.0f}s] {len(updated)} signal(s) updated:")
        print(f"  {'Node':<18} {'Split':>14} {'Cycle':>14} {'NS Avg':>9} {'EW Avg':>9}")
        print(f"  {'-'*66}")
        for (node_id, old_s, new_s, old_c, new_c, ns_a, ew_a) in updated[:10]:
            s_arrow = "↑" if new_s > old_s else ("↓" if new_s < old_s else "=")
            c_arrow = "↑" if new_c > old_c else ("↓" if new_c < old_c else "=")
            print(f"  {str(node_id):<18} "
                  f"{old_s:.3f}→{new_s:.3f} {s_arrow}  "
                  f"{old_c:.0f}s→{new_c:.0f}s {c_arrow}  "
                  f"{ns_a:>8.1f}s {ew_a:>8.1f}s")
        if len(updated) > 10:
            print(f"  ... and {len(updated) - 10} more")
        print()

        if RL_SAVE_EVERY == 0 or update_rounds % RL_SAVE_EVERY == 0:
            save_model()


# ============================
# Auto-load on import
# ============================
if SIGNAL_MODE in ("adaptive", "pretrained"):
    _loaded = load_model()
    if SIGNAL_MODE == "pretrained" and not _loaded:
        print("  ⚠️  [signal_model] No saved model — falling back to adaptive\n")
        SIGNAL_MODE = "adaptive"
