from fastapi import FastAPI, UploadFile, File
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
            response = await client.post(url, headers=headers, data=data)
            logger.info(f"Auth response status: {response.status_code}")
            if response.status_code == 200:
                self.access_token = response.json().get("access_token")
                logger.info("Authentication successful")
                return True
            else:
                logger.error(f"Auth failed: {response.text}")
                return False

    async def track_shipment(self, tracking_number):
        if not self.access_token:
            await self.authenticate()
        url = f"{self.base_url}/track/v1/trackingnumbers"
        headers = {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json", "X-locale": "en_US"}
        payload = {"includeDetailedScans": True, "trackingInfo": [{"trackingNumberInfo": {"trackingNumber": tracking_number}}]}
        logger.info(f"Tracking {tracking_number} at {url}")
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=payload)
            logger.info(f"Track response status: {response.status_code}")
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Track failed for {tracking_number}: {response.text}")
                return None

def get_short_status(status_code, description):
    status_mapping = {"PU": "Picked Up", "IT": "In Transit", "DL": "Delivered", "DE": "Delivery Exception", "OD": "Out for Delivery", "HL": "Hold at Location", "SE": "Shipment Exception", "CA": "Cancelled", "RS": "Return to Shipper"}
    if status_code in status_mapping:
        return status_mapping[status_code]
    desc_lower = description.lower() if description else ""
    if "delivered" in desc_lower: return "Delivered"
    elif "in transit" in desc_lower or "transit" in desc_lower: return "In Transit"
    elif "picked up" in desc_lower or "pickup" in desc_lower: return "Picked Up"
    elif "out for delivery" in desc_lower: return "Out for Delivery"
    elif "label" in desc_lower and "created" in desc_lower: return "Label Created"
    elif "exception" in desc_lower: return "Exception"
    elif "hold" in desc_lower: return "On Hold"
    elif "clearance" in desc_lower or "customs" in desc_lower: return "In Customs"
    elif "departed" in desc_lower or "arrived" in desc_lower: return "In Transit"
    return description[:20] if description else "Unknown"

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
                    recommendation = "Etiqueta creada pero aun no recogida."
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
        recommendation = "ACCION REQUERIDA: Paquete tiene una excepcion."
    elif "customs" in status.lower() or "clearance" in status.lower():
        recommendation = "Paquete en proceso de aduana."
    else:
        recommendation = "Monitorear envio para actualizaciones."
    return history_summary, recommendation


def parse_tracking_response(response, tracking_number):
    result = {"tracking_number": tracking_number, "status": "Unknown", "history_summary": "", "sonia_recommendation": "", "label_creation_date": "", "ship_date": "", "delivery_date": "", "days_after_shipment": 0, "working_days_after_shipment": 0, "days_after_label_creation": 0, "destination_location": "", "is_delivered": False}
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
                    result["status"] = get_short_status(status_code, status_desc)
                    result["is_delivered"] = "delivered" in result["status"].lower()
                    date_times = track_data.get("dateAndTimes", [])
                    for dt in date_times:
                        dt_type = dt.get("type", "")
                        dt_value = dt.get("dateTime", "")
                        if dt_value:
                            date_only = dt_value[:10]
                            if dt_type == "ACTUAL_PICKUP" or dt_type == "SHIP":
                                result["ship_date"] = date_only
                            elif dt_type == "ACTUAL_TENDER" or dt_type == "LABEL":
                                if not result["label_creation_date"]:
                                    result["label_creation_date"] = date_only
                            elif dt_type == "ACTUAL_DELIVERY":
                                result["delivery_date"] = date_only
                                result["is_delivered"] = True
                    dest = track_data.get("recipientInformation", {}).get("address", {})
                    if not dest:
                        dest = track_data.get("destinationLocation", {}).get("locationContactAndAddress", {}).get("address", {})
                    if dest:
                        city = dest.get("city", "")
                        state = dest.get("stateOrProvinceCode", "")
                        country = dest.get("countryCode", "")
                        parts = [p for p in [city, state, country] if p]
                        result["destination_location"] = ", ".join(parts)
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
                    history, recommendation = generate_sonia_analysis(track_data, result["status"], result["is_delivered"], result["delivery_date"], result["ship_date"], result["label_creation_date"])
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
    <html>
    <head>
        <title>FedEx Tracker - SonIA</title>
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
            .progress-bar { width: 100%; height: 30px; background: #e0e0e0; border-radius: 15px; overflow: hidden; }
            .progress-fill { height: 100%; background: linear-gradient(90deg, #4D148C, #FF6600); width: 0%; transition: width 0.3s ease; }
            .progress-text { text-align: center; margin-top: 10px; font-size: 18px; font-weight: bold; color: #4D148C; }
            .progress-detail { text-align: center; margin-top: 5px; font-size: 14px; color: #666; }
            .result { margin-top: 20px; padding: 20px; background: #e8f5e9; border-radius: 5px; text-align: center; display: none; }
            .error { background: #ffebee; color: #c62828; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>FedEx Tracker</h1>
            <p class="subtitle">SonIA - BloomsPal</p>
            <div class="upload-form">
                <input type="file" id="fileInput" accept=".xlsx,.xls">
                <br>
                <button id="processBtn" onclick="processFile()">Procesar Archivo</button>
            </div>
            <div class="progress-container" id="progressContainer">
                <div class="progress-bar">
                    <div class="progress-fill" id="progressFill"></div>
                </div>
                <div class="progress-text" id="progressText">0%</div>
                <div class="progress-detail" id="progressDetail">Iniciando...</div>
            </div>
            <div class="result" id="result"></div>
        </div>
        <script>
            async function processFile() {
                const fileInput = document.getElementById('fileInput');
                const progressContainer = document.getElementById('progressContainer');
                const progressFill = document.getElementById('progressFill');
                const progressText = document.getElementById('progressText');
                const progressDetail = document.getElementById('progressDetail');
                const result = document.getElementById('result');
                const processBtn = document.getElementById('processBtn');
                if (!fileInput.files[0]) { alert('Por favor selecciona un archivo Excel'); return; }
                progressContainer.style.display = 'block';
                result.style.display = 'none';
                progressFill.style.width = '0%';
                progressText.textContent = '0%';
                progressDetail.textContent = 'Subiendo archivo...';
                processBtn.disabled = true;
                const formData = new FormData();
                formData.append('file', fileInput.files[0]);
                try {
                    const response = await fetch('/process-with-progress', { method: 'POST', body: formData });
                    const reader = response.body.getReader();
                    const decoder = new TextDecoder();
                    let fileData = null;
                    while (true) {
                        const {value, done} = await reader.read();
                        if (done) break;
                        const text = decoder.decode(value);
                        const lines = text.split('\n');
                        for (const line of lines) {
                            if (line.startsWith('data: ')) {
                                try {
                                    const data = JSON.parse(line.slice(6));
                                    if (data.type === 'progress') {
                                        const percent = Math.round(data.percent);
                                        progressFill.style.width = percent + '%';
                                        progressText.textContent = percent + '%';
                                        progressDetail.textContent = 'Procesando ' + data.current + ' de ' + data.total + ' guias...';
                                    } else if (data.type === 'complete') {
                                        fileData = data.file;
                                        progressFill.style.width = '100%';
                                        progressText.textContent = '100%';
                                        progressDetail.textContent = 'Completado! ' + data.total + ' guias procesadas';
                                    } else if (data.type === 'error') {
                                        throw new Error(data.error);
                                    }
                                } catch (e) {}
                            }
                        }
                    }
                    if (fileData) {
                        const link = document.createElement('a');
                        link.href = 'data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,' + fileData;
                        link.download = 'FedEx_Tracking_Results.xlsx';
                        link.click();
                        result.style.display = 'block';
                        result.className = 'result';
                        result.innerHTML = '<p>SonIA ha procesado tu archivo exitosamente!</p>';
                    }
                } catch (error) {
                    result.style.display = 'block';
                    result.className = 'result error';
                    result.innerHTML = '<p>Error: ' + error.message + '</p>';
                } finally {
                    processBtn.disabled = false;
                }
            }
        </script>
    </body>
    </html>
    """


@app.post("/process-with-progress")
async def process_file_with_progress(file: UploadFile = File(...)):
    async def generate():
        try:
            contents = await file.read()
            df = pd.read_excel(BytesIO(contents), skiprows=[0], dtype={14: str, 'HAWB': str})
            logger.info(f"Processing file with {len(df)} rows")
            tracking_col = None
            client_col = None
            for col in df.columns:
                col_upper = str(col).upper()
                if 'HAWB' in col_upper: tracking_col = col
                elif 'CLIENTE' in col_upper: client_col = col
            if tracking_col is None and len(df.columns) > 14: tracking_col = df.columns[14]
            if client_col is None and len(df.columns) > 2: client_col = df.columns[2]
            if tracking_col is None:
                yield f"data: {json.dumps({'type': 'error', 'error': 'No se encontro la columna HAWB'})}\n\n"
                return
            valid_rows = []
            for idx, row in df.iterrows():
                raw_tracking = row[tracking_col] if pd.notna(row[tracking_col]) else ""
                tracking_number = str(raw_tracking).strip()
                if tracking_number.endswith('.0'): tracking_number = tracking_number[:-2]
                client_name = str(row[client_col]).strip() if client_col and pd.notna(row[client_col]) else ""
                if tracking_number and tracking_number != "nan" and tracking_number.isdigit():
                    valid_rows.append((tracking_number, client_name))
            total = len(valid_rows)
            if total == 0:
                yield f"data: {json.dumps({'type': 'error', 'error': 'No se encontraron numeros de tracking validos'})}\n\n"
                return
            logger.info(f"Found {total} valid tracking numbers")
            client = FedExClient()
            results = []
            for i, (tracking_number, client_name) in enumerate(valid_rows):
                percent = ((i) / total) * 100
                yield f"data: {json.dumps({'type': 'progress', 'current': i+1, 'total': total, 'percent': percent})}\n\n"
                logger.info(f"Processing tracking: {tracking_number} ({i+1}/{total})")
                response = await client.track_shipment(tracking_number)
                parsed = parse_tracking_response(response, tracking_number)
                parsed["client_name"] = client_name
                results.append(parsed)
                await asyncio.sleep(0.05)
            logger.info(f"Processed {len(results)} tracking numbers successfully")
            output_df = pd.DataFrame(results)
            output_df = output_df[["client_name", "tracking_number", "status", "label_creation_date", "ship_date", "days_after_shipment", "working_days_after_shipment", "days_after_label_creation", "destination_location", "history_summary", "sonia_recommendation"]]
            output_df.columns = ["Nombre Cliente", "FEDEX Tracking", "Status", "Label Creation Date", "Shipping Date", "Days After Shipment", "Working Days After Shipment", "Days After Label Creation", "Destination City/State/Country", "Historial", "SonIA Recomendacion"]
            output = BytesIO()
            output_df.to_excel(output, index=False)
            output.seek(0)
            encoded = base64.b64encode(output.read()).decode()
            yield f"data: {json.dumps({'type': 'complete', 'total': total, 'file': encoded})}\n\n"
        except Exception as e:
            logger.error(f"Error processing file: {e}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
    return StreamingResponse(generate(), media_type="text/event-stream")

@app.post("/process")
async def process_file(file: UploadFile = File(...)):
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
            return JSONResponse({"success": False, "error": "No se encontro la columna HAWB"})
        client = FedExClient()
        results = []
        for idx, row in df.iterrows():
            raw_tracking = row[tracking_col] if pd.notna(row[tracking_col]) else ""
            tracking_number = str(raw_tracking).strip()
            if tracking_number.endswith('.0'): tracking_number = tracking_number[:-2]
            client_name = str(row[client_col]).strip() if client_col and pd.notna(row[client_col]) else ""
            if tracking_number and tracking_number != "nan" and tracking_number.isdigit():
                response = await client.track_shipment(tracking_number)
                parsed = parse_tracking_response(response, tracking_number)
                parsed["client_name"] = client_name
                results.append(parsed)
        if not results:
            return JSONResponse({"success": False, "error": "No se encontraron numeros de tracking validos"})
        output_df = pd.DataFrame(results)
        output_df = output_df[["client_name", "tracking_number", "status", "label_creation_date", "ship_date", "days_after_shipment", "working_days_after_shipment", "days_after_label_creation", "destination_location", "history_summary", "sonia_recommendation"]]
        output_df.columns = ["Nombre Cliente", "FEDEX Tracking", "Status", "Label Creation Date", "Shipping Date", "Days After Shipment", "Working Days After Shipment", "Days After Label Creation", "Destination City/State/Country", "Historial", "SonIA Recomendacion"]
        output = BytesIO()
        output_df.to_excel(output, index=False)
        output.seek(0)
        encoded = base64.b64encode(output.read()).decode()
        return JSONResponse({"success": True, "file": encoded})
    except Exception as e:
        logger.error(f"Error processing file: {e}")
        return JSONResponse({"success": False, "error": str(e)})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
