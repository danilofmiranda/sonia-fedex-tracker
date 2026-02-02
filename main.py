from fastapi import FastAPI, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
import httpx
import pandas as pd
import os
from io import BytesIO
from datetime import datetime, timedelta
import base64
import asyncio
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="FedEx Tracker - BloomsPal SonIA")

FEDEX_API_KEY = os.getenv("FEDEX_API_KEY")
FEDEX_SECRET_KEY = os.getenv("FEDEX_SECRET_KEY")
FEDEX_ACCOUNT = os.getenv("FEDEX_ACCOUNT")
FEDEX_BASE_URL = os.getenv("FEDEX_BASE_URL", "https://apis.fedex.com")

class FedExClient:
    def __init__(self):
        self.access_token = None
        self.base_url = FEDEX_BASE_URL
        
    async def authenticate(self):
        url = f"{self.base_url}/oauth/token"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = {"grant_type": "client_credentials", "client_id": FEDEX_API_KEY, "client_secret": FEDEX_SECRET_KEY}
        logger.info(f"Authenticating with FedEx at {url}")
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, headers=headers, data=data, timeout=30)
                logger.info(f"Auth response status: {response.status_code}")
                if response.status_code == 200:
                    self.access_token = response.json().get("access_token")
                    logger.info("Authentication successful")
                    return True
                else:
                    logger.error(f"Auth failed: {response.text}")
                    return False
            except Exception as e:
                logger.error(f"Auth exception: {str(e)}")
                return False
                
    async def track_shipment(self, tracking_number):
        if not self.access_token:
            if not await self.authenticate():
                logger.error("Failed to authenticate")
                return {"error": "Authentication failed"}
        
        url = f"{self.base_url}/track/v1/trackingnumbers"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "X-locale": "en_US"
        }
        data = {
            "includeDetailedScans": True,
            "trackingInfo": [{"trackingNumberInfo": {"trackingNumber": str(tracking_number)}}]
        }
        logger.info(f"Tracking {tracking_number}")
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, json=data, headers=headers, timeout=30)
                logger.info(f"Track status: {response.status_code}")
                result = response.json()
                logger.info(f"Response for {tracking_number}: {str(result)[:500]}")
                return result
            except Exception as e:
                logger.error(f"Track error {tracking_number}: {str(e)}")
                return {"error": str(e)}

fedex_client = FedExClient()

def calc_working_days(start, end):
    if not start or not end: return 0
    days = 0
    current = start
    while current <= end:
        if current.weekday() < 5: days += 1
        current += timedelta(days=1)
    return days

def parse_result(result, tracking, client=""):
    try:
        if "error" in result and isinstance(result["error"], str):
            return {"cliente": client, "tracking": tracking, "status": "Error", "status_desc": result["error"],
                    "label_date": "-", "ship_date": "-", "days_ship": 0, "work_days": 0, "days_label": 0, "location": "-", "sonia": f"❌ {result['error']}"}
        
        output = result.get("output", {})
        packages = output.get("completeTrackResults", [])
        if not packages:
            errors = result.get("errors", [])
            if errors:
                err_msg = errors[0].get("message", "API Error")
                logger.error(f"API error for {tracking}: {err_msg}")
                return {"cliente": client, "tracking": tracking, "status": "Error", "status_desc": err_msg,
                        "label_date": "-", "ship_date": "-", "days_ship": 0, "work_days": 0, "days_label": 0, "location": "-", "sonia": f"❌ {err_msg}"}
            return {"cliente": client, "tracking": tracking, "status": "Desconocido", "status_desc": "No data",
                    "label_date": "-", "ship_date": "-", "days_ship": 0, "work_days": 0, "days_label": 0, "location": "-", "sonia": "📍 Sin datos"}
        
        pkg = packages[0]
        track_results = pkg.get("trackResults", [])
        if not track_results:
            return {"cliente": client, "tracking": tracking, "status": "Desconocido", "status_desc": "No results",
                    "label_date": "-", "ship_date": "-", "days_ship": 0, "work_days": 0, "days_label": 0, "location": "-", "sonia": "📍 Sin resultados"}
        
        tr = track_results[0]
        if tr.get("error"):
            err = tr["error"]
            err_msg = err.get("message", "Track error")
            return {"cliente": client, "tracking": tracking, "status": "Error", "status_desc": err_msg,
                    "label_date": "-", "ship_date": "-", "days_ship": 0, "work_days": 0, "days_label": 0, "location": "-", "sonia": f"⚠️ {err_msg}"}
        
        latest = tr.get("latestStatusDetail", {})
        status_code = latest.get("code", "")
        status_desc = latest.get("description", "Unknown")
        status_map = {"DL": "Delivered", "DE": "Delivery Exception", "IT": "In Transit", "PU": "Picked Up", "OD": "Out for Delivery", "HL": "Label Created", "RS": "Return to Shipper", "DP": "Departed", "AR": "Arrived"}
        status = status_map.get(status_code, status_code if status_code else "Unknown")
        
        dates = tr.get("dateAndTimes", [])
        label_date = ship_date = None
        for d in dates:
            dtype = d.get("type", "")
            dval = d.get("dateTime", "")[:10] if d.get("dateTime") else None
            if dtype == "SHIP" and dval: ship_date = datetime.strptime(dval, "%Y-%m-%d")
            if dtype == "ACTUAL_PICKUP" and dval: label_date = datetime.strptime(dval, "%Y-%m-%d")
        if not label_date and ship_date: label_date = ship_date
        
        today = datetime.now()
        days_ship = (today - ship_date).days if ship_date else 0
        days_label = (today - label_date).days if label_date else 0
        work_days = calc_working_days(ship_date, today) if ship_date else 0
        
        loc = latest.get("scanLocation", {})
        location = f"{loc.get('city', '')}, {loc.get('stateOrProvinceCode', '')}, {loc.get('countryCode', '')}".strip(", ")
        if not location or location == ", ,": location = "-"
        
        if status == "Delivered": sonia = f"✅ Entregado - {days_ship} días"
        elif status == "In Transit": sonia = f"🚚 En tránsito - {days_ship} días"
        elif status == "Label Created": sonia = f"🏷️ Etiqueta - {days_label} días"
        elif status == "Delivery Exception": sonia = f"⚠️ Excepción"
        else: sonia = f"📍 {status}"
        
        return {"cliente": client, "tracking": tracking, "status": status, "status_desc": status_desc,
                "label_date": label_date.strftime("%Y-%m-%d") if label_date else "-",
                "ship_date": ship_date.strftime("%Y-%m-%d") if ship_date else "-",
                "days_ship": days_ship, "work_days": work_days, "days_label": days_label, "location": location, "sonia": sonia}
    except Exception as e:
        logger.error(f"Parse error {tracking}: {str(e)}")
        return {"cliente": client, "tracking": tracking, "status": "Error", "status_desc": str(e),
                "label_date": "-", "ship_date": "-", "days_ship": 0, "work_days": 0, "days_label": 0, "location": "-", "sonia": f"❌ {str(e)}"}

@app.get("/", response_class=HTMLResponse)
async def home():
    return """<!DOCTYPE html><html><head><title>FedEx Tracker - SonIA</title>
<style>body{font-family:Arial;background:linear-gradient(135deg,#667eea,#764ba2);min-height:100vh;display:flex;align-items:center;justify-content:center;margin:0}.container{background:white;padding:40px;border-radius:20px;box-shadow:0 20px 60px rgba(0,0,0,0.3);text-align:center;max-width:500px}h1{color:#4B0082}.upload-area{border:2px dashed #667eea;border-radius:10px;padding:40px;margin:20px 0;cursor:pointer}#fileInput{display:none}button{background:linear-gradient(135deg,#667eea,#764ba2);color:white;border:none;padding:15px 40px;border-radius:10px;font-size:16px;cursor:pointer;width:100%}button:disabled{background:#ccc}.result{margin-top:20px;padding:20px;background:#e8f5e9;border-radius:10px;display:none}.info{color:#888;font-size:12px;margin-top:10px}</style></head>
<body><div class="container"><h1>📦 FedEx Tracker</h1><p>BloomsPal SonIA</p>
<div class="upload-area" onclick="document.getElementById('fileInput').click()"><p>📄<br><strong>Sube tu archivo Excel</strong><br><span style="color:#888;font-size:12px">Col C=Cliente, Col O=HAWB</span></p></div>
<input type="file" id="fileInput" accept=".xlsx,.xls" onchange="document.getElementById('processBtn').disabled=false">
<button id="processBtn" onclick="processFile()" disabled>Procesar Guias</button>
<div id="result" class="result"></div>
<p class="info">Columnas: Cliente, Tracking, Status, Label Date, Ship Date, Days, Working Days, Days Label, Location, SonIA</p></div>
<script>async function processFile(){const file=document.getElementById('fileInput').files[0];if(!file)return;const fd=new FormData();fd.append('file',file);document.getElementById('processBtn').disabled=true;document.getElementById('processBtn').innerText='Procesando...';document.getElementById('result').style.display='none';try{const r=await fetch('/api/track',{method:'POST',body:fd});const d=await r.json();if(d.error){document.getElementById('result').innerHTML='<p style="color:red">Error: '+d.error+'</p>';document.getElementById('result').style.display='block'}else{document.getElementById('result').innerHTML='<p style="color:green">✅ SonIA procesó <strong>'+d.total+'</strong> guías</p>';document.getElementById('result').style.display='block';const a=document.createElement('a');a.href='data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,'+d.excel_file;a.download=d.filename;a.click()}}catch(e){document.getElementById('result').innerHTML='<p style="color:red">Error: '+e.message+'</p>';document.getElementById('result').style.display='block'}document.getElementById('processBtn').disabled=false;document.getElementById('processBtn').innerText='Procesar Guias'}</script></body></html>"""

@app.post("/api/track")
async def track_shipments(file: UploadFile = File(...)):
    try:
        logger.info(f"Processing: {file.filename}")
        contents = await file.read()
        df = pd.read_excel(BytesIO(contents), header=1)
        logger.info(f"Rows: {len(df)}, Cols: {df.columns.tolist()}")
        client_col = df.columns[2] if len(df.columns) > 2 else None
        tracking_col = df.columns[14] if len(df.columns) > 14 else df.columns[-1]
        for col in df.columns:
            if any(x in str(col).lower() for x in ['cliente', 'client']): client_col = col
            if any(x in str(col).lower() for x in ['hawb', 'tracking', 'guia']): tracking_col = col
        logger.info(f"Client: {client_col}, Track: {tracking_col}")
        tc = {}
        for _, row in df.iterrows():
            t = str(row[tracking_col]).strip() if pd.notna(row[tracking_col]) else None
            c = str(row[client_col]).strip() if client_col and pd.notna(row[client_col]) else ""
            if t and t != 'nan' and t != 'HAWB': tc[t] = c
        tns = list(tc.keys())
        logger.info(f"Tracking numbers: {len(tns)}")
        if not tns: return JSONResponse({"error": "No tracking numbers"})
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
        with pd.ExcelWriter(buf, engine='openpyxl') as w: df_out.to_excel(w, index=False)
        buf.seek(0)
        fn = f"fedex_tracking_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        logger.info(f"Done: {fn}")
        return JSONResponse({"total": len(results), "excel_file": base64.b64encode(buf.getvalue()).decode(), "filename": fn})
    except Exception as e:
        logger.error(f"Error: {str(e)}")
        return JSONResponse({"error": str(e)})

@app.get("/health")
async def health(): return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))from fastapi import FastAPI, UploadFile, File
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
