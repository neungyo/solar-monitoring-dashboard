"""
Refresh script for the Synnex Thailand solar monitoring dashboard.

Runs inside GitHub Actions on a schedule. Logs into Huawei FusionSolar
Northbound API, pulls fresh data (respecting Huawei's per-interface rate
limits by caching the slow-changing endpoints), rebuilds a PII-stripped
index.html, and leaves it in the repo root for GitHub Pages to serve.

Env vars required: FUSIONSOLAR_USER, FUSIONSOLAR_PASS
Optional: FUSIONSOLAR_DOMAIN (default sg5.fusionsolar.huawei.com)
"""
import os, sys, json, re, urllib.request, http.cookiejar, datetime, statistics, copy

DOMAIN = os.environ.get("FUSIONSOLAR_DOMAIN", "sg5.fusionsolar.huawei.com")
USER = os.environ["FUSIONSOLAR_USER"]
PASSWORD = os.environ["FUSIONSOLAR_PASS"]

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def post(path, body):
    req = urllib.request.Request(
        f"https://{DOMAIN}{path}",
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
        data = json.loads(resp.read())
    if not data.get("success"):
        raise RuntimeError(f"{path} failed: {data.get('failCode')} {data.get('message')}")
    return data

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

def main():
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
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

    # ---- realtime data: every run ----
    realkpi = post("/thirdData/getStationRealKpi", {"stationCodes": codes})["data"]
    devreal = []
    if inv38:
        devreal += post("/thirdData/getDevRealKpi", {"devIds": ",".join(inv38), "devTypeId": 38})["data"]
    if inv1:
        devreal += post("/thirdData/getDevRealKpi", {"devIds": ",".join(inv1), "devTypeId": 1})["data"]

    end = now
    start = end - datetime.timedelta(days=5)
    alarms = post("/thirdData/getAlarmList", {
        "stationCodes": codes,
        "beginTime": int(start.timestamp() * 1000),
        "endTime": int(end.timestamp() * 1000),
        "language": "en_US",
    })["data"]

    # ---- historical day + weather: refresh at most hourly ----
    if stale("data/hourly_refreshed_at.json", 3600) or not os.path.exists("data/kpiday.json"):
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
        save_json("data/kpiday.json", kpiday)
        save_json("data/devkpiday.json", devkpiday)
        save_json("data/weather.json", weather)
        touch("data/hourly_refreshed_at.json")
    else:
        kpiday = load_json("data/kpiday.json")
        devkpiday = load_json("data/devkpiday.json")
        weather = load_json("data/weather.json")

    # ================= combine =================
    real_by_code = {r["stationCode"]: r["dataItemMap"] for r in realkpi}
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
            dd = devday_by_devid.get(devid, {})
            inv_trend = []
            for t, dstr in zip(dev_last10, dev_dates10):
                item = dd.get(t, {})
                inv_trend.append({"date": dstr, "power_kwh": item.get("product_power"), "perf_ratio": item.get("perpower_ratio")})
            inv_out.append({
                "sn": inv.get("esnCode"), "model": inv.get("model"),
                "capacity_kw": parse_kw_from_model(inv.get("model")),
                "optimizer_count": inv.get("optimizerNumber"),
                "temperature_c": real_kpi.get("temperature"),
                "active_power_kw": real_kpi.get("active_power"),
                "day_energy_kwh": real_kpi.get("day_cap"),
                "strings": strings, "trend_10d": inv_trend,
            })

        battery_total_kwh = 0.0
        battery_model = None
        for b in batteries:
            kwv = parse_kw_from_model(b.get("model"))
            if kwv: battery_total_kwh += kwv
            battery_model = b.get("model")

        equipment = {
            "inverter_count": len(inverters),
            "inverter_models": sorted(set(i.get("model") for i in inverters if i.get("model"))),
            "has_optimizer": len(optimizers) > 0, "optimizer_count": len(optimizers),
            "optimizer_model": (optimizers[0].get("model") if optimizers else None),
            "has_battery": len(batteries) > 0, "battery_count": len(batteries),
            "battery_model": battery_model,
            "battery_total_kwh": battery_total_kwh if batteries else None,
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
                    if pw < med * 0.4 and t["weather_symbol"] not in ("rain", "storm", "drizzle"):
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
                        anomalies.append({"date": None, "type": "inverter_underperform",
                            "detail": f"Inverter {sn}: ค่าเฉลี่ย performance ratio 10 วัน ({avg:.2f}) ต่ำกว่าค่าเฉลี่ยรวมของสถานี ({overall:.2f}) อย่างมีนัยสำคัญ — อาจมี string หรือ inverter ตัวนี้ทำงานผิดปกติ"})
        for inv in invs:
            t = inv.get("temperature_c")
            if t is not None and t >= 65:
                anomalies.append({"date": None, "type": "high_temp", "detail": f"Inverter {inv['sn']}: อุณหภูมิ {t}°C สูง ควรตรวจสอบการระบายความร้อน"})
        p["anomalies"] = anomalies

    out = {"generated_at": now.isoformat(), "dates10": dates10, "plants": plants_out, "alarms_5day": alarms}
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
