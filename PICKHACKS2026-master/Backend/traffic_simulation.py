"""
traffic_simulation.py
---------------------
Handles vehicle spawning, movement, intersection queuing, and road heat.

Signal logic is delegated to signal_model.py.
Wait-time reporting is handled by wait_report.py.
"""

import json
import os
import random
from shapely.geometry import LineString
from collections import defaultdict

import signal_model

WAIT_STATS_PATH = os.path.join(os.path.dirname(__file__), "wait_stats.json")

# ============================
# Global state
# ============================
vehicles = []
edge_queues = {}
vehicle_delay = {}
initialized = False
current_vol_bin = -1   # tracks volume_multiplier for re-spawn
current_spd_bin = -1   # tracks speed_multiplier for re-spawn (BUG FIX: was missing)

edge_queues   = {}    # { (u,v,key): [vehicle_ids...] }
vehicle_delay = {}    # { vehicle_id: cumulative delay seconds }
signal_timer  = 0.0   # global simulation clock (seconds)

SIGNAL_CYCLE             = signal_model.DEFAULT_CYCLE
SATURATION_FLOW_PER_LANE = 1900
SIMULATION_STEP_TIME     = 1.0
VOLUME_DENSITY_FACTOR    = 500

# ============================
# Wait-time stats
# Written here; read by wait_report.py
# { node_id: { "total": float, "count": int, "max": float } }
# ============================
node_wait_stats: dict = {}

# ============================
# Road capacity table
# ============================
ROAD_CLASS_CAP = {
    "motorway":       10.0,
    "motorway_link":   8.0,
    "trunk":           7.0,
    "trunk_link":      6.0,
    "primary":         5.0,
    "primary_link":    4.5,
    "secondary":       3.5,
    "secondary_link":  3.0,
    "tertiary":        2.7,
    "tertiary_link":   2.4,
    "residential":     1.8,
    "unclassified":    2.0,
    "service":         1.2,
    "living_street":   1.0,
}


# ============================
# Vehicle spawning
# ============================
def initialize_vehicles(G, volume_multiplier=1.0, speed_multiplier=1.0):
    """
    Fully data-driven vehicle spawning.

    Uses:
    - GeoJSON traffic_volume for spatial weighting
    - Hourly demand multiplier (volume_multiplier)
    - Hourly speed multiplier (speed_multiplier)

    NOTE: vehicles store the RAW base speed here.
          The speed_multiplier is applied in the simulation step only,
          so it is never applied twice. (BUG FIX)
    """
    global vehicles, edge_queues, vehicle_delay

    vehicles = []
    edge_queues = {}
    vehicle_delay = {}
    vehicle_id = 0
    node_wait_stats.clear()
    signal_model.reset()

    edges_data   = []
    total_weight = 0.0

    for u, v, key, data in G.edges(keys=True, data=True):
        base_volume = data.get("traffic_volume", 0)
        if base_volume <= 0:
            base_volume = 0.01

        if data.get("junction") == "roundabout":
            base_volume *= 0.15  # heavily reduce spawn weight
        # Apply hourly demand multiplier
        adjusted_volume = float(base_volume) * float(volume_multiplier)

        total_weight += adjusted_volume
        edges_data.append((u, v, key, data, adjusted_volume))

    if total_weight <= 0:
        print("No traffic weights available.")
        return

    # 2) Determine total city cars.
    #    Rolla population ~20k; even at 3am there are meaningful vehicles present.
    #    A floor of 0.15 prevents the city from going nearly empty off-peak. (BUG FIX)
    BASE_CITY_CARS = 1600  # tune this for peak hour feel
    MAX_CITY_CARS = max(1, int(BASE_CITY_CARS * float(volume_multiplier)))

    print(f"Spawning {MAX_CITY_CARS} vehicles for this hour (vol_mult={volume_multiplier:.2f}).")

    # 3) Distribute proportionally — sample edges first, then group by edge so we
    #    can assign evenly-spaced progress values. Random progress caused multiple
    #    cars to spawn on top of each other on short edges, triggering emergency
    #    braking (-10 m/s²) on the very first tick and leaving highway cars near 0.
    edge_pool = [(u, v, key, data) for u, v, key, data, _ in edges_data]
    weights    = [w for _, _, _, _, w in edges_data]

    sampled_edges = random.choices(edge_pool, weights=weights, k=MAX_CITY_CARS)

    # Group sampled cars by their edge so we can space them properly
    from collections import defaultdict as _dd
    edge_groups = _dd(list)
    for u, v, key, data in sampled_edges:
        edge_groups[(u, v, key)].append(data)

    for (u, v, key), data_list in edge_groups.items():
        n = len(data_list)
        data = data_list[0]  # edge attributes are the same for all cars on this edge

        # Evenly space cars across the edge with a small random jitter (±half-slot).
        # This guarantees a minimum gap of (length / n) metres — no pile-ups.
        slot = 1.0 / (n + 1)
        is_roundabout = data.get("junction") == "roundabout"

        for rank, _ in enumerate(data_list):
            if is_roundabout:
                progress = (rank % 10) / 10.0
            else:
                # Centre of each slot + tiny jitter so cars don't look mechanical
                base_prog = slot * (rank + 1)
                jitter    = random.uniform(-slot * 0.4, slot * 0.4)
                progress  = max(0.01, min(0.99, base_prog + jitter))

            std        = data.get('traffic_speed_std', 5.0)
            base_speed = random.gauss(data.get('speed_kph', 30), std * 0.5)
            base_speed = max(10.0, base_speed)

            vehicles.append({
                "id":               vehicle_id,
                "u":                u,
                "v":                v,
                "key":              key,
                "progress":         progress,
                "speed_kph":        base_speed,
                # Apply speed_multiplier at spawn so curr_v == v0 on tick 0.
                # Without this, cars that spawn above their desired speed instantly
                # brake hard; cars below it creep until IDM ramps them up.
                "current_speed_ms": (base_speed * speed_multiplier) / 3.6,
            })
            vehicle_id += 1

    print(f"Simulation loaded with {len(vehicles)} vehicles.")


# ============================
# Helpers
# ============================
def _edge_length_m(edge_data) -> float:
    """Use OSMnx 'length' attribute (meters) for consistent physics."""
    try:
        return max(1.0, float(edge_data.get("length", 10.0)))
    except Exception:
        return 10.0


def _point_on_edge(G, u, v, key, progress):
    """
    Return (lat, lon) at normalized progress along the edge.
    Uses geometry if present (curved roads), else interpolates node-to-node.
    """
    edge_data = G[u][v][key]
    geom = edge_data.get("geometry")
    p    = max(0.0, min(1.0, float(progress)))

    if geom is not None:
        try:
            line = geom if isinstance(geom, LineString) else LineString(list(geom.coords))
            if line.length > 0:
                pt = line.interpolate(p, normalized=True)
                # Shapely geometry coords are (lon, lat)
                return pt.y, pt.x
        except Exception:
            pass

    u_n = G.nodes[u]
    v_n = G.nodes[v]
    return (
        u_n["y"] + p * (v_n["y"] - u_n["y"]),
        u_n["x"] + p * (v_n["x"] - u_n["x"]),
    )


def _end_wait(vehicle_id: int):
    """
    Release a vehicle from its signal wait.
    Retrieves wait duration from signal_model and stores it in node_wait_stats.
    """
    entry = signal_model._vehicle_wait_start.get(vehicle_id)
    if entry is None:
        return
    node_id = entry[0]

    wait_s = signal_model.record_wait_end(vehicle_id, signal_timer)
    if wait_s is None or wait_s <= 0:
        return

    stats = node_wait_stats.setdefault(node_id, {"total": 0.0, "count": 0, "max": 0.0})
    stats["total"] += wait_s
    stats["count"] += 1
    stats["max"]    = max(stats["max"], wait_s)


    # ============================
    # Main simulation step
    # ============================
# ============================
    # Main simulation step
    # ============================
def get_traffic_positions(G, speed_multiplier=1.0, volume_multiplier=1.0, dt=1.0):
    """
    Update vehicle positions using IDM for acceleration and 
    maintaining existing intersection/queuing logic.
    """
    global initialized, current_vol_bin, current_spd_bin, vehicles, edge_queues, vehicle_delay, signal_timer

    signal_timer += float(dt)

    vol_bin = round(volume_multiplier, 2)
    spd_bin = round(speed_multiplier, 2)
    if not initialized or vol_bin != current_vol_bin or spd_bin != current_spd_bin:
        initialize_vehicles(G, volume_multiplier, speed_multiplier)
        # Ensure new vehicles have a speed state
        for v in vehicles:
            if "current_speed_ms" not in v:
                v["current_speed_ms"] = (v["speed_kph"] * speed_multiplier) / 3.6
        initialized = True
        current_vol_bin = vol_bin
        current_spd_bin = spd_bin

    # 1. Group vehicles by edge and sort them so we know who is in front
    edge_map = defaultdict(list)
    for v in vehicles:
        edge_map[(v["u"], v["v"], v["key"])].append(v)

    positions = []
    all_edges_list = list(G.edges(keys=True, data=True))

    # 2. Iterate through each road segment
    for edge_id, road_vehicles in edge_map.items():
        # Sort by progress (highest progress first = leader)
        road_vehicles.sort(key=lambda x: x["progress"], reverse=True)

        u_orig, v_orig, key_orig = edge_id
        edge_data = G[u_orig][v_orig][key_orig]
        length_m = _edge_length_m(edge_data)

        for i, vehicle in enumerate(road_vehicles):
            # Desired speed for this specific road
            v0 = (vehicle["speed_kph"] * speed_multiplier) / 3.6
            v0 = max(v0, 5.0)
            curr_v = vehicle.get("current_speed_ms", v0)

            # speed_multiplier applied at step time via v0 (IDM desired speed)
            # vehicle["speed_kph"] always stores raw base speed. (BUG FIX)

            # Determine IDM acceleration based on car in front
            if i == 0:
                accel = get_idm_acceleration(curr_v, v0, 1000.0, v0)
            else:
                leader = road_vehicles[i - 1]
                gap = (leader["progress"] - vehicle["progress"]) * length_m - 4.0
                accel = get_idm_acceleration(curr_v, leader["current_speed_ms"], gap, v0)

            new_v = max(0.0, curr_v + accel * dt)
            vehicle["current_speed_ms"] = new_v
            remaining_m = max(0.0, (curr_v * dt) + (0.5 * accel * (dt ** 2)))

            current_edge_id = (vehicle["u"], vehicle["v"], vehicle["key"])
            if current_edge_id not in edge_queues:
                edge_queues[current_edge_id] = []

            teleported_this_tick = False
            hops_left = 25

            while remaining_m > 0 and hops_left > 0:
                hops_left -= 1

                u, v, key = vehicle["u"], vehicle["v"], vehicle["key"]
                edge_data = G[u][v][key]
                length_m = _edge_length_m(edge_data)
                dist_left_on_edge = (1.0 - vehicle["progress"]) * length_m

                if remaining_m < dist_left_on_edge:
                    vehicle["progress"] += remaining_m / length_m
                    remaining_m = 0
                    continue

                remaining_m -= dist_left_on_edge
                current_node = v
                control = G.nodes[current_node].get("control", "none")

                if control == "signal":
                    is_green = signal_model.is_green_for_edge(u, v, key, G, signal_timer)

                    lanes = edge_data.get("lanes", 1)
                    if isinstance(lanes, list):
                        lanes = lanes[0]
                    try:
                        lanes = int(lanes)
                    except (ValueError, TypeError):
                        lanes = 1

                    effective_lanes = float(lanes)
                    if effective_lanes == 1 and vehicle.get("is_turning_left"):
                        effective_lanes *= 0.5

                    discharge_rate = (SATURATION_FLOW_PER_LANE * effective_lanes / 3600) * float(dt)

                    if vehicle["id"] not in edge_queues[current_edge_id]:
                        if not is_green:
                            edge_queues[current_edge_id].append(vehicle["id"])
                            vehicle["is_turning_left"] = random.random() < 0.2
                            direction = signal_model.edge_direction(u, v, G)
                            signal_model.record_wait_start(vehicle["id"], current_node, direction, signal_timer)

                    if vehicle["id"] in edge_queues[current_edge_id]:
                        queue = edge_queues[current_edge_id]
                        pos_in_q = queue.index(vehicle["id"])

                        discharged = max(1, int(discharge_rate) + (1 if random.random() < (discharge_rate % 1) else 0))
                        if is_green and pos_in_q < discharged:
                            queue.pop(pos_in_q)
                            _end_wait(vehicle["id"])
                            # Fall through to next-edge selection
                        else:
                            vehicle_delay[vehicle["id"]] = vehicle_delay.get(vehicle["id"], 0) + float(dt)
                            pin_pos = min(0.999, 0.97 - (pos_in_q * 0.015))
                            vehicle["progress"] = max(vehicle["progress"], pin_pos)  # never go backward
                            remaining_m = 0
                            vehicle["current_speed_ms"] = 0
                            remaining_m = 0
                            vehicle["current_speed_ms"] = 0
                            continue

                elif control == "priority":
                    major_roads = {"primary", "secondary", "trunk"}
                    incoming_highway = edge_data.get("highway", "residential")
                    if isinstance(incoming_highway, list):
                        incoming_highway = incoming_highway[0]
                    coming_from_major = str(incoming_highway) in major_roads

                    if coming_from_major:
                        vehicle["stopped_at_node"] = False
                        vehicle["stop_timer"] = 0.0
                    else:
                        if not vehicle.get("stopped_at_node"):
                            vehicle["stop_timer"] = 1.0
                            vehicle["stopped_at_node"] = True

                        gap_is_safe = check_gap_acceptance(G, v, vehicle)

                        if vehicle.get("stop_timer", 0.0) > 0.0 or not gap_is_safe:
                            vehicle["stop_timer"] = max(0.0, vehicle.get("stop_timer", 0.0) - float(dt))
                            vehicle["progress"] = max(vehicle["progress"], 0.97)  # never go backward
                            remaining_m = 0.0
                            vehicle["current_speed_ms"] = 0
                            remaining_m = 0.0
                            vehicle["current_speed_ms"] = 0
                            vehicle_delay[vehicle["id"]] = vehicle_delay.get(vehicle["id"], 0.0) + float(dt)
                            continue
                        else:
                            vehicle["stopped_at_node"] = False

                # Transition to next road
                next_options = list(G.out_edges(current_node, keys=True))
                if next_options:
                    forward_options = [opt for opt in next_options if opt[1] != u]
                    new_u, new_v, new_key = random.choice(forward_options if forward_options else next_options)
                else:
                    new_u, new_v, new_key = random.choice(list(G.edges(keys=True)))
                    teleported_this_tick = True

                vehicle["u"], vehicle["v"], vehicle["key"] = new_u, new_v, new_key
                vehicle["progress"] = 0.0
                edge_id = (new_u, new_v, new_key)

                new_data = G[new_u][new_v][new_key]
                # Use the legal speed limit (speed_kph) as the desired speed (v0).
                # traffic_speed is observed average speed and may be artificially low
                # (congested samples), which would cap the car's goal speed on highways.
                new_speed = new_data.get("speed_kph", 30)
                if isinstance(new_speed, list):
                    new_speed = new_speed[0]
                vehicle["speed_kph"] = float(new_speed)  # desired speed = speed limit
                # Do NOT reset current_speed_ms here — let IDM smoothly
                # accelerate / decelerate the car to the new desired speed.
                # Resetting it caused instant speed jumps (and sharp braking on highways).

            lat, lon = _point_on_edge(G, vehicle["u"], vehicle["v"], vehicle["key"], vehicle["progress"])
            positions.append({"id": vehicle["id"], "lat": lat, "lon": lon, "teleport": teleported_this_tick})

    signal_model.maybe_rl_update(signal_timer)
    _flush_wait_stats()

    return positions


def _flush_wait_stats():
    """Write node_wait_stats + signal_model state to wait_stats.json for wait_report.py."""
    try:
        payload = {
            "sim_time":    signal_timer,
            "signal_mode": signal_model.SIGNAL_MODE,
            "rl_updates":  len(signal_model.update_log),
            "node_splits": {str(k): v for k, v in signal_model.node_ns_split.items()},
            "node_cycles": {str(k): v for k, v in signal_model.node_cycle.items()},
            "wait_stats":  {str(k): v for k, v in node_wait_stats.items()},
        }
        tmp = WAIT_STATS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, WAIT_STATS_PATH)
    except Exception:
        pass  # never let reporting crash the simulation


# ============================
# Road heatmap
# ============================
def get_road_heat(G):
    """
    Returns:
    {
        "counts": {edge_id: car_count},
        "heat":   {edge_id: 0..1.5+ congestion ratio}
    }
    """
    counts = {}
    for veh in vehicles:
        eid = f'{veh["u"]}-{veh["v"]}'
        counts[eid] = counts.get(eid, 0) + 1

    heat = {}
    for u, v, key, data in G.edges(keys=True, data=True):
        eid = f"{u}-{v}"

        lanes = data.get("lanes", 1)
        if isinstance(lanes, list):
            lanes = lanes[0]
        try:
            lanes = max(1, int(lanes))
        except Exception:
            lanes = 1

        highway = data.get("highway", "residential")
        if isinstance(highway, list):
            highway = highway[0]

        road_mult = ROAD_CLASS_CAP.get(str(highway), 2.0)
        try:
            length_m = float(data.get("length", 100))
        except Exception:
            length_m = 100.0

        length_factor = max(0.5, length_m / 100.0)
        capacity      = lanes * road_mult * length_factor
        car_count     = counts.get(eid, 0)
        heat[eid]     = (car_count / capacity) if capacity > 0 else 0

    return {"counts": counts, "heat": heat}


# ============================
# Traffic light state export (for UI)
# ============================
def get_signal_states(G):
    """
    Returns a list of signalized intersections with current phase.

    Uses the same per-node cycle length and NS split that signal_model uses
    during simulation, so the UI always matches what the vehicles experience.

    Output format:
      [
        {"id": "<node_id>", "lat": float, "lon": float,
         "ns": "green|red", "ew": "green|red",
         "cycle_pos": float, "cycle_len": float},
        ...
      ]
    """
    global signal_timer

    out = []
    for node_id, data in G.nodes(data=True):
        if data.get("control") != "signal":
            continue

        lat = data.get("y")
        lon = data.get("x")
        if lat is None or lon is None:
            continue

        # Use per-node learned values (falls back to defaults if not yet initialised)
        cycle     = signal_model._node_cycle(node_id)
        ns_frac   = signal_model._node_split(node_id)
        cycle_pos = signal_timer % cycle
        ns_green  = cycle_pos < (cycle * ns_frac)

        out.append({
            "id":        str(node_id),
            "lat":       float(lat),
            "lon":       float(lon),
            "ns":        "green" if ns_green else "red",
            "ew":        "green" if not ns_green else "red",
            "cycle_pos": float(cycle_pos),
            "cycle_len": float(cycle),
        })

    return out

def get_idm_acceleration(v, v_lead, gap, v0):
    """
    Calculates acceleration based on IDM formula.
    v: current speed (m/s)
    v_lead: speed of car in front (m/s)
    gap: distance to car in front (m)
    v0: desired speed (m/s) based on speed_multiplier
    """
    # Parameters for realistic driving
    is_highway = v0 > 20 # ~72 km/h
    a = 2.0 if is_highway else 1.5 # Max acceleration m/s^2
    T = 1.0 if is_highway else 1.5 # Desired time headway (s)
    b = 2.0       # Comfortable deceleration m/s^2
    s0 = 2.0      # Minimum jam distance (m)
    delta = 4.0   # Acceleration exponent
    
    # Safety check for collisions
    if gap <= 0.1: return -b * 5 

    delta_v = v - v_lead
    # Desired gap s*
    s_star = s0 + max(0, (v * T) + (v * delta_v) / (2 * (a * b)**0.5))
    
    # IDM Formula
    acceleration = a * (1 - (v / v0)**delta - (s_star / gap)**2)
    return acceleration


def check_gap_acceptance(G, current_node, vehicle):
    """
    Checks if there is a safe gap on the major road.
    Now takes the 'vehicle' object to check its wait time and road type.
    """
    # Track wait time for patience logic
    wait_time = vehicle.get("wait_at_intersection_start", 0)
    
    for u, v, key, data in G.in_edges(current_node, keys=True, data=True):
        highway = data.get("highway", "residential")
        if isinstance(highway, list): highway = highway[0]
        
        if highway in {"primary", "secondary", "trunk", "motorway"}:
            major_vehicles = [veh for veh in vehicles if veh["u"] == u and veh["v"] == v]
            if not major_vehicles:
                continue
            
            major_vehicles.sort(key=lambda x: x["progress"], reverse=True)
            lead_veh = major_vehicles[0]
            
            # Distance and Time calculation
            dist_remaining = (1.0 - lead_veh["progress"]) * _edge_length_m(data)
            speed_ms = max(0.1, lead_veh.get("current_speed_ms", 10.0))
            time_to_arrival = dist_remaining / speed_ms
            
            # DYNAMIC THRESHOLD CALL
            dynamic_threshold = get_critical_gap(highway, wait_time)
            
            if time_to_arrival < dynamic_threshold:
                return False 
    return True


def get_critical_gap(highway_type, wait_time=0.0):
    """
    Returns the required gap in seconds based on road type 
    and how long the driver has been waiting.
    """
    # Base gaps (in seconds) for different road types
    # Highways/Trunks usually have lower critical gaps in sims to favor flow
    base_gaps = {
        "motorway": 2.5,
        "trunk": 3.0,
        "primary": 3.5,
        "secondary": 4.0,
        "tertiary": 4.5,
        "residential": 4.5
    }
    
    threshold = base_gaps.get(str(highway_type), 4.0)
    
    # Patience factor: reduce required gap by 0.1s for every second waited
    # but never go below a 'suicidal' 1.5s gap.
    patience_reduction = min(2.0, wait_time * 0.1)
    
    # Add a bit of driver personality (stochasticity)
    driver_variability = random.uniform(-0.5, 0.5)
    
    return max(1.5, threshold - patience_reduction + driver_variability)