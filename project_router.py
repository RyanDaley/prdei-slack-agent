"""
AEC Project Router Utility.
Maps Slack dropdown select values to physical Google Doc Journal targets.
Accepts both raw Google Doc URLs or direct Document IDs for easier administration.
"""

import re

# 1. THE CENTRAL MAPPING DICTIONARY
# You can paste the FULL browser URL here or just the Document ID.
# The code will automatically extract what it needs.
PROJECT_JOURNAL_MAP = {
    "tahoe_backyard": "https://docs.google.com/document/d/1WPLNG3mj4XjTyb71LRLf6KzTizj4beoKtFTHdJFa3a8/edit?tab=t.0",
    "wood_energy_facility": "https://docs.google.com/document/d/1r_ea3QZfxog5WaENU77nvSY-LygmYJiVIe1Py_s_5Po/edit?tab=t.0",
    "8494_speckled": "https://docs.google.com/document/d/1EXn98v0Cem_9U9Jq6iFULPBtokFPBADFgZOkv9aUg5A/edit?tab=t.0"
}

PROJECT_DISPLAY_NAMES = {
    "tahoe_backyard": "Tahoe Backyard",
    "wood_energy_facility": "Wood Energy Facility",
    "8494_speckled": "8494 Speckled Ave",
}

def extract_id_from_url(input_string: str) -> str:
    """
    Helper function using regular expressions to safely pull the 
    Google Document ID out of a raw web browser URL.
    """
    if not input_string:
        return ""
        
    # Standard Google Doc URL regex pattern: .../document/d/[DOC_ID]/...
    match = re.search(r'/document/d/([a-zA-Z0-9-_]+)', input_string)
    if match:
        return match.group(1)
        
    # If the input doesn't contain a URL pattern, assume it's already a clean ID
    return input_string.strip()

def get_journal_id_for_project(project_value: str) -> str or None:
    """
    Given a project value from Slack (e.g. 'tahoe_backyard'), 
    looks up the document and returns a clean, API-ready Google Document ID.
    """
    raw_reference = PROJECT_JOURNAL_MAP.get(project_value)
    if not raw_reference:
        print(f"[ROUTER ERROR] No Google Doc mapped for project value: '{project_value}'")
        return None
        
    doc_id = extract_id_from_url(raw_reference)
    return doc_id


def get_project_display_name(project_value: str) -> str:
    return PROJECT_DISPLAY_NAMES.get(
        project_value,
        project_value.replace("_", " ").title(),
    )


if __name__ == "__main__":
    # Quick visual terminal test
    print("Testing Router Resolution Logic:")
    for project, raw in PROJECT_JOURNAL_MAP.items():
        resolved_id = get_journal_id_for_project(project)
        print(f"  ↳ Project: '{project}' -> Resolved Doc ID: '{resolved_id}'")