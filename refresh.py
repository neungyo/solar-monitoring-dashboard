"""
Refresh script for the Synnex Thailand solar monitoring dashboard.

Runs inside GitHub Actions on a schedule. Logs into Huawei FusionSolar
Northbound API, pulls fresh data (respecting Huawei's per-interface rate
limits by caching the slow-changing endpoints), rebuilds a PII-stripped
index.html, and leaves it in the repo root for GitHub Pages to serve.

Env vars required: FUSIONSOLAR_USER, FUSIONSOLAR_PASS
Optional: FUSIONSOLAR_DOMAIN (default sg5.fusionsolar.huawei.com)
"""
import os, sys, json, re, socket, time, urllib.request, urllib.error, http.cookiejar, datetime, statistics, copy

# Force IPv4 resolution: some CI runners fail to resolve certain hosts over
# IPv6 (getaddrinfo raises "No address associated with hostname"), even
# though plain IPv4 resolution works fine.
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = _ipv4_getaddrinfo

DOMAINS = [os.environ.get("FUSIONSOLAR_DOMAIN", "sg5.fusionsolar.huawei.com"), "intl.fusionsolar.huawei.com"]
USER = os.environ["FUSIONSOLAR_USER"]
PASSWORD = os.environ["FUSIONSOLAR_PASS"]

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
_active_domain = [DOMAINS[0]]

def _post_once(domain, path, body):
    req = urllib.request.Request(
        f"https://{domain}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    xsrf = None
    for c in cj:
        if c.name == "XSRF-TOKEN":
            xsrf = c.value
    if xsrf:
        req.add_header("XSRF-TOKEN", xsrf)
    with opener.open(req, timeout=30) as resp:
        return json.loads(resp.read())

def post(path, body):
    last_err = None
    for domain in [_active_domain[0]] + [d for d in DOMAINS if d != _active_domain[0]]:
        for attempt in range(2):
            try:
                data = _post_once(domain, path, body)
                _active_domain[0] = domain
                if not data.get("success"):
                    raise RuntimeError(f"{path} failed: {data.get('failCode')} {data.get('message')}")
                return data
            except (urllib.error.URLError, socket.error, TimeoutError) as e:
                last_err = e
                time.sleep(2)
    raise RuntimeError(f"{path} failed against all domains: {last_err}")

def login():
    post("/thirdData/login", {"userName": USER, "systemCode": PASSWORD})

def fetch_weather(lat, lon):
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
           "&daily=weathercode,precipitation_sum,sunshine_duration"
           "&past_days=10&forecast_days=1&timezone=Asia%2FBangkok")
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read())["daily"]

def wcode_symbol(code):
    if code is None: return ("?", "ไม่มีข้อมูล")
    if code == 0: return ("sun", "แดดจัด ฟ้าโปร่ง")
    if code == 1: return ("sun-cloud", "แดดเป็นส่วนใหญ่")
    if code == 2: return ("cloud-sun", "มีเมฆบางส่วน")
    if code == 3: return ("cloud", "เมฆมาก/ครึ้ม")
    if code in (45, 48): return ("fog", "หมอก")
    if code in (51, 53, 55, 56, 57): return ("drizzle", "ฝนปรอยๆ")
    if code in (61, 63, 65, 66, 67, 80, 81, 82): return ("rain", "ฝนตก")
    if code in (95, 96, 99): return ("storm", "ฝนฟ้าคะนอง")
    return ("cloud", "มีเมฆ")

def parse_kw_from_model(model):
    if not model: return None
    m = re.search(r"(\d+(?:\.\d+)?)K", model.upper())
    return float(m.group(1)) if m else None

def mask_name(raw):
    if not raw:
        return "Unknown xxxxxx"
    s = raw.strip()
    s = re.sub(r"[_\s]*(TS|SYN)[-]?\d[\d\-]*\s*$", "", s).strip()
    s = s.rstrip("_ ").strip()
    tokens = s.split()
    if not tokens:
        return "Unknown xxxxxx"
    visible = tokens[0] + " " + tokens[1] if tokens[0] == "คุณ" and len(tokens) > 1 else tokens[0]
    return f"{visible} xxxxxx"

def load_json(path, default=None):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default

def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)

def stale(path, seconds):
    ts = load_json(path)
    if ts is None:
        return True
    last = datetime.datetime.fromisoformat(ts)
    return (datetime.datetime.utcnow() - last).total_seconds() > seconds

def touch(path):
    save_json(path, datetime.datetime.utcnow().isoformat())

LIVE_PAGE_URL = "https://neungyo.github.io/solar-monitoring-dashboard/"

# How long the historical-day + weather block (getKpiStationDay/getDevKpiDay,
# the daily-quota endpoints) can go before it's considered stale enough to
# refetch. Per Huawei's official SmartPVMS 25.1.0 NBI Reference (section 4.2
# Flow Control Using the API Account), getDevKpiDay's real budget is
# ∑Roundup(devices per type/100) + 24 per day — for our 33 type-38 +
# 4 type-1 inverters that's 1+1+24 = 26 calls/day. We call it twice per
# refresh (once per device type), so refreshing hourly (24x/day) would burn
# 48 calls/day — nearly double the budget, and would start failing partway
# through the day. At this threshold it refreshes at most 6x/day = 12 calls,
# comfortably under budget even with a couple of extra manual triggers.
HIST_STALE_SECONDS = 4 * 3600

def fetch_live_state():
    """Fetch and parse the currently-deployed page's embedded DATA blob once.
    Returns {} if unavailable (first run ever, or the fetch fails).

    IMPORTANT CONTEXT: this script runs inside a fresh, throwaway sandbox
    every scheduled run (a Cowork scheduled task, not GitHub Actions — that
    path is blocked by Huawei's geo-restricted DNS, see .github/workflows,
    disabled). Only index.html gets uploaded back to the repo each run, so
    anything written to a local data/*.json file during the run does NOT
    survive to the next run — the live page itself is the only thing that
    reliably round-trips through GitHub every run. This single fetch backs
    two independent features:
      1. Seeding today's intraday PV1/PV2 curve (see pv_today_from_state).
      2. Letting the historical-day + weather block below skip re-querying
         Huawei when it was refreshed recently, by reusing each plant's/
         inverter's already-embedded trend_10d and weather symbol instead of
         leaving them blank (see HIST_STALE_SECONDS and its usage in main()).
    """
    try:
        req = urllib.request.Request(LIVE_PAGE_URL, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        m = re.search(r"const DATA = (.*?);\s*\nconst ICONS", html, re.S)
        if not m:
            return {}
        return json.loads(m.group(1))
    except Exception as e:
        print(f"WARN: could not fetch live page state ({e}); starting fresh", file=sys.stderr)
        return {}

def soc_today_from_state(state, today_str):
    """Rebuild today's intraday battery SOC readings from an already-fetched
    live-page state dict (see fetch_live_state), same reasoning as
    pv_today_from_state: the Northbound API has no historical battery SOC
    endpoint (getDevRealKpi only gives the current instant), so an intraday
    curve has to be accumulated one point per refresh run and carried
    forward via the deployed page itself. Used to answer "did this site's
    battery reach full charge today, and if so at what time?" and "how many
    battery sites are not reaching full charge, and roughly why?".
    """
    result = {}
    for p in state.get("plants", []):
        code = p.get("plantCode")
        entries = ((p.get("equipment") or {}).get("battery_soc_trend_today")) or []
        todays = []
        for e in entries:
            if e.get("date") != today_str:
                continue
            soc = e.get("soc")
            t = e.get("time")
            if soc is None or t is None:
                continue
            todays.append({"time": t, "soc": soc})
        if todays:
            result[code] = todays
    return result

def pv_today_from_state(state, today_str):
    """Rebuild today's intraday PV1/PV2 voltage/current readings from an
    already-fetched live-page state dict (see fetch_live_state).

    This is intentionally single-day (not the old 5-day daily-peak design):
    per user request, PV1/PV2 tracking now shows an intraday curve across
    the day's ~8 sunlight hours (one point per run, e.g. hourly) rather
    than one peak-of-day point kept for 5 days — cheaper to reason about
    and the API quota concern doesn't apply here anyway (these voltage/
    current values come from getDevRealKpi, which is not the
    quota-limited endpoint; that's getKpiStationDay/getDevKpiDay). Any
    reading embedded under a date other than today_str is stale (from
    before local midnight) and is dropped.
    """
    result = {}
    for p in state.get("plants", []):
        code = p.get("plantCode")
        for inv in p.get("inverters", []):
            sn = inv.get("sn")
            for pv_name, entries in (inv.get("pv_trend_today") or {}).items():
                key = f"{code}|{sn}|{pv_name}"
                todays = []
                for e in entries:
                    if e.get("date") != today_str:
                        continue
                    v = e.get("voltage_v")
                    i = e.get("current_a")
                    t = e.get("time")
                    if v is None or i is None or t is None:
                        continue
                    todays.append({"time": t, "v": v, "i": i})
                if todays:
                    result[key] = todays
    return result

def main():
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
    today_str = now.strftime("%Y-%m-%d")
    now_hhmm = now.strftime("%H:%M")
    anomaly_cutoff_date = (now - datetime.timedelta(days=3)).strftime("%Y-%m-%d")

    # Single fetch of the live page's embedded state — backs both the PV1/PV2
    # intraday curve and the historical-day/weather reuse-if-fresh check
    # below (see fetch_live_state docstring).
    live_state = fetch_live_state()
    pv_today = pv_today_from_state(live_state, today_str)
    soc_today = soc_today_from_state(live_state, today_str)
    live_plants_by_code = {p["plantCode"]: p for p in live_state.get("plants", [])}

    hist_ts_str = live_state.get("historical_refreshed_at")
    hist_is_fresh = False
    if hist_ts_str:
        try:
            hist_ts = datetime.datetime.fromisoformat(hist_ts_str)
            hist_is_fresh = (now - hist_ts).total_seconds() < HIST_STALE_SECONDS
        except Exception:
            hist_is_fresh = False

    login()

    # ---- asset data: refresh at most every 12h (Huawei daily-quota endpoints) ----
    if stale("data/asset_refreshed_at.json", 12 * 3600) or not os.path.exists("data/stations.json"):
        stations = post("/thirdData/stations", {"pageNo": 1})["data"]["list"]
        codes = ",".join(s["plantCode"] for s in stations)
        devlist = post("/thirdData/getDevList", {"stationCodes": codes})["data"]
        save_json("data/stations.json", stations)
        save_json("data/devlist.json", devlist)
        touch("data/asset_refreshed_at.json")
    else:
        stations = load_json("data/stations.json")
        devlist = load_json("data/devlist.json")

    codes = ",".join(s["plantCode"] for s in stations)
    inv38 = [d["devDn"] for d in devlist if d["devTypeId"] == 38]
    inv1 = [d["devDn"] for d in devlist if d["devTypeId"] == 1]
    bat39 = [d["devDn"] for d in devlist if d["devTypeId"] == 39]

    # ---- realtime data: every run ----
    realkpi = post("/thirdData/getStationRealKpi", {"stationCodes": codes})["data"]
    devreal = []
    if inv38:
        devreal += post("/thirdData/getDevRealKpi", {"devIds": ",".join(inv38), "devTypeId": 38})["data"]
    if inv1:
        devreal += post("/thirdData/getDevRealKpi", {"devIds": ",".join(inv1), "devTypeId": 1})["data"]

    # battery real kpi: gives per-pack SN/SOH (battery_unit_info) and the
    # current state of charge (battery_soc) — the devDn/devlist entry for a
    # battery is just one logical "Battery-1" combiner even when 2+ physical
    # packs are wired to it, so pack count has to come from here, not from
    # counting devTypeId==39 rows.
    bat_real_by_devid = {}
    if bat39:
        batreal = post("/thirdData/getDevRealKpi", {"devIds": ",".join(bat39), "devTypeId": 39})["data"]
        bat_real_by_devid = {r["devId"]: r["dataItemMap"] for r in batreal}

    end = now
    start = end - datetime.timedelta(days=5)
    alarms = post("/thirdData/getAlarmList", {
        "stationCodes": codes,
        "beginTime": int(start.timestamp() * 1000),
        "endTime": int(end.timestamp() * 1000),
        "language": "en_US",
    })["data"]

    # getAlarmList's raw stationName is the FULL, unmasked customer name
    # (e.g. "คุณจงรักษ์ ขวัญชื่น_ TS260600104") — overwrite it with the
    # masked name before it ever gets embedded in the public index.html.
    # Without this, the raw name was being written straight into the
    # page's JSON (viewable via page source) even though the alarms table
    # only displayed stationCode — a real privacy leak, now closed.
    station_masked_by_code = {s["plantCode"]: mask_name(s["plantName"]) for s in stations}
    for a in alarms:
        a["stationName"] = station_masked_by_code.get(a.get("stationCode"), "Unknown xxxxxx")

    # ---- historical day + weather: refresh at most every HIST_STALE_SECONDS ----
    # (see HIST_STALE_SECONDS comment above for the exact quota math). When
    # not fresh enough to skip, we still can't reuse a local data/*.json
    # cache — this sandbox is thrown away every run — so we reuse the
    # already-embedded trend_10d/weather values from live_state instead
    # (per-plant and per-inverter, applied further down in the combine
    # step), rather than leaving the charts blank on skipped runs.
    if not hist_is_fresh:
        collect = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        kpiday = post("/thirdData/getKpiStationDay", {"stationCodes": codes, "collectTime": collect})["data"]
        devkpiday = []
        if inv38:
            devkpiday += post("/thirdData/getDevKpiDay", {"devIds": ",".join(inv38), "devTypeId": 38, "collectTime": collect})["data"]
        if inv1:
            devkpiday += post("/thirdData/getDevKpiDay", {"devIds": ",".join(inv1), "devTypeId": 1, "collectTime": collect})["data"]
        weather = {}
        for s in stations:
            try:
                weather[s["plantCode"]] = fetch_weather(s["latitude"], s["longitude"])
            except Exception as e:
                weather[s["plantCode"]] = {"error": str(e)}
        historical_refreshed_at = now.isoformat()
    else:
        kpiday, devkpiday, weather = [], [], {}
        historical_refreshed_at = hist_ts_str

    # ================= combine =================
    real_by_code = {r["stationCode"]: r["dataItemMap"] for r in realkpi}
    if hist_is_fresh:
        dates10 = live_state.get("dates10", [])
        last10 = []
    else:
        all_times = sorted(set(x["collectTime"] for x in kpiday))
        last10 = all_times[-10:]
        dates10 = [datetime.datetime.utcfromtimestamp(t / 1000).strftime("%Y-%m-%d") for t in last10]
    by_station_day = {}
    for x in kpiday:
        if x["collectTime"] in last10:
            by_station_day.setdefault(x["stationCode"], {})[x["collectTime"]] = x["dataItemMap"]

    devs_by_station = {}
    for d in devlist:
        devs_by_station.setdefault(d["stationCode"], []).append(d)

    inv_real_by_devid = {r["devId"]: r["dataItemMap"] for r in devreal}
    devday_by_devid = {}
    for x in devkpiday:
        devday_by_devid.setdefault(x["devId"], {})[x["collectTime"]] = x["dataItemMap"]
    dev_all_times = sorted(set(x["collectTime"] for x in devkpiday)) if devkpiday else []
    dev_last10 = dev_all_times[-10:]
    dev_dates10 = [datetime.datetime.utcfromtimestamp(t / 1000).strftime("%Y-%m-%d") for t in dev_last10]

    plants_out = []
    for s in stations:
        code = s["plantCode"]
        real = real_by_code.get(code, {})
        devs = devs_by_station.get(code, [])
        inverters = [d for d in devs if d["devTypeId"] in (1, 38)]
        optimizers = [d for d in devs if d["devTypeId"] == 46]
        batteries = [d for d in devs if d["devTypeId"] == 39]
        smartguards = [d for d in devs if d["devTypeId"] == 23071]
        meters = [d for d in devs if d["devTypeId"] in (47, 23076)]

        live_plant = live_plants_by_code.get(code, {})
        if hist_is_fresh:
            # Historical/weather block was skipped this run (see
            # HIST_STALE_SECONDS) — carry forward what was already embedded
            # last time rather than leaving the 10-day chart/weather blank.
            trend = live_plant.get("trend_10d", [])
            today_sym = live_plant.get("today_weather_symbol", "?")
            today_label = live_plant.get("today_weather_label", "ไม่มีข้อมูล")
        else:
            day_data = by_station_day.get(code, {})
            trend = []
            for t, dstr in zip(last10, dates10):
                item = day_data.get(t, {})
                wcode = None
                precip = None
                try:
                    idx = weather.get(code, {}).get("time", []).index(dstr)
                    wcode = weather[code]["weathercode"][idx]
                    precip = weather[code]["precipitation_sum"][idx]
                except (ValueError, KeyError):
                    pass
                sym, label = wcode_symbol(wcode)
                trend.append({"date": dstr, "power_kwh": item.get("inverter_power"),
                               "perf_ratio": item.get("perpower_ratio"), "weather_symbol": sym,
                               "weather_label": label, "precip_mm": precip})

            wdata = weather.get(code, {})
            today_wcode = wdata.get("weathercode", [None])[-1] if wdata.get("weathercode") else None
            today_sym, today_label = wcode_symbol(today_wcode)

        live_inv_by_sn = {i.get("sn"): i for i in live_plant.get("inverters", [])}

        inv_out = []
        for inv in inverters:
            devid = inv["id"]
            real_kpi = inv_real_by_devid.get(devid, {})
            strings = []
            for i in range(1, 5):
                u = real_kpi.get(f"pv{i}_u")
                ii = real_kpi.get(f"pv{i}_i")
                if u is not None:
                    strings.append({"string": f"PV{i}", "voltage_v": u, "current_a": ii})
            if hist_is_fresh:
                # carried forward from the last actual refresh, same reason
                # as the plant-level trend above
                inv_trend = live_inv_by_sn.get(inv.get("esnCode"), {}).get("trend_10d", [])
            else:
                dd = devday_by_devid.get(devid, {})
                inv_trend = []
                for t, dstr in zip(dev_last10, dev_dates10):
                    item = dd.get(t, {})
                    inv_trend.append({"date": dstr, "power_kwh": item.get("product_power"), "perf_ratio": item.get("perpower_ratio")})

            # PV1/PV2 intraday voltage/current tracking. Confirmed against
            # the live API: getDevKpiDay (the historical daily endpoint)
            # only returns product_power/perpower_ratio for inverters — no
            # string-level V/A history exists anywhere in the Northbound
            # API. So we build our own curve by recording each string's V/A
            # once per run (this script runs hourly via a Cowork scheduled
            # task, not GitHub Actions — blocked by Huawei's geo-DNS),
            # covering roughly the day's 8 sunlight hours, then resetting
            # at local midnight. Each run is a fresh throwaway sandbox, so
            # pv_today is seeded at the top of main() from the live page's
            # own embedded data (fetch_live_state / pv_today_from_state)
            # rather than a local file.
            sn = inv.get("esnCode")
            pv_trend_today = {}
            for i in (1, 2):
                u = real_kpi.get(f"pv{i}_u")
                ii = real_kpi.get(f"pv{i}_i")
                key = f"{code}|{sn}|PV{i}"
                if u is not None and ii is not None:
                    entries = pv_today.setdefault(key, [])
                    # collapse to at most one point per hour, in case this
                    # run happens to land in the same hour as the last one
                    if not entries or entries[-1]["time"][:2] != now_hhmm[:2]:
                        entries.append({"time": now_hhmm, "v": round(u, 1), "i": round(ii, 2)})
                entries = pv_today.get(key, [])
                pv_trend_today[f"PV{i}"] = [
                    {"date": today_str, "time": e["time"], "voltage_v": e["v"], "current_a": e["i"]}
                    for e in entries
                ]

            inv_out.append({
                "sn": sn, "model": inv.get("model"),
                "capacity_kw": parse_kw_from_model(inv.get("model")),
                "optimizer_count": inv.get("optimizerNumber"),
                "temperature_c": real_kpi.get("temperature"),
                "active_power_kw": real_kpi.get("active_power"),
                "day_energy_kwh": real_kpi.get("day_cap"),
                "strings": strings, "trend_10d": inv_trend,
                "pv_trend_today": pv_trend_today,
            })

        # Battery: devlist only ever lists one logical "Battery-1" combiner
        # device per station even when 2+ physical packs are wired to it, so
        # the real pack count/SOH lives in getDevRealKpi's battery_unit_info
        # (per-pack sn/soh, spread across unit1..unit4 sub-arrays with
        # trailing null slots for empty bays). battery_soc is the live state
        # of charge %.
        battery_pack_count = 0
        battery_soc_vals = []
        battery_model = None
        for b in batteries:
            bkpi = bat_real_by_devid.get(b["id"], {})
            unit_info = bkpi.get("battery_unit_info") or {}
            for arr in unit_info.values():
                for item in (arr or []):
                    if item and item.get("sn"):
                        battery_pack_count += 1
            soc = bkpi.get("battery_soc")
            if soc is not None:
                battery_soc_vals.append(soc)
            battery_model = b.get("model")
        if batteries and battery_pack_count == 0:
            battery_pack_count = len(batteries)  # fallback if unit_info wasn't reported
        battery_soc_pct = round(sum(battery_soc_vals) / len(battery_soc_vals), 1) if battery_soc_vals else None

        # Intraday SOC curve: one point per refresh run (collapsed to at most
        # one per hour), reseeded from the deployed page and reset at local
        # midnight — same mechanism as pv_trend_today above, since the
        # Northbound API has no historical battery SOC endpoint either. Lets
        # the dashboard answer "did the battery reach full charge today, and
        # when?" without needing to store anything outside the page itself.
        soc_trend_today = []
        if batteries and battery_soc_pct is not None:
            entries = soc_today.setdefault(code, [])
            if not entries or entries[-1]["time"][:2] != now_hhmm[:2]:
                entries.append({"time": now_hhmm, "soc": battery_soc_pct})
            soc_trend_today = [{"date": today_str, "time": e["time"], "soc": e["soc"]} for e in entries]

        equipment = {
            "inverter_count": len(inverters),
            "inverter_models": sorted(set(i.get("model") for i in inverters if i.get("model"))),
            "has_optimizer": len(optimizers) > 0, "optimizer_count": len(optimizers),
            "optimizer_model": (optimizers[0].get("model") if optimizers else None),
            "has_battery": len(batteries) > 0,
            "battery_pack_count": battery_pack_count,
            "battery_model": battery_model,
            "battery_soc_pct": battery_soc_pct,
            "battery_soc_trend_today": soc_trend_today,
            "has_smartguard": len(smartguards) > 0,
            "meter_count": len(meters),
        }

        plants_out.append({
            "plantCode": code, "plantName": mask_name(s["plantName"]),
            "capacity_kwp": s["capacity"], "gridConnectionDate": s["gridConnectionDate"],
            "health_state": real.get("real_health_state"),
            "power_now_kw": real.get("total_power"), "day_energy_kwh": real.get("day_power"),
            "day_use_kwh": real.get("day_use_energy"), "day_ongrid_kwh": real.get("day_on_grid_energy"),
            "month_energy_kwh": real.get("month_power"),
            "today_weather_symbol": today_sym, "today_weather_label": today_label,
            "trend_10d": trend, "inverters": inv_out, "equipment": equipment,
        })

    # (pv_today itself is scratch state for this run only — each inverter's
    # pv_trend_today was already read off it above, and no local file needs
    # saving since the next run reseeds from the deployed index.html.)

    # anomaly detection
    for p in plants_out:
        anomalies = []
        powers = [t["power_kwh"] for t in p["trend_10d"] if t["power_kwh"] is not None]
        if len(powers) >= 4:
            med = statistics.median(powers)
            if med > 0:
                for t in p["trend_10d"]:
                    pw = t["power_kwh"]
                    if pw is None: continue
                    if pw < med * 0.4 and t["weather_symbol"] not in ("rain", "storm", "drizzle") and t["date"] >= anomaly_cutoff_date:
                        anomalies.append({"date": t["date"], "type": "production_dip",
                            "detail": f"ผลผลิต {pw:.1f} kWh ต่ำกว่าค่ามัธยฐาน ({med:.1f} kWh) มากกว่า 60% ทั้งที่สภาพอากาศ ({t['weather_label']}) ไม่ได้แย่ — ควรตรวจสอบ"})
        invs = p["inverters"]
        if len(invs) > 1:
            avgs = []
            for inv in invs:
                vals = [x["perf_ratio"] for x in inv["trend_10d"] if x.get("perf_ratio") is not None]
                if vals: avgs.append((inv["sn"], statistics.mean(vals)))
            if len(avgs) > 1:
                overall = statistics.mean([a[1] for a in avgs])
                for sn, avg in avgs:
                    if overall > 0 and avg < overall * 0.7:
                        # NOTE: this is perpower_ratio (Huawei's "Specific
                        # energy", kWh produced per kWp installed per day) —
                        # per the official NBI reference, getDevKpiDay has no
                        # true % Performance Ratio field at the device level
                        # (that field, performance_ratio, only exists at the
                        # plant level via getKpiStationDay). Comparing
                        # specific yield across inverters at the same site
                        # is still a valid way to catch an underperforming
                        # string/inverter, but the label should say what the
                        # number actually is.
                        anomalies.append({"date": None, "type": "inverter_underperform",
                            "detail": f"Inverter {sn}: ผลผลิตต่อกำลังติดตั้งเฉลี่ย 10 วัน (specific yield) {avg:.2f} kWh/kWp ต่ำกว่าค่าเฉลี่ยรวมของสถานี ({overall:.2f} kWh/kWp) อย่างมีนัยสำคัญ — อาจมี string หรือ inverter ตัวนี้ทำงานผิดปกติ"})
        for inv in invs:
            t = inv.get("temperature_c")
            if t is not None and t >= 65:
                anomalies.append({"date": None, "type": "high_temp", "detail": f"Inverter {inv['sn']}: อุณหภูมิ {t}°C สูง ควรตรวจสอบการระบายความร้อน"})
        p["anomalies"] = anomalies

    out = {"generated_at": now.isoformat(), "dates10": dates10, "plants": plants_out,
           "alarms_5day": alarms, "historical_refreshed_at": historical_refreshed_at}
    save_json("dashboard_data_public_live.json", out)
    build_html(out)

def build_html(data):
    data_json = json.dumps(data, ensure_ascii=False)
    icons = {"sun": "&#9728;&#65039;", "sun-cloud": "&#127774;&#65039;", "cloud-sun": "&#9925;&#65039;",
             "cloud": "&#9729;&#65039;", "fog": "&#127787;&#65039;", "drizzle": "&#127783;&#65039;",
             "rain": "&#127783;&#65039;", "storm": "&#9928;&#65039;", "?": "&#10067;"}
    template_path = os.path.join(os.path.dirname(__file__), "template.html")
    with open(template_path, encoding="utf-8") as f:
        html = f.read()
    html = html.replace("__DATA_JSON__", data_json).replace("__ICONS_JSON__", json.dumps(icons))
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

if __name__ == "__main__":
    main()
