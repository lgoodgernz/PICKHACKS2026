# Adaptive Grid 
### PickHacks 2026 Project

RollaFlow is a smart infrastructure tool designed to alleviate traffic congestion in Rolla, MO. By utilizing a machine-learned signal model and real-time simulation, the project visualizes traffic heatmaps and optimizes North-South / East-West signal timings to improve commuter wait times.

---

## 🚀 Features

### 🔥 Congestion Heatmap
A smooth-scaling visualization of traffic density across the Rolla road network.

### 🤖 Machine Learning Backend
A trained model (`signal_model.json`) predicts optimal signal states based on stop sign counts and intersection data.

### 🖥️ Interactive Frontend
A clean web UI for visualizing:
- Traffic flow
- Network statistics
- Signal behavior

### 🗺️ Network Analysis
Processes `.graphml` files to identify:
- Stop signs
- Intersection density
- Road features

---

## 📂 Project Structure

```
PICKHACKS2026/
├── Backend/                  # Python Flask/Logic server
│   ├── app.py                # Main API entry point
│   ├── signal_model.py       # ML model architecture
│   ├── train_signals.py      # Model training script
│   └── traffic_simulation.py # Traffic flow simulation logic
│
├── Frontend/                 # Web-based visualization
│   ├── index.html            # Main dashboard
│   ├── script.js             # Map logic & API calls
│   └── style.css             # Custom UI styling
│
├── Data/                     # Processed datasets & GeoJSON
│
└── requirements.txt          # Python dependencies
```

---

## 🛠️ Installation & Setup

### 1️⃣ Prerequisites

- Python 3.x
- Recommended: Virtual environment (`venv`)

---

### 2️⃣ Install Dependencies

From the project root directory:

```bash
pip install -r requirements.txt
```

---

### 3️⃣ Start the Backend

```bash
cd Backend
python app.py
```

---

### 4️⃣ Start the Frontend

In a new terminal window:

```bash
cd Frontend
python -m http.server 8080
```

---

### 5️⃣ Access the Application

Open your browser and navigate to:

```
http://localhost:8080
```

---

## 📊 Data Insights

The project uses:

- `processed_rolla_network.graphml`  
  → Parses local infrastructure and intersection metadata  

- `RollaReport.csv`  
  → Used to train the signal optimization model  

- `wait_stats.json`  
  → Demonstrates measurable reduction in idle time after optimization  

Our trained model correlates traffic volume and intersection features with optimal signal intervals, resulting in reduced commuter wait times and improved traffic flow.

---

## 🎯 Project Goal

RollaFlow demonstrates how machine-learned signal optimization can:

- Reduce congestion
- Improve commute efficiency
- Provide scalable infrastructure insights
- Serve as a foundation for smart city traffic systems

---

## 📌 Future Improvements

- Real-time API traffic integration (TomTom / Google / DOT feeds)
- Reinforcement learning for adaptive signals
- Multi-city scaling architecture
- Cloud deployment with persistent simulation state

---

## 🏆 Built For

PickHacks 2026  
Missouri University of Science & Technology

---

## 📄 License

This project was developed for educational and hackathon purposes.
