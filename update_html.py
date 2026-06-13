import json
import re
import os

if os.path.exists("data.json"):
    print("Updating HTML with latest screening data...")
    
    # Read the latest data
    with open("data.json", "r", encoding="utf-8") as f:
        latest_data = json.load(f)

    # Write data.json to the frontend repo root (same dir as index.html)
    # so the dashboard can fetch it asynchronously without needing GitHub Pages rebuild
    data_json_str = json.dumps(latest_data, indent=2)
    with open("data.json", "w", encoding="utf-8") as f:
        f.write(data_json_str)
    print(f"data.json written ({len(data_json_str)//1024} KB) for async dashboard loading")

    # Read the HTML file
    with open("index.html", "r", encoding="utf-8") as f:
        html_content = f.read()

    # Update the embedded LIVE_DATA with a more robust pattern
    data_str = json.dumps(latest_data, indent=2)
    
    # Find the start and end of LIVE_DATA
    start_pattern = r"const LIVE_DATA = "
    end_pattern = r";\s*\n\s*// Global variables"
    
    # Replace the embedded data using string replacement instead of regex
    start_marker = "const LIVE_DATA = "
    end_marker = ";\n        \n        // Global variables"
    
    start_idx = html_content.find(start_marker)
    if start_idx == -1:
        print("Could not find LIVE_DATA start marker")
        exit(1)
    
    end_idx = html_content.find(end_marker, start_idx)
    if end_idx == -1:
        print("Could not find LIVE_DATA end marker")
        exit(1)
    
    # Replace the data
    updated_html = (html_content[:start_idx + len(start_marker)] + 
                   data_str + 
                   html_content[end_idx:])

    # Write back to HTML
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(updated_html)

    print("HTML updated successfully via script!")
    print(f"Embedded {len(latest_data.get('golden_stocks', []))} Golden Stocks")
    print(f"Embedded {len(latest_data.get('volume_breakout_stocks', []))} Volume Breakout Stocks")
    print(f"Embedded {len(latest_data.get('w_pattern_stocks', []))} W-Pattern Stocks")
    print(f"Embedded {len(latest_data.get('elliott_wave_stocks', []))} Elliott Wave Stocks")
else:
    print("data.json not found, skipping HTML update.")
