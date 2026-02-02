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
                return data
            else:
                logger.error(f"Track failed for {tracking_number}: {response.text}")
                return None


def get_short_status(status_code, description):
    """Convert status to short, concise format"""
    status_mapping = {
        "PU": "Picked Up",
        "IT": "In Transit",
        "DL": "Delivered",
        "DE": "Delivery Exception",
        "OD": "Out for Delivery",
        "HL": "Hold at Location",
        "SE": "Shipment Exception",
        "CA": "Cancelled",
        "RS": "Return to Shipper"
    }

    if status_code in status_mapping:
        return status_mapping[status_code]

    desc_lower = description.lower() if description else ""
    if "delivered" in desc_lower:
        return "Delivered"
    elif "in transit" in desc_lower or "transit" in desc_lower:
        return "In Transit"
    elif "picked up" in desc_lower or "pickup" in desc_lower:
        return "Picked Up"
    elif "out for delivery" in desc_lower:
        return "Out for Delivery"
    elif "label" in desc_lower and "created" in desc_lower:
        return "Label Created"
    elif "exception" in desc_lower:
        return "Exception"
    elif "hold" in desc_lower:
        return "On Hold"
    elif "clearance" in desc_lower or "customs" in desc_lower:
        return "In Customs"
    elif "departed" in desc_lower:
        return "In Transit"
    elif "arrived" in desc_lower:
        return "In Transit"

    return description[:20] if description else "Unknown"


def calculate_working_days(start_date, end_date):
    """Calculate working days (Monday-Friday only) between two dates"""
    working_days = 0
    current = start_date
    while current < end_date:
        if current.weekday() < 5:
            working_days += 1
        current += timedelta(days=1)
    return working_days

def generate_sonia_analysis(track_data, status, is_delivered, delivery_date, ship_date, label_date):
    """Generate intelligent SonIA analysis with history summary and recommendation"""
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
                    recommendation = "Excellent delivery time! Package arrived quickly."
                elif transit_days <= 5:
                    recommendation = "Good delivery time within standard expectations."
                else:
                    recommendation = "Delivery took longer than usual. Consider reviewing carrier performance."
            except:
                recommendation = "Package delivered successfully."
        else:
            recommendation = "Package delivered successfully."

    elif "label created" in status.lower():
        if label_date:
            try:
                label_dt = datetime.strptime(label_date, "%Y-%m-%d")
                days_since_label = (today - label_dt).days
                if days_since_label > 5:
                    recommendation = f"ATTENTION: {days_since_label} days since label created but not shipped. Contact shipper to confirm pickup."
                elif days_since_label > 2:
                    recommendation = "Label created but not yet picked up. Monitor for pickup confirmation."
                else:
                    recommendation = "Recently created. Awaiting FedEx pickup."
            except:
                recommendation = "Awaiting FedEx pickup."
        else:
            recommendation = "Awaiting FedEx pickup."

    elif "in transit" in status.lower():
        if ship_date:
            try:
                ship_dt = datetime.strptime(ship_date, "%Y-%m-%d")
                days_in_transit = (today - ship_dt).days
                if days_in_transit > 7:
                    recommendation = f"ATTENTION: {days_in_transit} days in transit. Check for delays or customs issues."
                elif days_in_transit > 4:
                    recommendation = "Extended transit time. Package may be in customs or experiencing delays."
                else:
                    recommendation = "Package moving through FedEx network normally."
            except:
                recommendation = "Package in transit to destination."
        else:
            recommendation = "Package in transit to destination."

    elif "out for delivery" in status.lower():
        recommendation = "Package out for delivery today! Expect delivery soon."

    elif "exception" in status.lower() or "hold" in status.lower():
        recommendation = "ACTION REQUIRED: Package has an exception. Contact FedEx or recipient to resolve."

    elif "customs" in status.lower() or "clearance" in status.lower():
        recommendation = "Package in customs clearance. May require additional documentation."

    else:
        recommendation = "Monitor shipment for updates."

    sonia_text = f"History: {history_summary} | SonIA: {recommendation}"
    return sonia_text

def parse_tracking_response(response, tracking_number):
    """Parse FedEx tracking response with all enhanced fields"""
    result = {
        "tracking_number": tracking_number,
        "status": "Unknown",
        "sonia_analysis": "",
        "label_creation_date": "",
        "ship_date": "",
        "delivery_date": "",
        "days_after_shipment": 0,
        "working_days_after_shipment": 0,
        "days_after_label_creation": 0,
        "destination_location": "",
        "is_delivered": False
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
                                result["days_after_shipment"] = f"DELIVERED IN {days_to_deliver} DAYS"
                                result["working_days_after_shipment"] = f"DELIVERED IN {working_days_to_deliver} WORKING DAYS"
                            else:
                                days_diff = (today - ship_dt).days
                                working_days = calculate_working_days(ship_dt, today)
                                result["days_after_shipment"] = days_diff
                                result["working_days_after_shipment"] = working_days
                        except Exception as e:
                            logger.error(f"Error calculating ship days: {e}")
                    else:
                        result["days_after_shipment"] = 0
                        result["working_days_after_shipment"] = 0

                    if result["label_creation_date"]:
                        try:
                            label_dt = datetime.strptime(result["label_creation_date"], "%Y-%m-%d")

                            if result["is_delivered"] and result["delivery_date"]:
                                delivery_dt = datetime.strptime(result["delivery_date"], "%Y-%m-%d")
                                days_label_to_delivery = (delivery_dt - label_dt).days
                                result["days_after_label_creation"] = f"DELIVERED IN {days_label_to_delivery} DAYS"
                            else:
                                result["days_after_label_creation"] = (today - label_dt).days
                        except Exception as e:
                            logger.error(f"Error calculating label days: {e}")

                    result["sonia_analysis"] = generate_sonia_analysis(
                        track_data,
                        result["status"],
                        result["is_delivered"],
                        result["delivery_date"],
                        result["ship_date"],
                        result["label_creation_date"]
                    )

    except Exception as e:
        logger.error(f"Error parsing response: {e}")
        result["sonia_analysis"] = f"Error processing tracking data: {str(e)}"

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

        # Skip first 2 rows (they are titles) - data starts at row 3 (index 2)
        df = pd.read_excel(BytesIO(contents), skiprows=[0, 1])

        logger.info(f"Processing file with {len(df)} rows (after skipping title rows)")
        logger.info(f"Columns: {df.columns.tolist()}")

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

        output_df = pd.DataFrame(results)
        output_df = output_df[[
            "client_name",
            "tracking_number",
            "status",
            "sonia_analysis",
            "label_creation_date",
            "ship_date",
            "days_after_shipment",
            "working_days_after_shipment",
            "days_after_label_creation",
            "destination_location"
        ]]

        output_df.columns = [
            "Nombre Cliente",
            "FEDEX Tracking",
            "Status",
            "SonIA",
            "Label Creation Date",
            "Shipping Date",
            "Days After Shipment",
            "Working Days After Shipment",
            "Days After Label Creation",
            "Destination City/State/Country"
        ]

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
