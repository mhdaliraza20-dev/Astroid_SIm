import datetime
import math

import folium
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from streamlit_folium import st_folium

TNT_J_PER_MT = 4.184e15
HIROSHIMA_MT = 0.015
TSAR_BOMBA_MT = 50  


K_TOTAL, K_HEAVY, K_LIGHT = 2.5, 6.0, 15.0

DEATH_RATE = {"Total destruction": 0.90, "Heavy damage": 0.50, "Light damage": 0.05}

SHIP_MASS = 570         
SHIP_SPEED = 6100     
BETA = 3.6              
EARTH_RADIUS = 6.4e6     
SECONDS_PER_YEAR = 3.156e7

G = 6.674e-11            
TRACTOR_MASS = 20_000   
NUKE_MT = 1.0            
COUPLING = 0.01          
EJECTA_SPEED = 1000      

SOUND_SPEED_KMS = 0.343  

CITIES = {
    "New Delhi": (28.6139, 77.2090, 16_800_000, 11_300),
    "Mumbai": (19.0760, 72.8777, 12_500_000, 20_700),
    "New York": (40.7128, -74.0060, 8_300_000, 11_000),
    "London": (51.5074, -0.1278, 9_000_000, 5_700),
    "Tokyo": (35.6762, 139.6503, 9_700_000, 15_600),
    "Paris": (48.8566, 2.3522, 2_100_000, 20_600),
    "Sydney": (-33.8688, 151.2093, 5_300_000, 2_000),
}

PRESETS = {
    "Custom": (100, 20, 45, 3000),
    "Chelyabinsk 2013 (small)": (20, 19, 18, 3300),
    "Tunguska 1908 (medium)": (50, 15, 45, 2500),
    "Apophis (hypothetical hit)": (340, 12.6, 45, 3200),
    "Bennu (hypothetical hit)": (490, 12.7, 45, 1190),
    "Dinosaur killer (huge)": (10000, 20, 60, 3000),
}


def simulate(diameter_m, speed_kms, angle_deg, density):
    mass = density * (4 / 3) * math.pi * (diameter_m / 2) ** 3
    v = speed_kms * 1000
    energy = 0.5 * mass * v**2 * math.sin(math.radians(angle_deg))
    megatons = energy / TNT_J_PER_MT
    cube_root = megatons ** (1 / 3)
    radii = {
        "Total destruction": K_TOTAL * cube_root,
        "Heavy damage": K_HEAVY * cube_root,
        "Light damage": K_LIGHT * cube_root,
    }
    airburst = diameter_m < 60 and density < 4000   
    crater_km = 0.0
    if not airburst:
        crater_m = (1.161 * (density / 2500) ** (1 / 3) * diameter_m**0.78
                    * v**0.44 * 9.81**-0.22
                    * math.sin(math.radians(angle_deg)) ** (1 / 3))
        crater_km = crater_m * 1.25 / 1000
    return mass, energy, megatons, radii, crater_km, airburst


def people_within(r_km, pop, density):
    """People inside radius r, assuming the city is a uniform disc."""
    city_radius = math.sqrt(pop / density / math.pi)
    return pop * min(1.0, (r_km / city_radius) ** 2)

def casualties(radii, pop, dens):
    """People in each ring (no double counting) and estimated deaths."""
    total_in = people_within(radii["Total destruction"], pop, dens)
    heavy_in = people_within(radii["Heavy damage"], pop, dens) - total_in
    light_in = people_within(radii["Light damage"], pop, dens) - total_in - heavy_in
    people = {"Total destruction": total_in, "Heavy damage": heavy_in, "Light damage": light_in}
    deaths = sum(people[z] * DEATH_RATE[z] for z in people)
    return people, deaths


def deflection(mass, years, ships):
    """Can `ships` kinetic impactors move the asteroid enough in `years`?"""
    dv_per_ship = BETA * SHIP_MASS * SHIP_SPEED / mass          # m/s
    dv_needed = EARTH_RADIUS / (years * SECONDS_PER_YEAR) if years > 0 else float("inf")
    ships_needed = math.ceil(dv_needed / dv_per_ship) if years > 0 else None
    return dv_per_ship * ships, dv_needed, ships_needed


def compare_methods(mass, diameter_m, years):
    """Table comparing three ways to deflect the asteroid."""
    if years <= 0:
        return None
    need = EARTH_RADIUS / (years * SECONDS_PER_YEAR)             
    methods = [
        ("Kinetic impactor (DART-style)", BETA * SHIP_MASS * SHIP_SPEED / mass, 200, "spacecraft"),
        ("Gravity tractor", G * TRACTOR_MASS / diameter_m**2 * years * SECONDS_PER_YEAR, 3, "tractor(s)"),
        ("Nuclear standoff blast (1 Mt)",
         2 * COUPLING * NUKE_MT * TNT_J_PER_MT / (mass * EJECTA_SPEED), 10, "device(s)"),
    ]
    rows = []
    for name, dv, limit, unit in methods:
        n = math.ceil(need / dv)
        rows.append({"Method": name, "Push per unit (m/s)": f"{dv:.2e}",
                     "Units needed": f"{n:,} {unit}",
                     "Realistic?": "Yes" if n <= limit else f"No (limit about {limit})"})
    return pd.DataFrame(rows)


def seismic_magnitude(energy_j):
    return 0.67 * math.log10(energy_j) - 5.87

def impact_interval_text(megatons):
    
    if megatons >= 1e6:
        return "about once per 100 million years or rarer"
    per_year = 3.7 * (megatons * 1000) ** -0.9         
    years = 1 / per_year
    if years < 1:
        return "more than once a year"
    return f"about once every {years:,.0f} years"

def fetch_nasa_asteroids():
    today = datetime.date.today().isoformat()
    r = requests.get("https://api.nasa.gov/neo/rest/v1/feed",
                    params={"start_date": today, "end_date": today, "api_key": "DEMO_KEY"},
                    timeout=10)
    r.raise_for_status()
    found = []
    for day in r.json()["near_earth_objects"].values():
        for neo in day:
            m = neo["estimated_diameter"]["meters"]
            diameter = round((m["estimated_diameter_min"] + ["estimated_diameter_max"]) / 2)
            v_inf = float(neo["close_approach_data"][0]["relative_velocity"]["kilometers_per_second"])
            v_hit = min(72.0, round(math.sqrt(v_inf**2 + 11.2**2), 1))   
            found.append((diameter, f"NASA: {neo['name']}", (max(diameter, 1),v_hit, 45, 3000)))
    found.sort(reverse=True)                                             
    return {name: values for _, name, values in found[:15]}


st.set_page_config(page_title="Asteroid Impact Simulator", layout="wide")
st.title("Asteroid Impact Simulator")
st.caption("What if it hit YOUR city? Pick an asteroid, pick a place, see the damage and whether we could stop it.")

st.sidebar.header("Asteroid")
if st.sidebar.button("Load today's real asteroids (NASA)"):
    try:
        st.session_state["nasa"] = fetch_nasa_asteroids()
        st.sidebar.success(f"Loaded {len(st.session_state['nasa'])} real asteroids. See the preset list.")
    except Exception:
        st.sidebar.error("Could not reach NASA. This needs internet (the free key has a rate limit).")
presets = {**PRESETS, **st.session_state.get("nasa", {})}

preset = st.sidebar.selectbox("Preset scenario", list(presets))
d0, v0, a0, rho0 = presets[preset]
diameter = st.sidebar.number_input("Diameter (m)", 1, 20000, int(d0), key=f"d_{preset}")
speed = st.sidebar.slider("Speed (km/s)", 11.0, 72.0, float(v0), key=f"v_{preset}")
angle = st.sidebar.slider("Impact angle (deg)", 5, 90, int(a0), key=f"a_{preset}")
density = st.sidebar.number_input("Density (kg/m3)", 500, 8000, int(rho0), key=f"rho_{preset}")

st.sidebar.header("Location")
use_custom = st.sidebar.checkbox("Use my own location (latitude / longitude)")
if use_custom:
    lat = st.sidebar.number_input("Latitude", -90.0, 90.0, 28.6139, format="%.4f")
    lon = st.sidebar.number_input("Longitude", -180.0, 180.0, 77.2090, format="%.4f")
    pop = int(st.sidebar.number_input("Population of the area", 1_000, 50_000_000, 1_000_000, step=100_000))
    dens = int(st.sidebar.number_input("People per km2", 10, 50_000, 5_000, step=100))
    city = "Custom location"
else:
    city = st.sidebar.selectbox("Target city", list(CITIES))
    lat, lon, pop, dens = CITIES[city]

st.sidebar.header("Defense")
defend = st.sidebar.checkbox("Defense mode: apply my spacecraft to the map")
years = st.sidebar.slider("Years of warning", 0.0, 20.0, 5.0, 0.5)
ships = st.sidebar.slider("Impactor spacecraft launched", 0, 200, 10)

mass, energy, megatons, radii, crater_km, airburst = simulate(diameter, speed, angle, density)
people, deaths = casualties(radii, pop, dens)
dv_fleet, dv_needed, ships_needed = deflection(mass, years, ships)
saved = defend and dv_fleet >= dv_needed

magnitude = seismic_magnitude(energy)
blast_minutes = radii["Light damage"] / SOUND_SPEED_KMS / 60
interval = impact_interval_text(megatons)

if saved:
    st.success(f"DEFLECTED. {ships} spacecraft and {years} years of warning saved {city}.")
else:
    st.error(
        f"A {diameter:,.0f} m asteroid hits {city}: about {deaths:,.0f} people would die "
        f"and {sum(people.values()):,.0f} would be in the damage zones."
    )

tab_map, tab_defense, tab_facts, tab_rank, tab_valid = st.tabs(
    ["Impact map", "Can we stop it?", "More impact facts", "City ranking and report", "Validation"]
)

with tab_map:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Energy released", f"{megatons:,.1f} Mt TNT")
    c2.metric("Hiroshima bombs", f"{megatons / HIROSHIMA_MT:,.0f}")
    c3.metric("Tsar Bombas (50 Mt)", f"{megatons / TSAR_BOMBA_MT:,.1f}")
    c4.metric("Crater", "Airburst, no crater" if airburst else f"{crater_km:,.2f} km wide")

    zoom = 10 if saved else max(1, min(12, int(math.log2(20000 / radii["Light damage"]))))
    m = folium.Map(location=[lat, lon], zoom_start=zoom)
    if saved:
        folium.Circle([lat, lon], radius=5000, color="green", fill=True,
                      fill_opacity=0.3, tooltip=f"{city} is safe").add_to(m)
    else:
        colors = {"Light damage": "yellow", "Heavy damage": "orange", "Total destruction": "red"}
        for name in ["Light damage", "Heavy damage", "Total destruction"]:   
            folium.Circle(
                [lat, lon], radius=radii[name] * 1000, color=colors[name],
                fill=True, fill_opacity=0.35,
                tooltip=f"{name}: {radii[name]:,.1f} km, ~{people[name]:,.0f} people",
            ).add_to(m)
    folium.Marker([lat, lon], tooltip=f"Target: {city}").add_to(m)
    st_folium(m, height=560, use_container_width=True, returned_objects=[])
    st.caption("Zoom with the + / - buttons or the mouse wheel, and drag to move the map.")

    if not saved:
        st.subheader("Who is in each zone?")
        for name in ["Total destruction", "Heavy damage", "Light damage"]:
            st.write(f"**{name}**: radius {radii[name]:,.1f} km, about {people[name]:,.0f} people")

with tab_defense:
    st.subheader("Can we stop it?")
    st.caption("Tick 'Defense mode' in the sidebar to apply your spacecraft to the map. "
               "The numbers below always use the sidebar sliders.")
    d1, d2, d3 = st.columns(3)
    d1.metric("Push needed", "No time left" if years <= 0 else f"{dv_needed:.2e} m/s")
    d2.metric("Push from your fleet", f"{dv_fleet:.2e} m/s")
    d3.metric("Spacecraft needed", "No time left" if ships_needed is None else f"{ships_needed:,.0f}")
    if years > 0 and dv_fleet < dv_needed:
        st.warning(f"Your {ships} spacecraft are not enough. You need about {ships_needed:,.0f} "
                   f"DART-class spacecraft, or more years of warning.")
    elif years > 0:
        st.success("Your fleet is big enough: the asteroid would miss Earth.")

    st.subheader("Why early warning matters")
    dv_ship = BETA * SHIP_MASS * SHIP_SPEED / mass
    xs = [0.5 + 0.5 * i for i in range(40)]                     
    ys = [max(1, math.ceil(EARTH_RADIUS / (x * SECONDS_PER_YEAR) / dv_ship)) for x in xs]
    wfig = go.Figure()
    wfig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", name="Spacecraft needed",
                              line=dict(color="crimson", width=3)))
    if years > 0:
        wfig.add_trace(go.Scatter(
            x=[years], y=[max(1, ships_needed)], mode="markers", name="Your warning time",
            marker=dict(size=12, color="black")))
    wfig.add_hline(y=200, line_dash="dash", annotation_text="200 spacecraft (slider limit)")
    wfig.update_yaxes(type="log", title="Spacecraft needed (log scale)")
    wfig.update_xaxes(title="Years of warning")
    wfig.update_layout(height=380, margin=dict(l=10, r=10, t=10, b=10))
    st.plotly_chart(wfig, use_container_width=True)
    st.caption("More warning means far fewer spacecraft. Below the dashed line, today's technology could do it.")

    st.subheader("Compare deflection methods")
    table = compare_methods(mass, diameter, years)
    if table is None:
        st.info("Set at least 0.5 years of warning to compare methods.")
    else:
        st.dataframe(table, use_container_width=True, hide_index=True)
        st.caption("Rough assumptions: gravity tractor = 20,000 kg hovering one diameter away for the whole warning time; "
                   "nuclear = 1 Mt device, 1% of its energy pushes rock off the surface at 1 km/s.")

with tab_facts:
    st.subheader("More impact facts")
    f1, f2, f3 = st.columns(3)
    f1.metric("Earthquake magnitude", f"{magnitude:.1f}")
    f2.metric("Blast wave reaches the edge of light damage", f"{blast_minutes:,.1f} min")
    f3.metric("How often this size hits Earth", interval)
    st.write(
        f"- **Earthquake:** about 0.01% of the impact energy turns into seismic waves, "
        f"which gives a magnitude of roughly {magnitude:.1f}.\n"
        f"- **Warning time on the ground:** the blast wave travels at the speed of sound "
        f"(about 0.343 km/s), so it needs about {blast_minutes:,.1f} minutes to reach "
        f"{radii['Light damage']:,.1f} km from the impact.\n"
        f"- **Frequency:** impacts of this energy happen {interval}, based on a fit to "
        f"real fireball data (Brown et al., 2002)."
    )

with tab_rank:
    st.subheader("Which city would suffer most?")
    rows = []
    for cname, (clat, clon, cpop, cdens) in CITIES.items():
        cpeople, cdeaths = casualties(radii, cpop, cdens)
        rows.append({"City": cname, "Population": cpop,
                     "People in damage zones": round(sum(cpeople.values())),
                     "Estimated deaths": round(cdeaths)})
    ranking = pd.DataFrame(rows).sort_values("Estimated deaths", ascending=False)
    st.dataframe(ranking, use_container_width=True, hide_index=True)
    st.bar_chart(ranking.set_index("City")["Estimated deaths"])
    st.caption("Same asteroid dropped on every city. Cities are compared as if each were hit at its centre.")

    st.subheader("Download impact report")
    report = f"""ASTEROID IMPACT REPORT
======================
Target: {city} (lat {lat:.4f}, lon {lon:.4f})

ASTEROID
  Diameter: {diameter:,.0f} m
  Speed: {speed} km/s
  Angle: {angle} degrees
  Density: {density:,.0f} kg/m3
  Mass: {mass:.2e} kg

IMPACT
  Energy: {energy:.2e} J = {megatons:,.2f} megatons of TNT
  Equal to {megatons / HIROSHIMA_MT:,.0f} Hiroshima bombs / {megatons / TSAR_BOMBA_MT:,.1f} Tsar Bombas
  Crater: {"airburst, no crater" if airburst else f"{crater_km:,.2f} km wide"}
  Earthquake magnitude: about {magnitude:.1f}
  Frequency of this size: {interval}

DAMAGE ZONES
  Total destruction: {radii['Total destruction']:,.1f} km, about {people['Total destruction']:,.0f} people
  Heavy damage:      {radii['Heavy damage']:,.1f} km, about {people['Heavy damage']:,.0f} people
  Light damage:      {radii['Light damage']:,.1f} km, about {people['Light damage']:,.0f} people
  Estimated deaths:  {deaths:,.0f}

DEFENSE ({years} years of warning, {ships} spacecraft)
  Push needed: {"no time" if years <= 0 else f"{dv_needed:.2e} m/s"}
  Push from fleet: {dv_fleet:.2e} m/s
  Spacecraft needed: {"no time" if ships_needed is None else f"{ships_needed:,.0f}"}
  Result: {"asteroid deflected" if dv_fleet >= dv_needed else "asteroid hits"}

NOTE: Rough educational approximations. Fatality rates (90/50/5 %) and a uniform
population are assumptions. Compare with Purdue's Impact Earth calculator.
"""
    st.download_button("Download report (.txt)", report, file_name="asteroid_impact_report.txt",
                       mime="text/plain")
    with st.expander("Preview report"):
        st.text(report)

with tab_valid:
    st.subheader("Check our numbers")
    st.write("Our results for three well-known events, next to published estimates. "
             "Enter the same inputs into Purdue's Impact Earth to compare. "
             "Differences are expected because both are simplified models.")
    published = {"Chelyabinsk 2013 (small)": "about 0.4-0.5 Mt (NASA)",
                 "Tunguska 1908 (medium)": "about 3-15 Mt (estimates vary)",
                 "Dinosaur killer (huge)": "about 100,000,000 Mt (Chicxulub)"}
    vrows = []
    for pname, pub in published.items():
        pd0, pv0, pa0, prho0 = PRESETS[pname]
        _, penergy, pmt, pradii, pcrater, pair = simulate(pd0, pv0, pa0, prho0)
        vrows.append({"Scenario": pname, "Our energy (Mt)": f"{pmt:,.2f}",
                      "Published estimate": pub,
                      "Our crater (km)": "airburst" if pair else f"{pcrater:,.1f}",
                      "Our light-damage radius (km)": f"{pradii['Light damage']:,.1f}"})
    st.dataframe(pd.DataFrame(vrows), use_container_width=True, hide_index=True)

st.caption(
    "Rough approximations for education. Zones use blast scaling from nuclear data; "
    "population assumes a uniform city; fatality rates are assumed (90% / 50% / 5%); "
    "deflection methods use simple physics assumptions. Compare with Purdue's Impact Earth."
)
