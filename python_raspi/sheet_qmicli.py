# sheet_qmicli.py
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import csv
import io

# (same append/create helpers as before; qmi script will pass CSV strings already)
def get_service(spreadsheet_id):
    credentials = service_account.Credentials.from_service_account_file(
        "key.json", scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    service = build("sheets", "v4", credentials=credentials)
    return service

def append_row(csv_string, service, spreadsheet_id, sheet_name):
    reader = csv.reader(io.StringIO(csv_string))
    values = next(reader, [])

    body = {"values": [values]}

    result = (
        service.spreadsheets()
        .values()
        .append(
            spreadsheetId=spreadsheet_id,
            range=sheet_name,
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body=body,
        )
        .execute()
    )
    return result

def create_sheet(title, service, spreadsheet_id):
    try:
        request_body = {
            "requests": [{"addSheet": {"properties": {"title": title}}}]
        }

        response = (
            service.spreadsheets()
            .batchUpdate(spreadsheetId=spreadsheet_id, body=request_body)
            .execute()
        )

        sheet_id = response["replies"][0]["addSheet"]["properties"]["sheetId"]
        print(f"Sheet '{title}' created with ID: {sheet_id}")
        return sheet_id

    except HttpError as error:
        print(f"An error occurred: {error}")
        return None
