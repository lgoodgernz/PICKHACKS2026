# Flask Backend
import pandas as pd
from flask import Flask, jsonify, request
from flask_cors import CORS

from network import load_network, add_road, remove_road, network_to_json
from traffic_simulation import get_traffic_positions, get_road_heat, get_signal_states

app = Flask(__name__)
CORS(app)

# ============================
# Load Hourly Pulse Data
# ============================
df = pd.read_csv('../RollaReport.csv')
df['Time'] = pd.to_datetime(df['Time'])
df['hour'] = df['Time'].dt.hour

hourly_speeds = df.groupby('hour')['Speed [kmh]'].mean()
hourly_freeflow = df.groupby('hour')['Free flow speed [kmh]'].mean()
hourly_congestion = df.groupby('hour')['Congestion level [%]'].mean()
peak_congestion = hourly_congestion.max()

demand_multipliers = {
    hour: hourly_congestion[hour] / peak_congestion
    for hour in hourly_congestion.index
}

speed_multipliers = {
    hour: hourly_speeds[hour] / hourly_freeflow[hour]
    for hour in hourly_speeds.index
}
# ============================
# Load Network
# ============================
G = load_network()


# ============================
# ROUTES
# ============================

@app.route('/roads', methods=['GET'])
def get_roads():
    return jsonify(network_to_json(G))


@app.route('/edit-road', methods=['POST'])
def edit_road():
    global G
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data received'}), 400

    action = data.get('action')

    if 'start_node' not in data or 'end_node' not in data:
        return jsonify({'error': 'Missing node data'}), 400

    if action == 'add':
        G = add_road(G, data)
    elif action == 'remove':
        G = remove_road(G, data)
    else:
        return jsonify({'error': 'Invalid action'}), 400

    return jsonify({'status': 'success'})


@app.route('/simulate', methods=['GET'])
def simulate():
    hour = int(request.args.get('hour', 12))

    s_mult = speed_multipliers.get(hour, 1.0)
    v_mult = demand_multipliers.get(hour, 1.0)

    positions = get_traffic_positions(
    G,
    speed_multiplier=s_mult,
    volume_multiplier=v_mult,
    dt=0.4
)

    return jsonify(positions)


@app.route('/road-heat', methods=['GET'])
def road_heat():
    """
    Advances the simulation one step, then returns:
    {
      "counts": {edge_id: car_count},
      "heat":   {edge_id: 0..1 normalized}
    }
    """

    hour = int(request.args.get('hour', 12))

    s_mult = speed_multipliers.get(hour, 1.0)
    v_mult = demand_multipliers.get(hour, 1.0)

    # ✅ Advance the simulation so cars actually move / exist
    _ = get_traffic_positions(
    G,
    speed_multiplier=s_mult,
    volume_multiplier=v_mult,
    dt=0.8
)

    # ✅ Now compute heat based on updated vehicle locations
    heat_data = get_road_heat(G)
    return jsonify(heat_data)


# ============================
# 🚦 Signals (traffic lights) for UI
# ============================
@app.route('/signals', methods=['GET'])
def signals():
    """Return signal nodes + their current phase for the frontend."""
    return jsonify(get_signal_states(G))


# ============================
# 🛑 Stop signs (priority intersections) for UI
# ============================
@app.route('/stops', methods=['GET'])
def stops():
    """Return priority-controlled nodes as stop-sign markers for the frontend."""
    out = []
    for node_id, data in G.nodes(data=True):
        if data.get('control') != 'priority':
            continue
        x = data.get('x')
        y = data.get('y')
        if x is None or y is None:
            continue
        out.append({
            'id': str(node_id),
            'lat': float(y),
            'lon': float(x)
        })
    return jsonify(out)


@app.route('/nodes', methods=['GET'])
def nodes():
    """Return all intersection nodes with their control type for hover tooltips."""
    out = []
    for node_id, data in G.nodes(data=True):
        control = data.get('control', 'none')
        x = data.get('x')
        y = data.get('y')
        if x is None or y is None:
            continue
        degree = G.degree(node_id)
        # Only return actual intersections (degree >= 3) plus signals/priority
        if degree < 3 and control == 'none':
            continue
        out.append({
            'id': str(node_id),
            'lat': float(y),
            'lon': float(x),
            'control': control,
            'degree': degree
        })
    return jsonify(out)

@app.route("/multipliers", methods=["GET"])
def multipliers():
    hour = int(request.args.get("hour", 12))
    v = float(demand_multipliers.get(hour, 1.0))
    s = float(speed_multipliers.get(hour, 1.0))
    return jsonify({"hour": hour, "volume_multiplier": v, "speed_multiplier": s})


# ============================
# Wait stats for UI
# ============================
@app.route("/wait-stats", methods=["GET"])
def wait_stats_endpoint():
    """Return aggregated wait stats + signal mode for the frontend dashboard."""
    from traffic_simulation import node_wait_stats, signal_timer
    import signal_model as sm

    stats = {}
    for node_id, s in node_wait_stats.items():
        avg = s["total"] / s["count"] if s["count"] else 0.0
        stats[str(node_id)] = {
            "avg": round(avg, 2),
            "max": round(s["max"], 2),
            "count": s["count"],
            "ns_split": round(sm.node_ns_split.get(node_id, 0.5), 3),
            "cycle": round(sm.node_cycle.get(node_id, sm.DEFAULT_CYCLE), 1),
        }

    overall_total = sum(s["total"] for s in node_wait_stats.values())
    overall_count = sum(s["count"] for s in node_wait_stats.values())
    overall_avg   = overall_total / overall_count if overall_count else 0.0
    overall_max   = max((s["max"] for s in node_wait_stats.values()), default=0.0)

    return jsonify({
        "signal_mode":  sm.SIGNAL_MODE,
        "sim_time":     round(signal_timer, 1),
        "rl_updates":   len(sm.update_log),
        "city_avg_wait": round(overall_avg, 2),
        "city_max_wait": round(overall_max, 2),
        "city_total_vehicles": overall_count,
        "nodes": stats,
    })


# ============================
# Signal mode toggle
# ============================
@app.route("/set-signal-mode", methods=["POST"])
def set_signal_mode():
    """Switch signal mode between 'fixed' and 'pretrained'."""
    import signal_model as sm

    data = request.get_json() or {}
    mode = data.get("mode", "")

    if mode not in ("fixed", "pretrained", "adaptive"):
        return jsonify({"error": f"Invalid mode '{mode}'"}), 400

    sm.SIGNAL_MODE = mode

    if mode == "pretrained":
        loaded = sm.load_model()
        if not loaded:
            sm.SIGNAL_MODE = "adaptive"
            return jsonify({"signal_mode": "adaptive", "warning": "No saved model found, switched to adaptive"})

    if mode == "fixed":
        sm.node_ns_split.clear()
        sm.node_cycle.clear()

    # Clear accumulated wait stats from the previous model
    from traffic_simulation import node_wait_stats
    node_wait_stats.clear()

    return jsonify({"signal_mode": sm.SIGNAL_MODE})


# ============================
# Run Server
# ============================
if __name__ == '__main__':
    app.run(debug=True)
