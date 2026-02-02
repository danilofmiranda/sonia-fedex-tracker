from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx
import pandas as pd
import os
from io import BytesIO
from datetime import datetime

app = FastAPI(title="FedEx Tracker - BloomsPal SonIA")

# Configuracion FedEx - usando variables de entorno
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
                print(f"Auth response: {response.status_code}")
                if response.status_code == 200:
                    self.access_token = response.json().get("access_token")
                    return True
                return False
            except Exception as e:
                print(f"Auth error: {e}")
                return False

    async def track_shipment(self, tracking_number):
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

def parse_tracking_result(result, tracking_number):
    try:
        if "error" in result:
            return {"tracking_number": tracking_number, "status": "Error", "description": result.get("error"), "location": "-", "date": "-", "sonia_comment": "No se pudo obtener info"}
        output = result.get("output", {})
        packages = output.get("completeTrackResults", [])
        if not packages:
            return {"tracking_number": tracking_number, "status": "No encontrado", "description": "Sin info", "location": "-", "date": "-", "sonia_comment": "No encontrado"}
        track_results = packages[0].get("trackResults", [])
        if not track_results:
            return {"tracking_number": tracking_number, "status": "Sin datos", "description": "Sin tracking", "location": "-", "date": "-", "sonia_comment": "Sin info"}
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
        comments = {"DL": f"Entregado en {location}", "IT": f"En transito: {location}", "PU": "Recogido", "OD": f"En camino: {location}", "DE": "ATENCION: Problema entrega", "SE": "ALERTA: Excepcion"}
        sonia = comments.get(status_code, f"{status_desc} - {location}")
        return {"tracking_number": tracking_number, "status": status_code, "description": status_desc, "location": location or "-", "date": delivery_date, "sonia_comment": sonia}
    except Exception as e:
        return {"tracking_number": tracking_number, "status": "Error", "description": str(e), "location": "-", "date": "-", "sonia_comment": "Error"}

@app.get("/", response_class=HTMLResponse)
async def home():
    return """<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>FedEx Tracker - SonIA</title><style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:system-ui;background:linear-gradient(135deg,#667eea,#764ba2);min-height:100vh;display:flex;justify-content:center;align-items:center;padding:20px}.container{background:#fff;border-radius:20px;padding:40px;max-width:600px;width:100%;box-shadow:0 20px 60px rgba(0,0,0,.3)}h1{color:#4a00a0;text-align:center;margin-bottom:10px}.subtitle{text-align:center;color:#666;margin-bottom:30px}.upload-area{border:2px dashed #667eea;border-radius:15px;padding:40px;text-align:center;background:#f8f9ff;margin-bottom:20px;cursor:pointer}.upload-area:hover{border-color:#4a00a0;background:#f0f2ff}.upload-icon{font-size:48px;margin-bottom:15px}input[type=file]{display:none}.btn{width:100%;padding:15px;background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;border:none;border-radius:10px;font-size:16px;font-weight:700;cursor:pointer}.btn:disabled{background:#ccc}.file-name{background:#e8f5e9;padding:10px 15px;border-radius:8px;margin-bottom:20px;display:none;color:#2e7d32}.results{margin-top:30px;display:none}.result-item{background:#f5f5f5;padding:15px;border-radius:10px;margin-bottom:10px;border-left:4px solid #667eea}.result-item.success{border-left-color:#4caf50}.result-item.error{border-left-color:#f44336}.tracking-num{font-weight:700}.status{font-size:14px;color:#666;margin:5px 0}.sonia{font-style:italic;color:#4a00a0;margin-top:8px}.loading{text-align:center;padding:20px;display:none}.spinner{border:3px solid #f3f3f3;border-top:3px solid #667eea;border-radius:50%;width:40px;height:40px;animation:spin 1s linear infinite;margin:0 auto 15px}@keyframes spin{to{transform:rotate(360deg)}}.error-msg{background:#ffebee;color:#c62828;padding:15px;border-radius:10px;margin-top:20px;display:none}.footer{text-align:center;margin-top:30px;color:#999;font-size:12px}</style></head><body><div class="container"><h1>FedEx Tracker</h1><p class="subtitle">BloomsPal SonIA - Sistema de Rastreo</p><div class="upload-area" onclick="document.getElementById('fileInput').click()"><div class="upload-icon">📦</div><p><strong>Sube tu archivo Excel</strong></p><p style="color:#999;font-size:14px">Haz clic para seleccionar</p></div><input type="file" id="fileInput" accept=".xlsx,.xls" onchange="fileSelected(this)"><div class="file-name" id="fileName"></div><button class="btn" id="processBtn" onclick="processFile()" disabled>Procesar y Generar Reporte</button><div class="loading" id="loading"><div class="spinner"></div><p>Consultando FedEx...</p></div><div class="error-msg" id="errorMsg"></div><div class="results" id="results"></div><div class="footer">BloomsPal | FedEx Track API</div></div><script>let selectedFile=null;function fileSelected(e){e.files&&e.files[0]&&(selectedFile=e.files[0],document.getElementById("fileName").style.display="block",document.getElementById("fileName").textContent="📄 "+selectedFile.name,document.getElementById("processBtn").disabled=!1)}async function processFile(){if(selectedFile){const e=document.getElementById("processBtn"),t=document.getElementById("loading"),n=document.getElementById("results"),o=document.getElementById("errorMsg");e.disabled=!0,t.style.display="block",n.style.display="none",o.style.display="none";const s=new FormData;s.append("file",selectedFile);try{const e=await(await fetch("/api/track",{method:"POST",body:s})).json();e.error?(o.textContent="❌ "+e.error,o.style.display="block"):displayResults(e.results)}catch(e){o.textContent="❌ Error: "+e.message,o.style.display="block"}finally{t.style.display="none",e.disabled=!1}}}function displayResults(e){const t=document.getElementById("results");t.innerHTML="<h3>Resultados</h3>",e.forEach(e=>{let n="result-item";"DL"===e.status?n+=" success":"Error"!==e.status&&"SE"!==e.status||(n+=" error"),t.innerHTML+=`<div class="${n}"><div class="tracking-num">${e.tracking_number}</div><div class="status"><b>Estado:</b> ${e.description}</div><div class="status"><b>Ubicacion:</b> ${e.location}</div><div class="status"><b>Fecha:</b> ${e.date}</div><div class="sonia">💬 SonIA: ${e.sonia_comment}</div></div>`}),t.style.display="block"}</script></body></html>"""

@app.post("/api/track")
async def track_shipments(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        df = pd.read_excel(BytesIO(contents))
        tracking_col = None
        if len(df.columns) > 14:
            tracking_col = df.columns[14]
        for col in df.columns:
            if any(x in str(col).lower() for x in ['hawb', 'tracking', 'guia']):
                tracking_col = col
                break
        if not tracking_col:
            tracking_col = df.columns[0]
        tracking_numbers = df[tracking_col].dropna().astype(str).unique()
        tracking_numbers = [t.strip() for t in tracking_numbers if t.strip() and t != 'nan'][:10]
        if not tracking_numbers:
            return JSONResponse({"error": "No se encontraron tracking numbers"})
        results = []
        for tn in tracking_numbers:
            result = await fedex_client.track_shipment(tn)
            results.append(parse_tracking_result(result, tn))
        return JSONResponse({"results": results, "total": len(results)})
    except Exception as e:
        return JSONResponse({"error": str(e)})

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
