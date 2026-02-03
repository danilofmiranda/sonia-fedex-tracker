from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
import httpx
import pandas as pd
import os
from io import BytesIO
from datetime import datetime, timedelta
import base64
import asyncio
import logging
import json
import uuid
import time

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="SonIA Tracker - BloomsPal")

# Store job progress and results
jobs = {}

FEDEX_API_KEY = os.getenv("FEDEX_API_KEY")
FEDEX_SECRET_KEY = os.getenv("FEDEX_SECRET_KEY")
FEDEX_ACCOUNT = os.getenv("FEDEX_ACCOUNT")
FEDEX_BASE_URL = os.getenv("FEDEX_BASE_URL", "https://apis.fedex.com")

class FedExClient:
    def __init__(self):
        self.access_token = None
        self.base_url = FEDEX_BASE_URL
        self.token_expires_at = None  # Timestamp when token expires
        self.token_buffer = 300  # Refresh 5 minutes before expiration

    def is_token_expired(self):
        """Check if token is expired or about to expire"""
        if not self.access_token or not self.token_expires_at:
            return True
        # Refresh if less than buffer seconds remaining
        return time.time() >= (self.token_expires_at - self.token_buffer)

    async def authenticate(self):
        url = f"{self.base_url}/oauth/token"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = {"grant_type": "client_credentials", "client_id": FEDEX_API_KEY, "client_secret": FEDEX_SECRET_KEY}
        logger.info(f"Authenticating with FedEx at {url}")
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, headers=headers, data=data)
                logger.info(f"Auth response status: {response.status_code}")
                if response.status_code == 200:
                    token_data = response.json()
                    self.access_token = token_data.get("access_token")
                    expires_in = token_data.get("expires_in", 3600)  # Default 1 hour
                    self.token_expires_at = time.time() + expires_in
                    logger.info(f"Authentication successful - token valid for {expires_in} seconds")
                    return True
                else:
                    logger.error(f"Auth failed: {response.text}")
                    return False
        except Exception as e:
            logger.error(f"Auth exception: {e}")
            return False

    async def track_shipment(self, tracking_number, retry_count=0):
        # Check if token needs refresh based on expires_in
        if self.is_token_expired():
            logger.info("Token expired or about to expire, refreshing...")
            await self.authenticate()
        
        url = f"{self.base_url}/track/v1/trackingnumbers"
        headers = {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json", "X-locale": "en_US"}
        payload = {"includeDetailedScans": True, "trackingInfo": [{"trackingNumberInfo": {"trackingNumber": tracking_number}}]}
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, headers=headers, json=payload)
                
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 401 and retry_count < 2:
                    # Token rejected, force refresh and retry
                    logger.info("Got 401, forcing token refresh...")
                    self.token_expires_at = None  # Force refresh
                    await self.authenticate()
                    return await self.track_shipment(tracking_number, retry_count + 1)
                elif response.status_code == 429 and retry_count < 3:
                    # Rate limited - wait and retry with exponential backoff
                    wait_time = (retry_count + 1) * 2
                    logger.warning(f"Rate limited (429), waiting {wait_time}s before retry...")
                    await asyncio.sleep(wait_time)
                    return await self.track_shipment(tracking_number, retry_count + 1)
                else:
                    logger.error(f"Track failed for {tracking_number}: {response.status_code}")
                    return None
        except Exception as e:
            logger.error(f"Exception tracking {tracking_number}: {str(e)}")
            if retry_count < 2:
                await asyncio.sleep(1)
                return await self.track_shipment(tracking_number, retry_count + 1)
            return None

def get_short_status(status_code, description):
    """Convert FedEx status to normalized SonIA status. Check description FIRST."""
    desc_lower = description.lower() if description else ""

    if "shipment information sent" in desc_lower:
        return "Label Created"
    if "label created" in desc_lower or "shipping label" in desc_lower:
        return "Label Created"
    if "delivered" in desc_lower:
        return "Delivered"
    if "out for delivery" in desc_lower or "on fedex vehicle for delivery" in desc_lower:
        return "Out for Delivery"
    if "picked up" in desc_lower or "package received" in desc_lower:
        return "Picked Up"
    if any(x in desc_lower for x in ["in transit", "departed", "arrived", "left fedex",
                                      "at fedex", "on the way", "at destination sort",
                                      "at local fedex", "in fedex", "international shipment release"]):
        return "In Transit"
    if any(x in desc_lower for x in ["clearance", "customs", "import", "broker"]):
        return "In Customs"
    if "exception" in desc_lower:
        return "Exception"
    if "delay" in desc_lower:
        return "Delayed"
    if "hold" in desc_lower:
        return "On Hold"
    if "delivery attempt" in desc_lower or "unable to deliver" in desc_lower:
        return "Delivery Attempted"
    if "return" in desc_lower:
        return "Returned to Sender"

    status_mapping = {
        "DL": "Delivered", "OD": "Out for Delivery", "PU": "Picked Up",
        "IT": "In Transit", "AA": "In Transit", "AR": "In Transit",
        "DP": "In Transit", "AF": "In Transit", "PM": "In Transit",
        "DE": "Exception", "SE": "Exception", "OC": "Exception",
        "HL": "On Hold", "RS": "Returned to Sender", "CA": "Cancelled",
        "CD": "In Customs", "IN": "Label Created", "SP": "Label Created", "PL": "Label Created"
    }
    if status_code:
        code_upper = status_code.upper()
        if code_upper in status_mapping:
            return status_mapping[code_upper]
    return description[:30] if description else "Unknown"

def calculate_working_days(start_date, end_date):
    working_days = 0
    current = start_date
    while current < end_date:
        if current.weekday() < 5:
            working_days += 1
        current += timedelta(days=1)
    return working_days

def generate_sonia_analysis(track_data, status, is_delivered, delivery_date, ship_date, label_date):
    scan_events = track_data.get("scanEvents", [])
    history_parts = []
    for event in scan_events[:5]:
        event_desc = event.get("eventDescription", "")
        event_date = event.get("date", "")[:10] if event.get("date") else ""
        event_city = event.get("scanLocation", {}).get("city", "")
        if event_desc:
            if event_city:
                history_parts.append(f"{event_date}: {event_desc} ({event_city})")
            else:
                history_parts.append(f"{event_date}: {event_desc}")
    history_summary = " -> ".join(history_parts) if history_parts else "No scan history available"

    recommendation = ""
    today = datetime.now()

    if is_delivered:
        if delivery_date and ship_date:
            try:
                delivery_dt = datetime.strptime(delivery_date, "%Y-%m-%d")
                ship_dt = datetime.strptime(ship_date, "%Y-%m-%d")
                transit_days = (delivery_dt - ship_dt).days
                if transit_days <= 2:
                    recommendation = "Excelente tiempo de entrega! Paquete llego rapido."
                elif transit_days <= 5:
                    recommendation = "Buen tiempo de entrega dentro de lo esperado."
                else:
                    recommendation = "Entrega tomo mas tiempo de lo usual."
            except:
                recommendation = "Paquete entregado exitosamente."
        else:
            recommendation = "Paquete entregado exitosamente."
    elif "label created" in status.lower():
        if label_date:
            try:
                label_dt = datetime.strptime(label_date, "%Y-%m-%d")
                days_since_label = (today - label_dt).days
                if days_since_label > 5:
                    recommendation = f"ATENCION: {days_since_label} dias desde que se creo la etiqueta. Contactar al remitente."
                elif days_since_label > 2:
                    recommendation = "Etiqueta creada pero aun no recogida. Verificar con remitente."
                else:
                    recommendation = "Recien creada. Esperando recogida de FedEx."
            except:
                recommendation = "Esperando recogida de FedEx."
        else:
            recommendation = "Esperando recogida de FedEx."
    elif "in transit" in status.lower():
        if ship_date:
            try:
                ship_dt = datetime.strptime(ship_date, "%Y-%m-%d")
                days_in_transit = (today - ship_dt).days
                if days_in_transit > 7:
                    recommendation = f"ATENCION: {days_in_transit} dias en transito. Verificar retrasos."
                elif days_in_transit > 4:
                    recommendation = "Tiempo de transito extendido. Posible retraso en aduana."
                else:
                    recommendation = "Paquete moviendose normalmente en red FedEx."
            except:
                recommendation = "Paquete en transito al destino."
        else:
            recommendation = "Paquete en transito al destino."
    elif "out for delivery" in status.lower():
        recommendation = "Paquete en camino para entrega hoy!"
    elif "exception" in status.lower() or "hold" in status.lower():
        recommendation = "ACCION REQUERIDA: Paquete tiene una excepcion. Contactar FedEx."
    elif "customs" in status.lower() or "clearance" in status.lower():
        recommendation = "Paquete en proceso de aduana. Puede tomar varios dias."
    elif "delayed" in status.lower():
        recommendation = "ATENCION: Paquete retrasado. Monitorear de cerca."
    else:
        recommendation = "Monitorear envio para actualizaciones."

    return history_summary, recommendation

def parse_tracking_response(response, tracking_number):
    result = {
        "tracking_number": tracking_number, "sonia_status": "Unknown", "fedex_status": "",
        "history_summary": "", "sonia_recommendation": "", "label_creation_date": "",
        "ship_date": "", "delivery_date": "", "days_after_shipment": 0,
        "working_days_after_shipment": 0, "days_after_label_creation": 0,
        "destination_location": "", "is_delivered": False
    }
    try:
        if response and "output" in response:
            complete_track = response["output"].get("completeTrackResults", [])
            if complete_track:
                track_result = complete_track[0].get("trackResults", [])
                if track_result:
                    track_data = track_result[0]
                    latest_status = track_data.get("latestStatusDetail", {})
                    status_code = latest_status.get("code", "")
                    status_desc = latest_status.get("description", "")
                    result["sonia_status"] = get_short_status(status_code, status_desc)
                    result["fedex_status"] = status_desc if status_desc else status_code
                    result["is_delivered"] = "delivered" in result["sonia_status"].lower()

                    date_times = track_data.get("dateAndTimes", [])
                    for dt in date_times:
                        dt_type = dt.get("type", "")
                        dt_value = dt.get("dateTime", "")
                        if dt_value:
                            date_only = dt_value[:10]
                            if dt_type == "ACTUAL_PICKUP" or dt_type == "SHIP":
                                result["ship_date"] = date_only
                            elif dt_type == "ACTUAL_DELIVERY":
                                result["delivery_date"] = date_only
                                result["is_delivered"] = True

                    scan_events = track_data.get("scanEvents", [])
                    for event in reversed(scan_events):
                        event_desc = event.get("eventDescription", "").lower()
                        event_date = event.get("date", "")
                        if event_date and ("shipment information sent" in event_desc or "label created" in event_desc):
                            result["label_creation_date"] = event_date[:10]
                            break

                    if not result["ship_date"]:
                        for event in reversed(scan_events):
                            event_desc = event.get("eventDescription", "").lower()
                            event_date = event.get("date", "")
                            if event_date and ("picked up" in event_desc or "package received" in event_desc):
                                result["ship_date"] = event_date[:10]
                                break

                    dest = track_data.get("recipientInformation", {}).get("address", {})
                    if not dest:
                        dest = track_data.get("destinationLocation", {}).get("locationContactAndAddress", {}).get("address", {})
                    if dest:
                        city = dest.get("city", "")
                        state = dest.get("stateOrProvinceCode", "")
                        country = dest.get("countryCode", "")
                        result["destination_location"] = ", ".join([p for p in [city, state, country] if p])

                    today = datetime.now()
                    if result["ship_date"]:
                        try:
                            ship_dt = datetime.strptime(result["ship_date"], "%Y-%m-%d")
                            if result["is_delivered"] and result["delivery_date"]:
                                delivery_dt = datetime.strptime(result["delivery_date"], "%Y-%m-%d")
                                days = (delivery_dt - ship_dt).days
                                result["days_after_shipment"] = f"ENTREGADO EN {days} DIAS"
                                result["working_days_after_shipment"] = f"ENTREGADO EN {calculate_working_days(ship_dt, delivery_dt)} DIAS HABILES"
                            else:
                                result["days_after_shipment"] = (today - ship_dt).days
                                result["working_days_after_shipment"] = calculate_working_days(ship_dt, today)
                        except: pass

                    if result["label_creation_date"]:
                        try:
                            label_dt = datetime.strptime(result["label_creation_date"], "%Y-%m-%d")
                            if result["is_delivered"] and result["delivery_date"]:
                                delivery_dt = datetime.strptime(result["delivery_date"], "%Y-%m-%d")
                                result["days_after_label_creation"] = f"ENTREGADO EN {(delivery_dt - label_dt).days} DIAS"
                            else:
                                result["days_after_label_creation"] = (today - label_dt).days
                        except: pass

                    history, recommendation = generate_sonia_analysis(track_data, result["sonia_status"], result["is_delivered"], result["delivery_date"], result["ship_date"], result["label_creation_date"])
                    result["history_summary"] = history
                    result["sonia_recommendation"] = recommendation
    except Exception as e:
        logger.error(f"Error parsing response for {tracking_number}: {e}")
    return result

@app.get("/", response_class=HTMLResponse)
async def home():
    return """
<!DOCTYPE html>
<html>
<head>
    <title>SonIA Tracker - BloomsPal</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px; background: #f5f5f5; }
        .container { background: white; padding: 40px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        h1 { color: #4D148C; text-align: center; }
        .subtitle { text-align: center; color: #FF6600; margin-bottom: 30px; }
        .upload-form { text-align: center; }
        input[type="file"] { margin: 20px 0; padding: 10px; }
        button { background: #4D148C; color: white; padding: 15px 40px; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; }
        button:hover { background: #3a0f6a; }
        button:disabled { background: #ccc; cursor: not-allowed; }
        .progress-container { display: none; margin-top: 30px; }
        .progress-bar-bg { background: #e0e0e0; border-radius: 10px; height: 30px; overflow: hidden; }
        .progress-bar { background: linear-gradient(90deg, #4D148C, #FF6600); height: 100%; width: 0%; transition: width 0.3s ease; }
        .progress-text { text-align: center; margin-top: 10px; font-size: 18px; font-weight: bold; color: #4D148C; }
        .progress-details { text-align: center; margin-top: 5px; color: #666; }
        .result { margin-top: 20px; padding: 20px; background: #e8f5e9; border-radius: 5px; text-align: center; display: none; }
        .error { background: #ffebee; color: #c62828; }
        .success-icon { font-size: 48px; margin-bottom: 10px; }
        .info-note { text-align: center; color: #666; font-size: 12px; margin-top: 20px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>SonIA Tracker</h1>
        <p class="subtitle">BloomsPal - FedEx Tracking</p>
        <div class="upload-form">
            <input type="file" id="fileInput" accept=".xlsx,.xls"><br>
            <button id="processBtn" onclick="processFile()">Procesar Archivo</button>
        </div>
        <div class="progress-container" id="progressContainer">
            <div class="progress-bar-bg"><div class="progress-bar" id="progressBar"></div></div>
            <div class="progress-text" id="progressText">0%</div>
            <div class="progress-details" id="progressDetails">Iniciando...</div>
        </div>
        <div class="result" id="result"></div>
        <p class="info-note">Soporta archivos con +3000 guias. Token se refresca automaticamente.</p>
    </div>
    <script>
        async function processFile() {
            var fileInput = document.getElementById("fileInput");
            var progressContainer = document.getElementById("progressContainer");
            var progressBar = document.getElementById("progressBar");
            var progressText = document.getElementById("progressText");
            var progressDetails = document.getElementById("progressDetails");
            var result = document.getElementById("result");
            var processBtn = document.getElementById("processBtn");
            if (!fileInput.files[0]) { alert("Por favor selecciona un archivo Excel"); return; }
            progressContainer.style.display = "block";
            result.style.display = "none";
            processBtn.disabled = true;
            progressBar.style.width = "0%";
            progressText.textContent = "0%";
            progressDetails.textContent = "Subiendo archivo...";
            var formData = new FormData();
            formData.append("file", fileInput.files[0]);
            try {
                var startResponse = await fetch("/start-process", { method: "POST", body: formData });
                var startData = await startResponse.json();
                if (!startData.job_id) { throw new Error(startData.error || "Error al iniciar"); }
                var jobId = startData.job_id;
                var totalGuias = startData.total;
                progressDetails.textContent = "Procesando " + totalGuias + " guias...";
                var completed = false;
                while (!completed) {
                    await new Promise(r => setTimeout(r, 1000));
                    var progressResponse = await fetch("/progress/" + jobId);
                    var progressData = await progressResponse.json();
                    var percent = progressData.percent || 0;
                    var current = progressData.current || 0;
                    var total = progressData.total || totalGuias;
                    progressBar.style.width = percent + "%";
                    progressText.textContent = percent + "%";
                    progressDetails.textContent = "Procesando guia " + current + " de " + total;
                    if (progressData.status === "completed") {
                        completed = true;
                        progressBar.style.width = "100%";
                        progressText.textContent = "100%";
                        progressDetails.textContent = "Generando Excel...";
                        var resultResponse = await fetch("/result/" + jobId);
                        var resultData = await resultResponse.json();
                        if (resultData.success) {
                            var link = document.createElement("a");
                            link.href = "data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64," + resultData.file;
                            link.download = "SonIA_Tracking_Results.xlsx";
                            link.click();
                            result.style.display = "block";
                            result.className = "result";
                            result.innerHTML = "<div class='success-icon'>✅</div><p><strong>SonIA proceso " + total + " guias!</strong></p>";
                        } else { throw new Error(resultData.error); }
                    } else if (progressData.status === "error") { throw new Error(progressData.error || "Error"); }
                }
            } catch (error) {
                result.style.display = "block";
                result.className = "result error";
                result.innerHTML = "<p>Error: " + error.message + "</p>";
            } finally {
                setTimeout(function() { progressContainer.style.display = "none"; }, 1000);
                processBtn.disabled = false;
            }
        }
    </script>
</body>
</html>
"""

@app.post("/start-process")
async def start_process(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        df = pd.read_excel(BytesIO(contents), skiprows=[0], dtype={14: str, 'HAWB': str})
        tracking_col = None
        client_col = None
        for col in df.columns:
            col_upper = str(col).upper()
            if 'HAWB' in col_upper: tracking_col = col
            elif 'CLIENTE' in col_upper: client_col = col
        if tracking_col is None and len(df.columns) > 14: tracking_col = df.columns[14]
        if client_col is None and len(df.columns) > 2: client_col = df.columns[2]
        if tracking_col is None:
            return JSONResponse({"success": False, "error": "No se encontro columna HAWB"})
        tracking_list = []
        for idx, row in df.iterrows():
            raw = row[tracking_col] if pd.notna(row[tracking_col]) else ""
            tracking_number = str(raw).strip()
            if tracking_number.endswith('.0'): tracking_number = tracking_number[:-2]
            client_name = str(row[client_col]).strip() if client_col and pd.notna(row[client_col]) else ""
            if tracking_number and tracking_number != "nan" and tracking_number.isdigit():
                tracking_list.append({"tracking": tracking_number, "client": client_name})
        if not tracking_list:
            return JSONResponse({"success": False, "error": "No se encontraron trackings validos"})
        job_id = str(uuid.uuid4())
        jobs[job_id] = {"status": "processing", "total": len(tracking_list), "current": 0, "percent": 0, "tracking_list": tracking_list, "results": [], "error": None}
        asyncio.create_task(process_tracking_job(job_id))
        return JSONResponse({"job_id": job_id, "total": len(tracking_list)})
    except Exception as e:
        logger.error(f"Error starting: {e}")
        return JSONResponse({"success": False, "error": str(e)})

async def process_tracking_job(job_id: str):
    try:
        job = jobs[job_id]
        client = FedExClient()
        tracking_list = job["tracking_list"]
        total = len(tracking_list)
        logger.info(f"Starting job {job_id} with {total} trackings")
        for i, item in enumerate(tracking_list):
            tracking_number = item["tracking"]
            client_name = item["client"]
            try:
                response = await client.track_shipment(tracking_number)
                parsed = parse_tracking_response(response, tracking_number)
                parsed["client_name"] = client_name
                job["results"].append(parsed)
            except Exception as e:
                logger.error(f"Error {tracking_number}: {e}")
                job["results"].append({"tracking_number": tracking_number, "client_name": client_name, "sonia_status": "Error", "fedex_status": "Error", "sonia_recommendation": str(e)[:50], "history_summary": "", "label_creation_date": "", "ship_date": "", "delivery_date": "", "days_after_shipment": 0, "working_days_after_shipment": 0, "days_after_label_creation": 0, "destination_location": ""})
            job["current"] = i + 1
            job["percent"] = int(((i + 1) / total) * 100)
            await asyncio.sleep(0.05)
            if (i + 1) % 100 == 0: logger.info(f"Job {job_id}: {i + 1}/{total}")
        job["status"] = "completed"
        logger.info(f"Job {job_id} completed")
    except Exception as e:
        logger.error(f"Fatal error job {job_id}: {e}")
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)

@app.get("/progress/{job_id}")
async def get_progress(job_id: str):
    if job_id not in jobs:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    job = jobs[job_id]
    return JSONResponse({"status": job["status"], "total": job["total"], "current": job["current"], "percent": job["percent"], "error": job.get("error")})

@app.get("/result/{job_id}")
async def get_result(job_id: str):
    if job_id not in jobs:
        return JSONResponse({"success": False, "error": "Job not found"})
    job = jobs[job_id]
    if job["status"] != "completed":
        return JSONResponse({"success": False, "error": "Job not completed"})
    try:
        results = job["results"]
        output_df = pd.DataFrame(results)
        output_df = output_df[["client_name", "tracking_number", "sonia_status", "fedex_status", "label_creation_date", "ship_date", "days_after_shipment", "working_days_after_shipment", "days_after_label_creation", "destination_location", "history_summary", "sonia_recommendation"]]
        output_df.columns = ["Nombre Cliente", "FEDEX Tracking", "SonIA status", "FedEx status", "Label Creation Date", "Shipping Date", "Days After Shipment", "Working Days After Shipment", "Days After Label Creation", "Destination City/State/Country", "Historial", "SonIA Recomendacion"]
        output = BytesIO()
        output_df.to_excel(output, index=False)
        output.seek(0)
        encoded = base64.b64encode(output.read()).decode()
        del jobs[job_id]
        return JSONResponse({"success": True, "file": encoded})
    except Exception as e:
        logger.error(f"Error generating result: {e}")
        return JSONResponse({"success": False, "error": str(e)})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
