import time
print(f"[{time.strftime('%X')}] 1. Starting script...")

import os
import json
import traceback
from dotenv import load_dotenv

load_dotenv()
print(f"[{time.strftime('%X')}] 2. Standard imports loaded.")

from google.oauth2.service_account import Credentials
import gspread
from gspread_formatting import cellFormat, format_cell_range, set_column_width
print(f"[{time.strftime('%X')}] 3. Google Sheets API loaded.")

print(f"[{time.strftime('%X')}] 4. LangChain loaded.")

from graph import app
print(f"[{time.strftime('%X')}] 5. Custom Graph loaded.")

def format_sheet(worksheet):
    """Sets a clean layout: Clips long text and sets column widths."""
    print("--- APPLYING SHEET FORMATTING ---")
    
    clip_format = cellFormat(wrapStrategy='CLIP')
    
    # Applied 'Clip' up to column J to ensure new columns are covered
    format_cell_range(worksheet, 'A:J', clip_format)
    
    set_column_width(worksheet, 'B', 150) # Company
    set_column_width(worksheet, 'E', 400) # JD
    set_column_width(worksheet, 'G', 120) # Resume Status (Assuming G)
    set_column_width(worksheet, 'H', 120) # Cover Letter Status (Assuming H)

def listen_to_sheets():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_path = os.path.join(os.path.dirname(__file__), '..', 'google_credentials.json')
    creds = Credentials.from_service_account_file(creds_path, scopes=scope)
    client = gspread.authorize(creds)

    spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip()
    if spreadsheet_id:
        spreadsheet = client.open_by_key(spreadsheet_id)
    else:
        spreadsheet = client.open("Job_Application_Tracker")
    sheet = spreadsheet.sheet1
    
    # --- AUTO FORMAT ON START ---
    format_sheet(sheet)
    
    print("--- LISTENING FOR 'TRIGGER' STATUS ---")

    while True:
        try:
            # Dynamically map the columns by reading the header row (Row 1)
            # This prevents bugs if you ever rearrange your Google Sheet!
            headers = sheet.row_values(1)

            # gspread uses 1-based indexing (A=1, B=2, etc.)
            score_col = headers.index("Match Score") + 1 if "Match Score" in headers else 6
            resume_status_col = headers.index("Resume Status") + 1 if "Resume Status" in headers else 7
            cl_status_col = headers.index("Cover Letter Status") + 1 if "Cover Letter Status" in headers else 8

            # IMPROVEMENT 7: Ensure "Skill Gaps" column exists; add it if not.
            if "Skill Gaps" not in headers:
                new_col = len(headers) + 1
                sheet.update_cell(1, new_col, "Skill Gaps")
                headers = sheet.row_values(1)  # re-fetch after mutation
            skill_gaps_col = headers.index("Skill Gaps") + 1

            # Read fresh JSON every time (path is relative to this file, not CWD)
            ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
            master_data_path = os.path.join(ROOT_DIR, 'Ankush_Master_Data.json')
            with open(master_data_path, 'r') as f:
                master_data = json.load(f)
                
            # Re-fetch records
            records = sheet.get_all_records()        

            for i, row in enumerate(records, start=2):
                # Grab our new dual-status columns
                resume_status = str(row.get("Resume Status", "")).lower().strip()
                cl_status = str(row.get("Cover Letter Status", "")).lower().strip()
                
                company = row.get("Company", "Unknown")
                jd = row.get("Job Description", "")
                jt = row.get("Role", "Software Engineer")

                trigger_resume = (resume_status == "trigger")
                trigger_cl = (cl_status == "trigger")

                # If either one is triggered, we run the pipeline
                if trigger_resume or trigger_cl:
                    print(f"\nProcessing {company}...")
                    
                    # 1. Mark as processing in Sheet
                    if trigger_resume:
                        sheet.update_cell(i, resume_status_col, "processing")
                    if trigger_cl:
                        sheet.update_cell(i, cl_status_col, "processing")
                    
                    initial_state = {
                        "company_name": company,
                        "job_description": jd,
                        "job_title": jt,
                        "master_data": master_data,
                        "generate_resume": trigger_resume,
                        "generate_cover_letter": trigger_cl,
                        "jd_keywords": [],
                        "verified_keywords": [],
                        "gap_keywords": [],
                        "triage_score": None,
                        "triage_reasoning": None,
                        "proceed": False,
                        "selected_content": None,
                        "selection_report": None,
                        "trust_report": None,
                        "final_resume_data": None,
                        "resume_strategy": None,
                        "output_folder": "",
                        "pipeline_status": ""
                    }

                    # 2. Run Pipeline
                    result = app.invoke(initial_state)

                    # 3. Update Sheet with Match Score
                    if "triage_score" in result and result["triage_score"] is not None:
                        sheet.update_cell(i, score_col, result["triage_score"])

                    # IMPROVEMENT 7: Write gap keywords to "Skill Gaps" column
                    gap_kw = result.get("gap_keywords", [])
                    sheet.update_cell(i, skill_gaps_col, ", ".join(gap_kw) if gap_kw else "")

                    # 4. Mark as done or skipped depending on triage result
                    skipped = not result.get("proceed", True)
                    status_label = "skipped (<70)" if skipped else "done"
                    if trigger_resume:
                        sheet.update_cell(i, resume_status_col, status_label)
                    if trigger_cl:
                        sheet.update_cell(i, cl_status_col, status_label)
                        
                    print(f"Done with {company}.")

            time.sleep(30) # Check every 30 seconds
            
        except Exception as e:
            print(f"Error: {e}")
            traceback.print_exc()
            time.sleep(5)

if __name__ == "__main__":
    listen_to_sheets()