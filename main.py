from fastapi import FastAPI, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
import httpx
import pandas as pd
import os
from io import BytesIO
from datetime import datetime, timedelta
import base64
import asyncio

app = FastAPI(title="FedEx Tracker - BloomsPal SonIA")

FEDEX_API_KEY = os.getenv("FEDEX_API_KEY")
FEDEX_SECRET_KEY = os.getenv("FEDEX_SECRET_KEY")
FEDEX_ACCOUNT = os.getenv("FEDEX_ACCOUNT")
FEDEX_BASE_URL = os.getenv("FEDEX_BASE_URL", "https://apis-sandbox.fedex.com")

class FedExClient:
    def __init__(self):
        self.access_token = None
        self.base_url = FEDEX_BASE_URL

    async def authenticate(self):
        url = f"{self.base_url}/oauth/token"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = {"grant_type": "client_credentials", "client_id": FEDEX_API_KEY, "client_secret": FEDEX_SECRET_KEY}
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, headers=headers, data=data, timeout=30)
                if response.status_code == 200:
                    self.access_token = response.json().get("access_token")
                    return True
                return False
            except:
                return False

    async def track_shipment(self, tracking_number: str):
        if not self.access_token:
            if not await self.authenticate():
                return {"error": "Auth error"}
        url = f"{self.base_url}/track/v1/trackingnumbers"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.access_token}", "X-locale": "es_CO"}
        payload = {"includeDetailedScans": True, "trackingInfo": [{"trackingNumberInfo": {"trackingNumber": str(tracking_number).strip()}}]}
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, headers=headers, json=payload, timeout=30)
                if response.status_code == 200:
                    return response.json()
                return {"error": f"Error {response.status_code}"}
            except Exception as e:
                return {"error": str(e)}

fedex_client = FedExClient()

def calc_working_days(start, end):
    if not start or not end: return 0
    try:
        if isinstance(start, str): start = datetime.fromisoformat(start[:10])
        if isinstance(end, str): end = datetime.fromisoformat(end[:10])
        wd = 0
        cur = start
        while cur <= end:
            if cur.weekday() < 5: wd += 1
            cur += timedelta(days=1)
        return wd
    except: return 0

def calc_days(start, end):
    if not start or not end: return 0
    try:
        if isinstance(start, str): start = datetime.fromisoformat(start[:10])
        if isinstance(end, str): end = datetime.fromisoformat(end[:10])
        return (end - start).days
    except: return 0

def parse_result(result, tracking, client=""):
    today = datetime.now()
    default = {"cliente": client, "tracking": tracking, "status": "Error", "status_desc": "No info", "label_date": "-", "ship_date": "-", "days_ship": 0, "work_days": 0, "days_label": 0, "location": "-", "sonia": "No se pudo obtener info"}
    try:
        if "error" in result:
            default["status_desc"] = result.get("error", "Error")
            return default
        output = result.get("output", {})
        packages = output.get("completeTrackResults", [])
        if not packages:
            default["status"] = "No encontrado"
            return default
        track_results = packages[0].get("trackResults", [])
        if not track_results:
            default["status"] = "Sin datos"
            return default
        info = track_results[0]
        latest = info.get("latestStatusDetail", {})
        code = latest.get("code", "UNKNOWN")
        desc = latest.get("description", "Desconocido")
        status_map = {"DL": "Delivered", "IT": "In Transit", "PU": "Picked Up", "OD": "Out for Delivery", "DE": "Delivery Exception", "SE": "Shipment Exception", "HL": "Label Created", "OC": "Label Created", "RS": "Return to Shipper", "CA": "Cancelled", "AD": "At Destination", "AF": "At FedEx", "AR": "Arrived", "DP": "Departed", "PM": "In Progress"}
        friendly = status_map.get(code, desc)
        loc = latest.get("scanLocation", {})
        location = f"{loc.get('city', '')}, {loc.get('stateOrProvinceCode', '')}, {loc.get('countryCode', '')}".strip(", ")
        dates = info.get("dateAndTimes", [])
        ship_date = label_date = None
        for d in dates:
            t = d.get("type", "")
            v = d.get("dateTime", "")
            if t == "SHIP": ship_date = v
            elif t in ["ACTUAL_PICKUP", "PICKUP"] and not ship_date: ship_date = v
        if not ship_date:
            scans = info.get("scanEvents", [])
            if scans: ship_date = scans[-1].get("date", "")
        label_date = ship_date
        ship_str = ship_date[:10] if ship_date else "-"
        label_str = label_date[:10] if label_date else "-"
        days_ship = calc_days(ship_date, today.isoformat()) if ship_date else 0
        work_days = calc_working_days(ship_date, today.isoformat()) if ship_date else 0
        days_label = calc_days(label_date, today.isoformat()) if label_date else 0
        sonia = gen_sonia(code, friendly, location, days_ship)
        return {"cliente": client, "tracking": tracking, "status": friendly, "status_desc": desc, "label_date": label_str, "ship_date": ship_str, "days_ship": days_ship, "work_days": work_days, "days_label": days_label, "location": location or "-", "sonia": sonia}
    except Exception as e:
        default["status_desc"] = str(e)
        return default

def gen_sonia(code, status, loc, days):
    if code == "DL": return f"✅ Entregado en {loc}"
    elif code == "IT": return f"🚚 En transito - {days} dias. {loc}"
    elif code == "PU": return "📦 Recogido por FedEx"
    elif code == "OD": return f"🏃 En camino para entrega hoy - {loc}"
    elif code in ["DE", "SE"]: return "⚠️ ATENCION: Excepcion - Revisar"
    elif code in ["HL", "OC"]: return f"🏷️ Etiqueta creada - {days} dias"
    elif code == "CA": return "❌ Cancelado"
    elif code == "RS": return "↩️ Devuelto"
    return f"📍 {status} - {loc}"

@app.get("/", response_class=HTMLResponse)
async def home():
    return """<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>FedEx Tracker - SonIA</title><style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:system-ui;background:linear-gradient(135deg,#667eea,#764ba2);min-height:100vh;display:flex;justify-content:center;align-items:center;padding:20px}.c{background:#fff;border-radius:20px;padding:40px;max-width:500px;width:100%;box-shadow:0 20px 60px rgba(0,0,0,.3)}h1{color:#4a00a0;text-align:center;margin-bottom:10px}.sub{text-align:center;color:#666;margin-bottom:30px;font-size:14px}.u{border:2px dashed #667eea;border-radius:15px;padding:40px;text-align:center;background:#f8f9ff;margin-bottom:20px;cursor:pointer}input[type=file]{display:none}.b{width:100%;padding:15px;background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;border:none;border-radius:10px;font-size:16px;font-weight:700;cursor:pointer}.b:disabled{background:#ccc}.f{background:#e8f5e9;padding:10px;border-radius:8px;margin-bottom:20px;display:none;color:#2e7d32}.l{text-align:center;padding:30px;display:none}.s{border:4px solid #f3f3f3;border-top:4px solid #667eea;border-radius:50%;width:50px;height:50px;animation:spin 1s linear infinite;margin:0 auto 20px}@keyframes spin{to{transform:rotate(360deg)}}.ok{background:#e8f5e9;padding:25px;border-radius:15px;margin-top:20px;display:none;text-align:center}.ok .msg{color:#4a00a0;font-weight:bold;font-size:16px}.er{background:#ffebee;color:#c62828;padding:20px;border-radius:10px;margin-top:20px;display:none;text-align:center}.note{font-size:11px;color:#999;text-align:center;margin-top:15px}</style></head><body><div class="c"><h1>📦 FedEx Tracker</h1><p class="sub">BloomsPal SonIA</p><div class="u" id="u" onclick="document.getElementById('i').click()"><p style="font-size:48px">📄</p><p><b>Sube tu archivo Excel</b></p><p style="color:#999;font-size:12px">Col C=Cliente, Col O=HAWB</p></div><input type="file" id="i" accept=".xlsx,.xls" onchange="sel(this)"><div class="f" id="f"></div><button class="b" id="b" onclick="go()" disabled>Procesar Guias</button><div class="l" id="l"><div class="s"></div><p>Procesando...</p></div><div class="ok" id="ok"><p style="font-size:48px">✅</p><p class="msg" id="msg"></p></div><div class="er" id="er"></div><p class="note">Columnas: Cliente, Tracking, Status, Label Date, Ship Date, Days, Working Days, Days Label, Location, SonIA</p></div><script>let file;function sel(e){if(e.files&&e.files[0]){file=e.files[0];document.getElementById("f").style.display="block";document.getElementById("f").textContent="📄 "+file.name;document.getElementById("b").disabled=!1;document.getElementById("ok").style.display="none";document.getElementById("er").style.display="none"}}async function go(){if(!file)return;const b=document.getElementById("b"),l=document.getElementById("l"),u=document.getElementById("u"),ok=document.getElementById("ok"),er=document.getElementById("er");b.disabled=!0;b.style.display="none";u.style.display="none";document.getElementById("f").style.display="none";l.style.display="block";ok.style.display="none";er.style.display="none";const fd=new FormData;fd.append("file",file);try{const r=await fetch("/api/track",{method:"POST",body:fd});const d=await r.json();if(d.error){er.textContent="❌ "+d.error;er.style.display="block"}else{dl(d.excel_file,d.filename);document.getElementById("msg").textContent="💬 SonIA: Procesadas y descargadas "+d.total+" guias";ok.style.display="block"}}catch(e){er.textContent="❌ "+e.message;er.style.display="block"}finally{l.style.display="none";b.style.display="block";b.disabled=!1;u.style.display="block";file=null;document.getElementById("i").value=""}}function dl(b64,fn){const bc=atob(b64),bn=new Array(bc.length);for(let i=0;i<bc.length;i++)bn[i]=bc.charCodeAt(i);const ba=new Uint8Array(bn),bl=new Blob([ba],{type:"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}),a=document.createElement("a");a.href=URL.createObjectURL(bl);a.download=fn;document.body.appendChild(a);a.click();document.body.removeChild(a)}</script></body></html>"""

@app.post("/api/track")
async def track_shipments(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        df = pd.read_excel(BytesIO(contents), header=1)
        client_col = df.columns[2] if len(df.columns) > 2 else None
        for col in df.columns:
            if any(x in str(col).lower() for x in ['cliente', 'client', 'nombre']): client_col = col; break
        tracking_col = df.columns[14] if len(df.columns) > 14 else df.columns[0]
        for col in df.columns:
            if any(x in str(col).lower() for x in ['hawb', 'tracking', 'guia']): tracking_col = col; break
        tc = {}
        for _, row in df.iterrows():
            t = str(row[tracking_col]).strip() if pd.notna(row[tracking_col]) else ""
            c = str(row[client_col]).strip() if client_col and pd.notna(row[client_col]) else ""
            if t and t != 'nan' and t != 'HAWB': tc[t] = c
        tns = list(tc.keys())
        if not tns: return JSONResponse({"error": "No tracking numbers found"})
        results = []
        for i in range(0, len(tns), 30):
            batch = tns[i:i+30]
            tasks = [fedex_client.track_shipment(tn) for tn in batch]
            br = await asyncio.gather(*tasks)
            for tn, r in zip(batch, br): results.append(parse_result(r, tn, tc.get(tn, "")))
            if i + 30 < len(tns): await asyncio.sleep(0.5)
        df_out = pd.DataFrame(results)
        df_out.columns = ['Nombre Cliente', 'FEDEX Tracking', 'Status', 'Status Description', 'Label Creation Date', 'Shipping Date', 'Days After Shipment', 'Working Days After Shipment', 'Days After Label Creation', 'Shipping City/State/Country', 'SonIA - BloomsPal']
        buf = BytesIO()
        with pd.ExcelWriter(buf, engine='openpyxl') as w: df_out.to_excel(w, index=False, sheet_name='Results')
        buf.seek(0)
        return JSONResponse({"total": len(results), "excel_file": base64.b64encode(buf.getvalue()).decode('utf-8'), "filename": f"fedex_tracking_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"})
    except Exception as e: return JSONResponse({"error": str(e)})

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
