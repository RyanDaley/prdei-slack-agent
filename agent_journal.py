"""
AEC Agent Journal Integration.
Handles keyless Google Docs API authentication, formats raw notes using Gemini,
and appends structured activity logs directly to the designated Google Doc.
"""

import os
import datetime
import google.auth
from google import genai
from google.genai import types as genai_types
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- CONFIGURATION ---
# The same project configuration you established in your RAG metadata sandbox
PROJECT_ID = "prdei-ai-sandbox"
LOCATION = "us-west1"
GEMINI_MODEL = "gemini-2.5-flash"

# Document API Scope (Read & Write permissions)
SCOPES = ['https://www.googleapis.com/auth/documents']


def get_docs_service():
    """
    Authenticates securely using Application Default Credentials (ADC).
    Works seamlessly on local machines (via gcloud auth application-default login)
    and autonomously in Cloud Run without requiring raw JSON key files.
    """
    try:
        # Cloud Run service accounts should call Docs API without a forced quota
        # project; with_quota_project triggers serviceusage checks that fail on
        # the default compute service account even when IAM is correct.
        credentials, _ = google.auth.default(scopes=SCOPES)
        return build('docs', 'v1', credentials=credentials)
    except Exception as e:
        print(f"[AUTH ERROR] Failed to obtain Application Default Credentials: {e}")
        print(" -> To run locally, execute: gcloud auth application-default login")
        raise e


def format_entry_with_ai(user_name: str, task_category: str, raw_details: str) -> str:
    """
    Uses Gemini to format raw developer/architect updates into professional,
    high-quality project journal entries.
    """
    # Fallback to plain formatting if API client fails to initialize
    try:
        # Vertex AI billing/quota is tied to the GCP project, not the Docs client.
        os.environ["GOOGLE_CLOUD_QUOTA_PROJECT"] = PROJECT_ID
        ai_client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)
        
        system_instruction = """
        You are an expert technical writer and document controller for an architecture and engineering firm.
        Your task is to rewrite raw, informal daily updates into highly professional project journal log entries.
        
        Rules:
        1. Maintain a formal, objective, and technical tone.
        2. Clean up grammar, typos, and jargon.
        3. Keep the output concise but structured (maximum 2-3 sentences).
        4. Focus on the actual architectural/engineering work done (modeling, calculations, permitting, client coordination).
        5. DO NOT invent details, hours, or names that are not in the raw input.
        """
        
        user_prompt = f"""
        User: {user_name}
        Category: {task_category}
        Raw Update: "{raw_details}"
        
        Provide ONLY the polished activity description block. Do not add markdown headers, titles, or dates.
        """
        
        response = ai_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.2, # Low temperature for factual consistency
            ),
        )
        ai_summary = response.text.strip()
    except Exception as exc:
        print(f"  [AI WARNING] Gemini formatting failed, falling back to raw text. Error: {exc}")
        ai_summary = raw_details.strip()

    # Generate timestamp blocks
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %I:%M %p")
    clean_category = task_category.replace('_', ' ').title()
    
    # Standardize document entry block layout
    formatted_entry = (
        f"📅 Date/Time: {timestamp}\n"
        f"👤 Logged By: {user_name}\n"
        f"🏷️  Category: {clean_category}\n"
        f"📝 Activity: {ai_summary}\n"
        f"{'─'*60}\n\n"
    )
    return formatted_entry


def append_to_journal(document_id: str, user_name: str, task_category: str, raw_details: str) -> bool:
    """
    Appends a structured timesheet entry directly to the bottom of the specified Google Doc.
    """
    if not document_id:
        print("[JOURNAL ERROR] Cannot append: Document ID is empty.")
        return False

    try:
        # Initialize Google Docs Client
        service = get_docs_service()
        
        # 1. Format the text block using Gemini
        new_entry_text = format_entry_with_ai(user_name, task_category, raw_details)
        
        # 2. Get the current document metadata to find the end-of-file index
        doc_metadata = service.documents().get(documentId=document_id).execute()
        
        # In the Docs API, the final character index is represented by the end index of the body content
        body_content = doc_metadata.get('body', {}).get('content', [])
        if not body_content:
            print("[JOURNAL ERROR] Document body is empty or unreadable.")
            return False
            
        end_index = body_content[-1].get('endIndex', 1) - 1
        
        # 3. Create the write payload
        requests = [
            {
                'insertText': {
                    'location': {
                        'index': end_index,
                    },
                    'text': new_entry_text
                }
            }
        ]
        
        # 4. Push update live via Docs API
        service.documents().batchUpdate(
            documentId=document_id, 
            body={'requests': requests}
        ).execute()
        
        print(f"  [JOURNAL SUCCESS] Entry appended successfully to Doc ID: {document_id}")
        return True
        
    except HttpError as http_err:
        print(f"  [JOURNAL API ERROR] Google Docs API returned status {http_err.resp.status}: {http_err._get_reason()}")
        print("  -> Ensure your Cloud Run Service Account email has 'Editor' permission on the target Google Doc!")
        return False
    except Exception as exc:
        print(f"  [JOURNAL ERROR] Failed to append to Google Doc: {exc}")
        return False


if __name__ == "__main__":
    # Local dry-run test
    print("Testing formatting module locally...")
    test_log = format_entry_with_ai(
        user_name="rdaley",
        task_category="cad_modeling",
        raw_details="finished tracing the main property setbacks in model and aligned framing rafters to the rear roof profile"
    )
    print(test_log)