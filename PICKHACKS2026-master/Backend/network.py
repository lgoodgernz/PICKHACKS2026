import json
from shapely.geometry import LineString, shape
import osmnx as ox
import networkx as nx

def save_rolla_boundary():
    # Get the exact boundary OSMnx uses for Rolla
    boundary = ox.geocode_to_gdf("Rolla, Missouri, USA")
    boundary.to_file("rolla_boundary.geojson", driver="GeoJSON")
    print("Saved rolla_boundary.geojson")


def load_network():
    G = ox.graph_from_place("Rolla, Missouri, USA", network_type="drive")
    
    # Detect and classify intersections
    # ============================================
    for node, data in G.nodes(data=True):

        # 1️⃣ Signalized intersections (from OSM)
        if data.get("highway") == "traffic_signals":
            data["control"] = "signal"
            continue

        # 2️⃣ Degree of node (how many roads meet)
        degree = G.degree(node)

        if degree < 3:
            data["control"] = "none"   # not really an intersection
            continue

        # 3️⃣ Look at connected road types
        connected_highways = set()

        for u, v, key, edge_data in G.edges(node, keys=True, data=True):
            highway = edge_data.get("highway")

            if isinstance(highway, list):
                highway = highway[0]

            connected_highways.add(highway)

        # 4️⃣ Classification:
        #    1) If OSM explicitly tags stop/yield at this node -> priority
        #    2) Else, tightened heuristic: major-road meets minor-road -> priority
        #    3) Else -> none

        major_roads = {"primary", "secondary", "trunk"}
        minor_roads = {"residential", "tertiary", "unclassified", "service", "living_street"}

        # --- Explicit stop/yield tags (if present in your OSM data) ---
        node_highway = data.get("highway")        # sometimes 'stop' or 'give_way' on nodes
        traffic_sign = data.get("traffic_sign")  # sometimes 'stop' / 'yield'

        explicit_stop = (node_highway == "stop") or (traffic_sign == "stop")
        explicit_yield = (node_highway in {"give_way", "yield"}) or (traffic_sign == "yield")

        if explicit_stop or explicit_yield:
            data["control"] = "priority"
        else:
            has_major = len(connected_highways & major_roads) > 0
            has_minor = len(connected_highways & minor_roads) > 0

            # Tightened heuristic: major meets minor
            if has_major and has_minor:
                data["control"] = "priority"
            else:
                data["control"] = "none"
    # Add speed and travel time data from OSMnx
    G = ox.add_edge_speeds(G)
    G = ox.add_edge_travel_times(G)

    # Calculate and store road score on every edge
    geojson_file = '../jobs_8773253_results_Rolla_Full.geojson'
    G = apply_traffic_data(G, geojson_file)

    signal_count = sum(1 for _, d in G.nodes(data=True) if d.get("control") == "signal")
    priority_count = sum(1 for _, d in G.nodes(data=True) if d.get("control") == "priority")
    none_count = sum(1 for _, d in G.nodes(data=True) if d.get("control") == "none")

    print("Signals:", signal_count)
    print("Priority:", priority_count)
    print("Free-flow:", none_count)
    
    return G


def network_to_json(G):
    edges = []
    for u, v, data in G.edges(data=True):
        u_data = G.nodes[u]
        v_data = G.nodes[v]

        # If the edge has geometry (curved road), use it
        if 'geometry' in data:
            coords = [{'lat': lat, 'lon': lon} for lon, lat in data['geometry'].coords]
        else:
            # Straight road, just use the two endpoints
            coords = [
                {'lat': u_data['y'], 'lon': u_data['x']},
                {'lat': v_data['y'], 'lon': v_data['x']}
            ]

        # Safely extract lanes
        lanes = data.get('lanes', 1)
        if isinstance(lanes, list):
            lanes = int(lanes[0])
        else:
            lanes = int(lanes)

        # Safely extract highway type
        highway = data.get('highway', 'unknown')
        if isinstance(highway, list):
            highway = highway[0]

        edges.append({
            'coords': coords,
            'id': f"{u}-{v}",
            'lanes': lanes,
            'name': data.get('name', 'Unknown'),
            'highway': highway,
            'speed_kph': data.get('speed_kph', 30),
            'length': data.get('length', 0),
            'oneway': data.get('oneway', False),
            'road_score': data.get('road_score', 1.0)
        })

    return {'edges': edges}


def add_road(G, data):
    u = data['start_node']
    v = data['end_node']
    G.add_edge(u, v)
    return G


def remove_road(G, data):
    u = data['start_node']
    v = data['end_node']
    if G.has_edge(u, v):
        G.remove_edge(u, v)
    return G

def apply_traffic_data(G, geojson_path):
    import numpy as np
    from shapely.geometry import shape

    with open(geojson_path, 'r') as f:
        traffic_data = json.load(f)

    features = traffic_data['features']
    print(f"Matching {len(features)} traffic segments...")

    mid_x = []
    mid_y = []
    valid_features = []

    # ================================
    # 1️⃣ Collect midpoints first
    # ================================
    for feature in features:
        if feature['geometry'] is None:
            continue

        try:
            geom = shape(feature['geometry'])
            midpoint = geom.interpolate(0.5, normalized=True)

            mid_x.append(midpoint.x)
            mid_y.append(midpoint.y)
            valid_features.append(feature)
        except Exception:
            continue

    if not mid_x:
        print("No valid traffic geometries found.")
        return G

    # ================================
    # 2️⃣ Batch nearest edge lookup
    # ================================
    nearest_edges = ox.nearest_edges(
        G,
        X=np.array(mid_x),
        Y=np.array(mid_y)
    )

    # ================================
    # 3️⃣ Assign traffic data
    # ================================
    for i, feature in enumerate(valid_features):
        props = feature.get('properties', {})
        results = props.get('segmentTimeResults', [{}])[0]

        try:
            u, v, key = nearest_edges[i]

            G[u][v][key]['traffic_speed'] = results.get('harmonicAverageSpeed')
            G[u][v][key]['traffic_volume'] = results.get('normalizedSampleSize', 0)
            G[u][v][key]['traffic_speed_std'] = results.get('standardDeviationSpeed', 5.0)
        except Exception:
            continue

    print("Full traffic data loaded successfully!")
    return G
