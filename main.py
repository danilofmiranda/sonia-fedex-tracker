from fastapi import FastAPI, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
import httpx
import pandas as pd
import os
from io import BytesIO
from datetime import datetime
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
                return {"error": "Error de autenticacion con FedEx"}
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

def parse_tracking_result(result: dict, tracking_number: str):
    try:
        if "error" in result:
            return {"tracking_number": tracking_number, "status": "Error", "description": result.get("error", "Error"), "location": "-", "date": "-", "sonia_comment": "No se pudo obtener info"}
        output = result.get("output", {})
        packages = output.get("completeTrackResults", [])
        if not packages:
            return {"tracking_number": tracking_number, "status": "No encontrado", "description": "No se encontro", "location": "-", "date": "-", "sonia_comment": "No encontrado en FedEx"}
        track_results = packages[0].get("trackResults", [])
        if not track_results:
            return {"tracking_number": tracking_number, "status": "Sin datos", "description": "Sin resultados", "location": "-", "date": "-", "sonia_comment": "Sin info"}
        track_info = track_results[0]
        latest_status = track_info.get("latestStatusDetail", {})
        status_code = latest_status.get("code", "UNKNOWN")
        status_desc = latest_status.get("description", "Desconocido")
        loc = latest_status.get("scanLocation", {})
        location = f"{loc.get('city', '')}, {loc.get('stateOrProvinceCode', '')} {loc.get('countryCode', '')}".strip(", ")
        date_times = track_info.get("dateAndTimes", [])
        delivery_date = "-"
        for dt in date_times:
            if dt.get("type") in ["ACTUAL_DELIVERY", "ESTIMATED_DELIVERY", "SHIP"]:
                delivery_date = dt.get("dateTime", "-")[:10]
                break
        sonia_comment = generate_sonia_comment(status_code, status_desc, location)
        return {"tracking_number": tracking_number, "status": status_code, "description": status_desc, "location": location if location else "-", "date": delivery_date, "sonia_comment": sonia_comment}
    except Exception as e:
        return {"tracking_number": tracking_number, "status": "Error", "description": str(e), "location": "-", "date": "-", "sonia_comment": "Error"}

def generate_sonia_comment(status_code: str, description: str, location: str):
    status_comments = {"DL": f"Entregado en {location}.", "IT": f"En transito: {location}", "PU": "Recogido.", "OD": f"En camino: {location}", "DE": "Problema entrega.", "SE": "Excepcion.", "CA": "Cancelado."}
    return status_comments.get(status_code, f"Estado: {description}")

@app.get("/", response_class=HTMLResponse)
async def home():
    return """<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>FedEx Tracker - SonIA</title><style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:system-ui;background:linear-gradient(135deg,#667eea,#764ba2);min-height:100vh;display:flex;justify-content:center;align-items:center;padding:20px}.c{background:#fff;border-radius:20px;padding:40px;max-width:500px;width:100%;box-shadow:0 20px 60px rgba(0,0,0,.3)}h1{color:#4a00a0;text-align:center;margin-bottom:30px}.u{border:2px dashed #667eea;border-radius:15px;padding:40px;text-align:center;background:#f8f9ff;margin-bottom:20px;cursor:pointer}.u:hover{border-color:#4a00a0}input[type=file]{display:none}.b{width:100%;padding:15px;background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;border:none;border-radius:10px;font-size:16px;font-weight:700;cursor:pointer}.b:disabled{background:#ccc}.f{background:#e8f5e9;padding:10px;border-radius:8px;margin-bottom:20px;display:none;color:#2e7d32}.l{text-align:center;padding:30px;display:none}.s{border:4px solid #f3f3f3;border-top:4px solid #667eea;border-radius:50%;width:50px;height:50px;animation:spin 1s linear infinite;margin:0 auto 20px}@keyframes spin{to{transform:rotate(360deg)}}.ok{background:#e8f5e9;padding:25px;border-radius:15px;margin-top:20px;display:none;text-align:center;color:#4a00a0;font-size:18px}.er{background:#ffebee;color:#c62828;padding:20px;border-radius:10px;margin-top:20px;display:none;text-align:center}</style></head><body><div class="c"><h1>📦 FedEx Tracker</h1><div class="u" id="u" onclick="document.getElementById('i').click()"><p style="font-size:48px">📄</p><p><b>Sube tu archivo Excel</b></p></div><input type="file" id="i" accept=".xlsx,.xls" onchange="sel(this)"><div class="f" id="f"></div><button class="b" id="b" onclick="go()" disabled>Procesar Guias</button><div class="l" id="l"><div class="s"></div><p>Procesando...</p></div><div class="ok" id="ok"></div><div class="er" id="er"></div></div><script>let file;function sel(e){e.files&&e.files[0]&&(file=e.files[0],document.getElementById("f").style.display="block",document.getElementById("f").textContent="📄 "+file.name,document.getElementById("b").disabled=!1,document.getElementById("ok").style.display="none",document.getElementById("er").style.display="none")}async function go(){if(!file)return;const b=document.getElementById("b"),l=document.getElementById("l"),u=document.getElementById("u"),ok=document.getElementById("ok"),er=document.getElementById("er");b.disabled=!0;b.style.display="none";u.style.display="none";document.getElementById("f").style.display="none";l.style.display="block";ok.style.display="none";er.style.display="none";const fd=new FormData;fd.append("file",file);try{const r=await fetch("/api/track",{method:"POST",body:fd});const d=await r.json();if(d.error){er.textContent="❌ "+d.error;er.style.display="block"}else{dl(d.excel_file,d.filename);ok.textContent="💬 SonIA: Han sido procesadas y descargadas exitosamente "+d.total+" guias";ok.style.display="block"}}catch(e){er.textContent="❌ "+e.message;er.style.display="block"}finally{l.style.display="none";b.style.display="block";b.disabled=!1;u.style.display="block";file=null;document.getElementById("i").value=""}}function dl(b64,fn){const bc=atob(b64),bn=new Array(bc.length);for(let i=0;i<bc.length;i++)bn[i]=bc.charCodeAt(i);const ba=new Uint8Array(bn),bl=new Blob([ba],{type:"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}),a=document.createElement("a");a.href=URL.createObjectURL(bl);a.download=fn;document.body.appendChild(a);a.click();document.body.removeChild(a)}</script></body></html>"""

@app.post("/api/track")
async def track_shipments(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        df = pd.read_excel(BytesIO(contents))
        tracking_col = None
        if len(df.columns) > 14:
            tracking_col = df.columns[14]
        for col in df.columns:
            if any(x in str(col).lower() for x in ['hawb', 'tracking', 'guia', 'numero']):
                tracking_col = col
                break
        if tracking_col is None:
            tracking_col = df.columns[14] if len(df.columns) > 14 else df.columns[0]
        tracking_numbers = df[tracking_col].dropna().astype(str).unique()
        tracking_numbers = [t.strip() for t in tracking_numbers if t.strip() and t.strip() != 'nan']
        if not tracking_numbers:
            return JSONResponse({"error": "No se encontraron numeros de tracking"})
        BATCH_SIZE = 30
        results = []
        for i in range(0, len(tracking_numbers), BATCH_SIZE):
            batch = tracking_numbers[i:i + BATCH_SIZE]
            tasks = [fedex_client.track_shipment(tn) for tn in batch]
            batch_results = await asyncio.gather(*tasks)
            for tn, r in zip(batch, batch_results):
                results.append(parse_tracking_result(r, tn))
            if i + BATCH_SIZE < len(tracking_numbers):
                await asyncio.sleep(0.5)
        df_results = pd.DataFrame(results)
        df_results.columns = ['Tracking', 'Estado', 'Descripcion', 'Ubicacion', 'Fecha', 'Comentario SonIA']
        excel_buffer = BytesIO()
        with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
            df_results.to_excel(writer, index=False, sheet_name='Resultados')
        excel_buffer.seek(0)
        return JSONResponse({"total": len(results), "excel_file": base64.b64encode(excel_buffer.getvalue()).decode('utf-8'), "filename": f"fedex_tracking_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"})
    except Exception as e:
        return JSONResponse({"error": str(e)})

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
