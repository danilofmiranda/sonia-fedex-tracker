# SonIA Tracker - FedEx Tracking API with Beautiful UI for BloomsPal
# Full redesign with BloomsPal colors and SonIA logo

import os
import httpx
import base64
import uuid
import asyncio
import logging
import pandas as pd
from datetime import datetime, timedelta
from io import BytesIO
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="SonIA Tracker - BloomsPal")

# In-memory job storage
jobs = {}

# FedEx API Credentials
FEDEX_API_KEY = os.getenv("FEDEX_API_KEY")
FEDEX_SECRET_KEY = os.getenv("FEDEX_SECRET_KEY")
FEDEX_BASE_URL = "https://apis.fedex.com"

class FedExClient:
    def __init__(self):
        self.access_token = None
        self.token_expires_at = None

    async def get_token(self):
        """Obtain or reuse an OAuth token with expiry based on expires_in"""
        if self.access_token and self.token_expires_at:
            # Add 5-minute buffer before expiry
            if datetime.now() < self.token_expires_at - timedelta(minutes=5):
                return self.access_token

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{FEDEX_BASE_URL}/oauth/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": FEDEX_API_KEY,
                    "client_secret": FEDEX_SECRET_KEY
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            if response.status_code == 200:
                data = response.json()
                self.access_token = data["access_token"]
                # Use expires_in from FedEx response (typically 3600 seconds = 1 hour)
                expires_in = data.get("expires_in", 3600)
                self.token_expires_at = datetime.now() + timedelta(seconds=expires_in)
                logger.info(f"New FedEx token obtained, expires in {expires_in} seconds")
                return self.access_token
            else:
                logger.error(f"Token error: {response.text}")
                raise Exception("Failed to obtain FedEx token")

    async def track_shipment(self, tracking_number: str, retry_count: int = 0):
        """Track a shipment with retry logic for 401/429 errors"""
        token = await self.get_token()

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{FEDEX_BASE_URL}/track/v1/shipments",
                json={
                    "includeDetailedScans": True,
                    "trackingInfo": [{"trackingNumberInfo": {"trackingNumber": tracking_number}}]
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"
                },
                timeout=30.0
            )

            # Handle 401 (Unauthorized) or 429 (Rate Limit) with retry
            if response.status_code in [401, 429] and retry_count < 3:
                if response.status_code == 401:
                    self.access_token = None
                    self.token_expires_at = None
                wait_time = 2 ** retry_count
                logger.warning(f"Got {response.status_code}, retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)
                return await self.track_shipment(tracking_number, retry_count + 1)

            return response.json()

def calculate_working_days(start_date, end_date):
    """Calculate working days (Mon-Fri) between two dates"""
    working_days = 0
    current = start_date
    while current < end_date:
        if current.weekday() < 5:
            working_days += 1
        current += timedelta(days=1)
    return working_days

def normalize_sonia_status(fedex_status: str) -> str:
    """Normalize FedEx status to standard SonIA status"""
    fedex_status_lower = fedex_status.lower() if fedex_status else ""

    # Delivered
    if any(s in fedex_status_lower for s in ["delivered"]):
        return "ENTREGADO"

    # In Transit / On the Way
    if any(s in fedex_status_lower for s in ["in transit", "on the way", "in-transit", "in_transit", "en transito", "departed", "arrived", "at local", "on fedex", "left fedex", "out for", "on vehicle", "customs", "brokerage", "clearance", "international", "transfer"]):
        return "EN CAMINO"

    # Label Created
    if any(s in fedex_status_lower for s in ["label created", "shipment information sent", "pickup", "picked up", "pending", "initiated"]):
        return "RECOGIDO"

    # Delayed
    if any(s in fedex_status_lower for s in ["delay", "hold", "exception", "problem", "issue", "undeliverable"]):
        return "RETRASADO"

    return "ESTADO DESCONOCIDO"

def generate_sonia_analysis(track_data: dict, sonia_status: str, is_delivered: bool, delivery_date: str, ship_date: str, label_creation_date: str = None):
    """Generate history summary and recommendation for SonIA"""
    scan_events = track_data.get("scanEvents", [])
    history_parts = []
    for event in scan_events[:5]:
        event_date = event.get("date", "")[:10]
        event_desc = event.get("eventDescription", "")
        location = event.get("scanLocation", {})
        city = location.get("city", "")
        if event_desc:
            history_parts.append(f"{event_date} - {event_desc}{' (' + city + ')' if city else ''}")

    history_summary = " | ".join(history_parts) if history_parts else "Sin eventos registrados"

    # Recommendation
    if sonia_status == "ENTREGADO":
        recommendation = f"Entregado exitosamente el {delivery_date}. No requiere accion."
    elif sonia_status == "RETRASADO":
        recommendation = "Retraso detectado. Verificar con FedEx para mas detalles."
    elif sonia_status == "RECOGIDO":
        recommendation = "Paquete recien recogido. Monitorear progreso."
    elif sonia_status == "EN CAMINO":
        recommendation = "Paquete en transito normal. Esperar actualizaciones."
    else:
        recommendation = "Estado no reconocido. Verificar manualmente."

    return history_summary, recommendation

def parse_tracking_response(response: dict, tracking_number: str):
    """Parse the FedEx API response and extract relevant data"""
    result = {
        "tracking_number": tracking_number,
        "sonia_status": "ESTADO DESCONOCIDO",
        "fedex_status": "",
        "label_creation_date": "",
        "ship_date": "",
        "delivery_date": "",
        "is_delivered": False,
        "days_after_shipment": "",
        "working_days_after_shipment": "",
        "days_after_label_creation": "",
        "destination_location": "",
        "history_summary": "",
        "sonia_recommendation": ""
    }

    try:
        output = response.get("output", {})
        packages = output.get("completeTrackResults", [])

        if packages:
            track_results = packages[0].get("trackResults", [])
            if track_results:
                track_data = track_results[0]

                # FedEx status
                latest_status = track_data.get("latestStatusDetail", {})
                result["fedex_status"] = latest_status.get("statusByLocale", latest_status.get("description", ""))

                # Normalize to SonIA status
                result["sonia_status"] = normalize_sonia_status(latest_status.get("statusByLocale", "") or latest_status.get("description", "") or track_data.get("packageDetails", {}).get("packagingDescription", {}).get("description", "")) if not result["fedex_status"] or result["fedex_status"] == "" else normalize_sonia_status(result["fedex_status"].lower()

                    # Get dates from dateAndTimes
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

                    # Get Label Creation Date from scan events
                    # Look for "Shipment information sent to FedEx" event
                    scan_events = track_data.get("scanEvents", [])
                    for event in reversed(scan_events):  # Start from oldest event
                        event_desc = event.get("eventDescription", "").lower()
                        event_date = event.get("date", "")
                        if event_date and ("shipment information sent" in event_desc or
                                          "label created" in event_desc or
                                          "shipping label" in event_desc):
                            result["label_creation_date"] = event_date[:10]
                            break

                    # Get Ship Date (Picked Up) from scan events if not found
                    if not result["ship_date"]:
                        for event in reversed(scan_events):
                            event_desc = event.get("eventDescription", "").lower()
                            event_date = event.get("date", "")
                            if event_date and ("picked up" in event_desc or "package received" in event_desc):
                                result["ship_date"] = event_date[:10]
                                break

                    # Destination
                    dest = track_data.get("recipientInformation", {}).get("address", {})
                    if not dest:
                        dest = track_data.get("destinationLocation", {}).get("locationContactAndAddress", {}).get("address", {})
                    if dest:
                        city = dest.get("city", "")
                        state = dest.get("stateOrProvinceCode", "")
                        country = dest.get("countryCode", "")
                        parts = [p for p in [city, state, country] if p]
                        result["destination_location"] = ", ".join(parts)

                    # Calculate days
                    today = datetime.now()

                    if result["ship_date"]:
                        try:
                            ship_dt = datetime.strptime(result["ship_date"], "%Y-%m-%d")
                            if result["is_delivered"] and result["delivery_date"]:
                                delivery_dt = datetime.strptime(result["delivery_date"], "%Y-%m-%d")
                                days_to_deliver = (delivery_dt - ship_dt).days
                                working_days_to_deliver = calculate_working_days(ship_dt, delivery_dt)
                                result["days_after_shipment"] = f"ENTREGADO EN {days_to_deliver} DIAS"
                                result["working_days_after_shipment"] = f"ENTREGADO EN {working_days_to_deliver} DIAS HABILES"
                            else:
                                result["days_after_shipment"] = (today - ship_dt).days
                                result["working_days_after_shipment"] = calculate_working_days(ship_dt, today)
                        except Exception as e:
                            logger.error(f"Error calculating ship days: {e}")

                    if result["label_creation_date"]:
                        try:
                            label_dt = datetime.strptime(result["label_creation_date"], "%Y-%m-%d")
                            if result["is_delivered"] and result["delivery_date"]:
                                delivery_dt = datetime.strptime(result["delivery_date"], "%Y-%m-%d")
                                result["days_after_label_creation"] = f"ENTREGADO EN {(delivery_dt - label_dt).days} DIAS"
                            else:
                                result["days_after_label_creation"] = (today - label_dt).days
                        except Exception as e:
                            logger.error(f"Error calculating label days: {e}")

                    history, recommendation = generate_sonia_analysis(
                        track_data, result["sonia_status"], result["is_delivered"],
                        result["delivery_date"], result["ship_date"], result["label_creation_date"]
                    )
                    result["history_summary"] = history
                    result["sonia_recommendation"] = recommendation

    except Exception as e:
        logger.error(f"Error parsing response: {e}")
        result["sonia_recommendation"] = f"Error procesando datos: {str(e)}"

    return result

@app.get("/", response_class=HTMLResponse)
async def home():
    return """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SonIA Tracker - BloomsPal</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #f8fafc 0%, #e2e8f0 100%);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
             align-items: center;
            justify-content: center;
            padding: 20px;
        }

        .container {
            background: white;
            border-radius: 24px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.15);
            padding: 48px;
            max-width: 520px;
            width: 100%;
            text-align: center;
        }

        .logo-container {
            margin-bottom: 24px;
        }

        .logo {
            width: 120px;
            height: 120px;
            margin: 0 auto;
        }

        /* SonIA Robot Logo SVG */
        .sonia-logo {
            fill: #4361EE;
        }

        h1 {
            font-size: 32px;
            font-weight: 700;
            color: #1e293b;
            margin-bottom: 8px;
            letter-spacing: -0.5px;
        }

        .brand-text {
            color: #4361EE;
        }

        .subtitle {
            color: #64748b;
            font-size: 16px;
            font-weight: 400;
            margin-bottom: 32px;
        }

        .subtitle span {
            color: #4361EE;
            font-weight: 600;
        }

        .upload-area {
            border: 2px dashed #cbd5e1;
            border-radius: 16px;
            padding: 40px 24px;
            margin-bottom: 24px;
            transition: all 0.3s ease;
            cursor: pointer;
            background: #f8fafc;
        }

        .upload-area:hover {
            border-color: #4361EE;
            background: #f0f4ff;
        }

        .upload-area.dragover {
            border-color: #4361EE;
            background: #e8edff;
            transform: scale(1.02);
        }

        .upload-icon {
            width: 64px;
            height: 64px;
            margin: 0 auto 16px;
            color: #4361EE;
        }

        .upload-text {
            color: #475569;
            font-size: 16px;
            margin-bottom: 8px;
        }

        .upload-hint {
            color: #94a3b8;
            font-size: 14px;
        }

        .file-name {
            display: none;
            background: #e8edff;
            color: #4361EE;
            padding: 12px 20px;
            border-radius: 12px;
            margin-bottom: 24px;
            font-weight: 500;
            font-size: 14px;
        }

        .file-name.visible {
            display: block;
        }

        .btn-primary {
            background: linear-gradient(135deg, #4361EE 0%, #3b52d4 100%);
            color: white;
            border: none;
            padding: 16px 48px;
            font-size: 16px;
            font-weight: 600;
            border-radius: 12px;
            cursor: pointer;
            transition: all 0.3s ease;
            width: 100%;
            letter-spacing: 0.3px;
        }

        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 20px -5px rgba(67, 97, 238, 0.4);
        }

        .btn-primary:active {
            transform: translateY(0);
        }

        .btn-primary:disabled {
            background: #cbd5e1;
            cursor: not-allowed;
            transform: none;
            box-shadow: none;
        }

        .progress-container {
            display: none;
            margin-top: 32px;
        }

        .progress-container.visible {
            display: block;
        }

        .progress-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
        }

        .progress-label {
            font-size: 14px;
            color: #475569;
            font-weight: 500;
        }

        .progress-percent {
            font-size: 24px;
            font-weight: 700;
            color: #4361EE;
        }

        .progress-bar-bg {
            background: #e2e8f0;
            border-radius: 100px;
            height: 12px;
            overflow: hidden;
        }

        .progress-bar {
            background: linear-gradient(90deg, #4361EE 0%, #7c3aed 100%);
            height: 100%;
            width: 0%;
            border-radius: 100px;
            transition: width 0.4s ease;
        }

        .progress-details {
            margin-top: 12px;
            color: #64748b;
            font-size: 14px;
        }

        .result {
            display: none;
            margin-top: 32px;
            padding: 24px;
            border-radius: 16px;
            text-align: center;
        }

        .result.visible {
            display: block;
        }

        .result.success {
            background: linear-gradient(135deg, #ecfdf5 0%, #d1fae5 100%);
            border: 1px solid #a7f3d0;
        }

        .result.error {
            background: linear-gradient(135deg, #fef2f2 0%, #fee2e2 100%);
            border: 1px solid #fecaca;
        }

        .result-icon {
            font-size: 48px;
            margin-bottom: 16px;
        }

        .result-text {
            font-size: 16px;
            font-weight: 600;
        }

        .result.success .result-text {
            color: #065f46;
        }

        .result.error .result-text {
            color: #991b1b;
        }

        .footer {
            margin-top: 24px;
            padding-top: 24px;
            border-top: 1px solid #e2e8f0;
        }

        .footer-text {
            color: #94a3b8;
            font-size: 13px;
        }

        .footer-text a {
            color: #4361EE;
            text-decoration: none;
            font-weight: 500;
        }

        .footer-text a:hover {
            text-decoration: underline;
        }

        /* Animations */
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.7; }
        }

        .processing .progress-label {
            animation: pulse 1.5s ease-in-out infinite;
        }

        /* Mobile responsive */
        @media (max-width: 480px) {
            .container {
                padding: 32px 24px;
            }

               h1 {
                font-size: 26px;
            }

            .upload-area {
                padding: 32px 16px;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="logo-container">
            <!-- SonIA Robot Logo -->
            <svg class="logo" viewBox="0 0 120 120" fill="none" xmlns="http://www.w3.org/2000/svg">
                <!-- Robot Head -->
                <rect x="25" y="35" width="70" height="55" rx="12" fill="#4361EE"/>
                <!-- Robot Eyes -->
                <circle cx="45" cy="58" r="8" fill="white"/>
                <circle cx="75" cy="58" r="8" fill="white"/>
                <circle cx="45" cy="58" r="4" fill="#1e293b"/>
                <circle cx="75" cy="58" r="4" fill="#1e293b"/>
                <!-- Robot Smile -->
                <path d="M45 75 Q60 85 75 75" stroke="white" stroke-width="3" stroke-linecap="round" fill="none"/>
                <!-- Antenna -->
                <rect x="56" y="20" width="8" height="18" rx="4" fill="#4361EE"/>
                <circle cx="60" cy="15" r="8" fill="#7c3aed"/>
                <!-- Flower/Petal on antenna -->
                <ellipse cx="60" cy="15" rx="5" ry="8" fill="#ec4899" transform="rotate(0 60 15)"/>
                <ellipse cx="60" cy="15" rx="5" ry="8" fill="#f472b6" transform="rotate(60 60 15)"/>
                <ellipse cx="60" cy="15" rx="5" ry="8" fill="#ec4899" transform="rotate(120 60 15)"/>
                <circle cx="60" cy="15" r="4" fill="#fbbf24"/>
                <!-- Robot Body Accent -->
                <rect x="35" y="95" width="50" height="8" rx="4" fill="#3b52d4"/>
            </svg>
        </div>

        <h1>Son<span class="brand-text">IA</span> Tracker</h1>
        <p class="subtitle">Rastreo inteligente de <span>BloomsPal</span></p>

        <div class="upload-area" id="uploadArea" onclick="document.getElementById('fileInput').click()">
            <svg class="upload-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                <polyline points="17 8 12 3 7 8"/>
                <line x1="12" y1="3" x2="12" y2="15"/>
            </svg>
            <p class="upload-text">Arrastra tu archivo Excel aquÃ­</p>
            <p class="upload-hint">o haz clic para seleccionar (.xlsx, .xls)</p>
            <input type="file" id="fileInput" accept=".xlsx,.xls" style="display: none;">
        </div>

        <div class="file-name" id="fileName"></div>

        <button class="btn-primary" id="processBtn" onclick="processFile()">
            Procesar Archivo
        </button>

        <div class="progress-container" id="progressContainer">
            <div class="progress-header">
                <span class="progress-label" id="progressLabel">Procesando guÃ­as...</span>
                <span class="progress-percent" id="progressPercent">0%</span>
            </div>
            <div class="progress-bar-bg">
                <div class="progress-bar" id="progressBar"></div>
            </div>
            <p class="progress-details" id="progressDetails">Iniciando...</p>
        </div>

        <div class="result" id="result">
            <div class="result-icon" id="resultIcon"></div>
            <p class="result-text" id="resultText"></p>
        </div>

        <div class="footer">
            <p class="footer-text">
                Powered by <a href="https://bloomspal.com" target="_blank">BloomsPal</a> â¢ FedEx Tracking API
            </p>
        </div>
    </div>

    <script>
        // Drag and drop functionality
        var uploadArea = document.getElementById('uploadArea');
        var fileInput = document.getElementById('fileInput');
        var fileName = document.getElementById('fileName');

        uploadArea.addEventListener('dragover', function(e) {
            e.preventDefault();
            uploadArea.classList.add('dragover');
        });

        uploadArea.addEventListener('dragleave', function(e) {
            e.preventDefault();
            uploadArea.classList.remove('dragover');
        });

        uploadArea.addEventListener('drop', function(e) {
            e.preventDefault();
            uploadArea.classList.remove('dragover');
            if (e.dataTransfer.files.length) {
                fileInput.files = e.dataTransfer.files;
                showFileName(e.dataTransfer.files[0].name);
            }
        });

        fileInput.addEventListener('change', function() {
            if (fileInput.files[0]) {
                showFileName(fileInput.files[0].name);
            }
        });

        function showFileName(name) {
            fileName.textContent = 'ð ' + name;
            fileName.classList.add('visible');
        }

        async function processFile() {
            var fileInput = document.getElementById('fileInput');
            var progressContainer = document.getElementById('progressContainer');
            var progressBar = document.getElementById('progressBar');
            var progressPercent = document.getElementById('progressPercent');
            var progressLabel = document.getElementById('progressLabel');
            var progressDetails = document.getElementById('progressDetails');
            var result = document.getElementById('result');
            var resultIcon = document.getElementById('resultIcon');
            var resultText = document.getElementById('resultText');
            var processBtn = document.getElementById('processBtn');

            if (!fileInput.files[0]) {
                alert('Por favor selecciona un archivo Excel');
                return;
            }

            progressContainer.classList.add('visible', 'processing');
            result.classList.remove('visible');
            processBtn.disabled = true;
            processBtn.textContent = 'Procesando...';
            progressBar.style.width = '0%';
            progressPercent.textContent = '0%';
                 progressDetails.textContent = 'Subiendo archivo...';

            var formData = new FormData();
            formData.append('file', fileInput.files[0]);

            try {
                var startResponse = await fetch('/start-process', { method: 'POST', body: formData });
                var startData = await startResponse.json();

                if (!startData.job_id) {
                    throw new Error(startData.error || 'Error al iniciar proceso');
                }

                var jobId = startData.job_id;
                var totalGuias = startData.total;
                progressLabel.textContent = 'Procesando ' + totalGuias + ' guÃ­as...';

                var completed = false;
                while (!completed) {
                    await new Promise(r => setTimeout(r, 500));

                    var progressResponse = await fetch('/progress/' + jobId);
                    var progressData = await progressResponse.json();

                    var percent = progressData.percent || 0;
                    var current = progressData.current || 0;
                    var total = progressData.total || totalGuias;

                    progressBar.style.width = percent + '%';
                    progressPercent.textContent = percent + '%';
                    progressDetails.textContent = 'GuÃ­a ' + current + ' de ' + total;

                    if (progressData.status === 'completed') {
                        completed = true;
                        progressBar.style.width = '100%';
                        progressPercent.textContent = '100%';
                        progressDetails.textContent = 'Generando reporte...';

                        var resultResponse = await fetch('/result/' + jobId);
                        var resultData = await resultResponse.json();

                        if (resultData.success) {
                            var link = document.createElement('a');
                            link.href = 'data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,' + resultData.file;
                            link.download = 'SonIA_Tracking_Results.xlsx';
                            link.click();

                            result.classList.add('visible', 'success');
                            result.classList.remove('error');
                            resultIcon.textContent = 'â';
                            resultText.textContent = 'SonIA procesÃ³ ' + total + ' guÃ­as exitosamente!';
                        } else {
                            throw new Error(resultData.error);
                        }
                    } else if (progressData.status === 'error') {
                        throw new Error(progressData.error || 'Error procesando archivo');
                    }
                }
            } catch (error) {
                result.classList.add('visible', 'error');
                result.classList.remove('success');
                resultIcon.textContent = 'â';
                resultText.textContent = 'Error: ' + error.message;
            } finally {
                setTimeout(function() {
                    progressContainer.classList.remove('visible', 'processing');
                }, 1000);
                processBtn.disabled = false;
                processBtn.textContent = 'Procesar Archivo';
            }
        }
    </script>
</body>
</html>
"""

@app.post("/start-process")
async def start_process(file: UploadFile = File(...)):
    """Start processing and return job_id for progress tracking"""
    try:
        contents = await file.read()
        df = pd.read_excel(BytesIO(contents), skiprows=[0], dtype={14: str, 'HAWB': str})

        tracking_col = None
        client_col = None
        for col in df.columns:
            col_upper = str(col).upper()
            if 'HAWB' in col_upper:
                tracking_col = col
            elif 'CLIENTE' in col_upper:
                client_col = col

        if tracking_col is None and len(df.columns) > 14:
            tracking_col = df.columns[14]
        if client_col is None and len(df.columns) > 2:
            client_col = df.columns[2]

        if tracking_col is None:
            return JSONResponse({"success": False, "error": "No se encontro la columna HAWB"})

        # Prepare tracking list
        tracking_list = []
        for idx, row in df.iterrows():
            raw_tracking = row[tracking_col] if pd.notna(row[tracking_col]) else ""
            tracking_number = str(raw_tracking).strip()
            if tracking_number.endswith('.0'):
                tracking_number = tracking_number[:-2]
            client_name = str(row[client_col]).strip() if client_col and pd.notna(row[client_col]) else ""
            if tracking_number and tracking_number != "nan" and tracking_number.isdigit():
                tracking_list.append({"tracking": tracking_number, "client": client_name})

        if not tracking_list:
            return JSONResponse({"success": False, "error": "No se encontraron numeros de tracking validos"})

        # Create job
        job_id = str(uuid.uuid4())
        jobs[job_id] = {
            "status": "processing",
            "total": len(tracking_list),
            "current": 0,
            "percent": 0,
            "tracking_list": tracking_list,
            "results": [],
            "error": None
        }

        # Start background processing
        asyncio.create_task(process_tracking_job(job_id))

        return JSONResponse({"job_id": job_id, "total": len(tracking_list)})

    except Exception as e:
        logger.error(f"Error starting process: {e}")
        return JSONResponse({"success": False, "error": str(e)})

async def process_tracking_job(job_id: str):
    """Background task to process tracking numbers"""
    try:
        job = jobs[job_id]
        client = FedExClient()
        tracking_list = job["tracking_list"]
        total = len(tracking_list)

        for i, item in enumerate(tracking_list):
            tracking_number = item["tracking"]
            client_name = item["client"]

            response = await client.track_shipment(tracking_number)
            parsed = parse_tracking_response(response, tracking_number)
            parsed["client_name"] = client_name
            job["results"].append(parsed)

            # Update progress
            job["current"] = i + 1
            job["percent"] = int(((i + 1) / total) * 100)

        job["status"] = "completed"

    except Exception as e:
        logger.error(f"Error in job {job_id}: {e}")
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)

@app.get("/progress/{job_id}")
async def get_progress(job_id: str):
    """Get current progress of a job"""
    if job_id not in jobs:
        return JSONResponse({"error": "Job not found"}, status_code=404)

    job = jobs[job_id]
    return JSONResponse({
        "status": job["status"],
        "total": job["total"],
        "current": job["current"],
        "percent": job["percent"],
        "error": job.get("error")
    })

@app.get("/result/{job_id}")
async def get_result(job_id: str):
    """Get the result Excel file for a completed job"""
    if job_id not in jobs:
        return JSONResponse({"success": False, "error": "Job not found"})

    job = jobs[job_id]
    if job["status"] != "completed":
        return JSONResponse({"success": False, "error": "Job not completed yet"})

    try:
        results = job["results"]
        output_df = pd.DataFrame(results)
        output_df = output_df[["client_name", "tracking_number", "sonia_status", "fedex_status", "label_creation_date", "ship_date", "days_after_shipment", "working_days_after_shipment", "days_after_label_creation", "destination_location", "history_summary", "sonia_recommendation"]]
        output_df.columns = ["Nombre Cliente", "FEDEX Tracking", "SonIA status", "FedEx status", "Label Creation Date", "Shipping Date", "Days After Shipment", "Working Days After Shipment", "Days After Label Creation", "Destination City/State/Country", "Historial", "SonIA Recomendacion"]

        output = BytesIO()
        output_df.to_excel(output, index=False)
        output.seek(0)
        encoded = base64.b64encode(output.read()).decode()

        # Clean up job after getting result
        del jobs[job_id]

        return JSONResponse({"success": True, "file": encoded})

    except Exception as e:
        logger.error(f"Error generating result: {e}")
        return JSONResponse({"success": False, "error": str(e)})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
