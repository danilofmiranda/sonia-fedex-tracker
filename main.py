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
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "X-locale": "en_US"
        }
        payload = {
            "includeDetailedScans": True,
            "trackingInfo": [{"trackingNumberInfo": {"trackingNumber": tracking_number}}]
        }

        logger.info(f"Tracking {tracking_number} at {url}")

        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=payload)
            logger.info(f"Track response status: {response.status_code}")

            if response.status_code == 200:
                data = response.json()
                logger.info(f"Track response: {data}")
                return data
            else:
                logger.error(f"Track failed for {tracking_number}: {response.text}")
                return None

def parse_tracking_response(response, tracking_number):
    result = {
        "tracking_number": tracking_number,
        "status": "Desconocido",
        "status_description": "",
        "label_creation_date": "",
        "ship_date": "",
        "days_after_shipment": "",
        "working_days_after_shipment": "",
        "days_after_label_creation": "",
        "origin_location": ""
    }

    try:
        if response and "output" in response:
            complete_track = response["output"].get("completeTrackResults", [])
            if complete_track:
                track_result = complete_track[0].get("trackResults", [])
                if track_result:
                    track_data = track_result[0]

                    # Status
                    latest_status = track_data.get("latestStatusDetail", {})
                    result["status"] = latest_status.get("description", "Desconocido")
                    result["status_description"] = latest_status.get("description", "")

                    # Dates
                    date_times = track_data.get("dateAndTimes", [])
                    for dt in date_times:
                        dt_type = dt.get("type", "")
                        dt_value = dt.get("dateTime", "")
                        if dt_type == "ACTUAL_PICKUP" or dt_type == "SHIP":
                            result["ship_date"] = dt_value[:10] if dt_value else ""
                        elif dt_type == "ACTUAL_TENDER":
                            if not result["label_creation_date"]:
                                result["label_creation_date"] = dt_value[:10] if dt_value else ""

                    # Ship date and label creation from shipment details
                    ship_details = track_data.get("shipmentDetails", {})
                    if not result["ship_date"]:
                        result["ship_date"] = ship_details.get("possessionStatus", {}).get("dateTime", "")[:10] if ship_details.get("possessionStatus", {}).get("dateTime") else ""

                    # Origin location
                    origin = track_data.get("originLocation", {}).get("locationContactAndAddress", {}).get("address", {})
                    if origin:
                        city = origin.get("city", "")
                        state = origin.get("stateOrProvinceCode", "")
                        country = origin.get("countryCode", "")
                        result["origin_location"] = f"{city}, {state}, {country}".strip(", ")

                    # Calculate days
                    if result["ship_date"]:
                        try:
                            ship_dt = datetime.strptime(result["ship_date"], "%Y-%m-%d")
                            today = datetime.now()
                            days_diff = (today - ship_dt).days
                            result["days_after_shipment"] = days_diff

                            # Working days calculation
                            working_days = 0
                            current = ship_dt
                            while current < today:
                                if current.weekday() < 5:
                                    working_days += 1
                                current += timedelta(days=1)
                            result["working_days_after_shipment"] = working_days
                        except:
                            pass

                    if result["label_creation_date"]:
                        try:
                            label_dt = datetime.strptime(result["label_creation_date"], "%Y-%m-%d")
                            today = datetime.now()
                            result["days_after_label_creation"] = (today - label_dt).days
                        except:
                            pass
    except Exception as e:
        logger.error(f"Error parsing response: {e}")

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
            .loading { display: none; text-align: center; margin-top: 20px; }
            .result { margin-top: 20px; padding: 20px; background: #e8f5e9; border-radius: 5px; text-align: center; display: none; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>FedEx Tracker</h1>
            <p class="subtitle">SonIA - BloomsPal</p>
            <div class="upload-form">
                <input type="file" id="fileInput" accept=".xlsx,.xls">
                <br>
                <button onclick="processFile()">Procesar Archivo</button>
            </div>
            <div class="loading" id="loading">
                <p>Procesando... Por favor espere.</p>
            </div>
            <div class="result" id="result">
                <p>SonIA ha procesado tu archivo exitosamente!</p>
            </div>
        </div>
        <script>
            async function processFile() {
                const fileInput = document.getElementById('fileInput');
                const loading = document.getElementById('loading');
                const result = document.getElementById('result');

                if (!fileInput.files[0]) {
                    alert('Por favor selecciona un archivo Excel');
                    return;
                }

                loading.style.display = 'block';
                result.style.display = 'none';

                const formData = new FormData();
                formData.append('file', fileInput.files[0]);

                try {
                    const response = await fetch('/process', {
                        method: 'POST',
                        body: formData
                    });

                    const data = await response.json();

                    if (data.success) {
                        // Download file
                        const link = document.createElement('a');
                        link.href = 'data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,' + data.file;
                        link.download = 'FedEx_Tracking_Results.xlsx';
                        link.click();

                        result.style.display = 'block';
                    } else {
                        alert('Error: ' + data.error);
                    }
                } catch (error) {
                    alert('Error procesando archivo: ' + error);
                } finally {
                    loading.style.display = 'none';
                }
            }
        </script>
    </body>
    </html>
    """

@app.post("/process")
async def process_file(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        df = pd.read_excel(BytesIO(contents))

        logger.info(f"Processing file with {len(df)} rows")
        logger.info(f"Columns: {df.columns.tolist()}")

        # Get tracking numbers from column O (index 14) and client names from column C (index 2)
        tracking_col = df.columns[14] if len(df.columns) > 14 else None
        client_col = df.columns[2] if len(df.columns) > 2 else None

        if tracking_col is None:
            return JSONResponse({"success": False, "error": "No se encontro la columna O con numeros de tracking"})

        client = FedExClient()
        results = []

        for idx, row in df.iterrows():
            tracking_number = str(row[tracking_col]).strip() if pd.notna(row[tracking_col]) else ""
            client_name = str(row[client_col]).strip() if client_col and pd.notna(row[client_col]) else ""

            if tracking_number and tracking_number != "nan":
                logger.info(f"Processing tracking: {tracking_number}")
                response = await client.track_shipment(tracking_number)
                parsed = parse_tracking_response(response, tracking_number)
                parsed["client_name"] = client_name
                results.append(parsed)

        # Create output DataFrame
        output_df = pd.DataFrame(results)
        output_df = output_df[[
            "client_name", "tracking_number", "status", "status_description",
            "label_creation_date", "ship_date", "days_after_shipment",
            "working_days_after_shipment", "days_after_label_creation", "origin_location"
        ]]
        output_df.columns = [
            "Nombre Cliente", "FEDEX Tracking", "Status", "Status Description",
            "Label Creation Date", "Shipping Date", "Days After Shipment",
            "Working Days After Shipment", "Days After Label Creation", "Shipping City/State/Country"
        ]

        # Add SonIA column
        output_df["SonIA - BloomsPal"] = "Procesado por SonIA"

        # Save to Excel
        output = BytesIO()
        output_df.to_excel(output, index=False)
        output.seek(0)

        # Encode to base64
        encoded = base64.b64encode(output.read()).decode()

        return JSONResponse({"success": True, "file": encoded})

    except Exception as e:
        logger.error(f"Error processing file: {e}")
        return JSONResponse({"success": False, "error": str(e)})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
