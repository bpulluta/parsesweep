#!/usr/bin/env python3
"""
Audit addresses in extracted JSON files for geocoding issues.

This script scans extracted permit JSON files and identifies addresses
that may fail geocoding, categorizes the issues, and suggests cleaned versions.
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Tuple
from collections import defaultdict
import argparse

# ANSI color codes
CYAN = "\033[96m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
RED = "\033[91m"
RESET = "\033[0m"


def identify_address_issues(address: str) -> List[str]:
    """
    Identify potential geocoding issues in an address.
    
    Args:
        address: The address string to analyze
        
    Returns:
        List of issue descriptions
    """
    if not address or not address.strip():
        return ["EMPTY_ADDRESS"]
    
    issues = []
    
    # Check for newlines
    if "\n" in address or "\r" in address:
        issues.append("NEWLINE")
    
    # Check for intersection description
    if "intersection of" in address.lower():
        issues.append("INTERSECTION")
    
    # Check for address ranges
    if re.search(r"\d+\s*-\s*\d+", address):
        issues.append("ADDRESS_RANGE")
    
    # Check for building/suite/floor info
    if re.search(r"(?:Bldg\.?|Building|Suite|Ste\.?|Floor|Fl\.?|Room|Rm\.?)\s+[\w\d\-\.]+", address, re.IGNORECASE):
        issues.append("BUILDING_INFO")
    
    # Check for Attn: info
    if "Attn:" in address or "ATTN:" in address:
        issues.append("ATTENTION_LINE")
    
    # Check for missing commas (street address without comma before city)
    # e.g., "123 Main St City, State" instead of "123 Main St, City, State"
    if re.match(r"^\d+\s+\w+.*?\s+[A-Z][a-z]+,?\s+[A-Z]{2}", address):
        parts = address.split(",")
        if len(parts) < 2:
            issues.append("MISSING_COMMA")
    
    # Check for very long addresses (likely multiple addresses or extra info)
    if len(address) > 150:
        issues.append("VERY_LONG")
    
    # Check if address has no numbers (likely descriptive only)
    if not any(c.isdigit() for c in address):
        issues.append("NO_NUMBERS")
    
    # Check for special characters that might confuse geocoding
    if any(char in address for char in ["#", "@", "*", "(", ")", "[", "]"]):
        issues.append("SPECIAL_CHARS")
    
    return issues if issues else ["OK"]


def clean_address_for_geocoding(address: str) -> str:
    """
    Clean and normalize address for better geocoding results.
    
    Args:
        address: Original address string
        
    Returns:
        Cleaned address string
    """
    if not address:
        return ""
    
    # Replace newlines with spaces
    address = address.replace("\n", " ").replace("\r", " ")
    
    # Normalize whitespace
    address = " ".join(address.split())
    
    # Handle intersection descriptions - return empty to signal use county
    if "intersection of" in address.lower():
        return "[USE COUNTY]"
    
    # Remove building/suite/floor/attention info
    address = re.sub(r",?\s*(?:Bldg\.?|Building|Suite|Ste\.?|Floor|Fl\.?|Room|Rm\.?)\s+[\w\d\-\.]+", "", address, flags=re.IGNORECASE)
    address = re.sub(r",?\s*Attn:\s*[^,]+", "", address, flags=re.IGNORECASE)
    
    # Handle address ranges like "1300-1700 Street" -> "1300 Street"
    address = re.sub(r"(\d+)\s*-\s*\d+\s+", r"\1 ", address)
    
    # Clean up any double commas or spaces
    address = re.sub(r"\s*,\s*,\s*", ", ", address)
    address = re.sub(r"\s+", " ", address)
    address = address.strip()
    
    return address


def analyze_json_file(file_path: Path) -> Dict:
    """
    Analyze a single JSON file for address issues.
    
    Args:
        file_path: Path to JSON file
        
    Returns:
        Dictionary with analysis results
    """
    try:
        with open(file_path, "r") as f:
            data = json.load(f)
        
        permit_details = data.get("data", {}).get("permitDetails", {})
        
        facility_name = permit_details.get("facilityName") or "Unknown"
        facility_address = permit_details.get("facilityAddress") or ""
        facility_county = permit_details.get("facilityCounty") or "Unknown"
        
        issues = identify_address_issues(facility_address)
        cleaned = clean_address_for_geocoding(facility_address)
        
        return {
            "file": file_path.name,
            "facility_name": facility_name,
            "original_address": facility_address,
            "county": facility_county,
            "issues": issues,
            "cleaned_address": cleaned,
            "has_issues": issues != ["OK"]
        }
        
    except Exception as e:
        return {
            "file": file_path.name,
            "facility_name": "ERROR",
            "original_address": "",
            "county": "",
            "issues": [f"ERROR: {str(e)}"],
            "cleaned_address": "",
            "has_issues": True
        }


def main():
    parser = argparse.ArgumentParser(
        description="Audit addresses in extracted permit JSON files for geocoding issues."
    )
    parser.add_argument(
        "extracted_dir",
        type=Path,
        nargs="?",
        default=Path("data/extracted"),
        help="Directory containing extracted JSON files (default: data/extracted)"
    )
    parser.add_argument(
        "--state",
        type=str,
        help="Analyze only files from a specific state directory"
    )
    parser.add_argument(
        "--show-all",
        action="store_true",
        help="Show all addresses, including those without issues"
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Save detailed report to JSON file"
    )
    
    args = parser.parse_args()
    
    if not args.extracted_dir.exists():
        print(f"{RED}Error: Directory not found: {args.extracted_dir}{RESET}")
        return 1
    
    # Find all JSON files
    if args.state:
        state_dir = args.extracted_dir / args.state
        if not state_dir.exists():
            print(f"{RED}Error: State directory not found: {state_dir}{RESET}")
            return 1
        json_files = list(state_dir.glob("*.json"))
        scope = f"{args.state}"
    else:
        json_files = list(args.extracted_dir.rglob("*.json"))
        scope = "all states"
    
    if not json_files:
        print(f"{YELLOW}Warning: No JSON files found in {args.extracted_dir}{RESET}")
        return 0
    
    print(f"{CYAN}Auditing {len(json_files)} addresses from {scope}...{RESET}\n")
    
    # Analyze all files
    results = []
    issue_counts = defaultdict(int)
    
    for json_file in sorted(json_files):
        result = analyze_json_file(json_file)
        results.append(result)
        
        # Count issue types
        for issue in result["issues"]:
            issue_counts[issue] += 1
    
    # Filter results if not showing all
    if not args.show_all:
        results = [r for r in results if r["has_issues"]]
    
    # Print results
    print(f"{CYAN}{'='*80}{RESET}")
    print(f"{CYAN}ADDRESS AUDIT RESULTS{RESET}")
    print(f"{CYAN}{'='*80}{RESET}\n")
    
    if results:
        for i, result in enumerate(results, 1):
            issues_str = ", ".join(result["issues"])
            color = YELLOW if result["has_issues"] else GREEN
            
            print(f"{color}[{i}] {result['file']}{RESET}")
            print(f"  Facility: {result['facility_name']}")
            print(f"  County: {result['county']}")
            print(f"  Issues: {issues_str}")
            print(f"  Original: {result['original_address'][:100]}{'...' if len(result['original_address']) > 100 else ''}")
            
            if result["cleaned_address"] != result["original_address"]:
                print(f"  {GREEN}Cleaned: {result['cleaned_address'][:100]}{'...' if len(result['cleaned_address']) > 100 else ''}{RESET}")
            
            print()
    else:
        print(f"{GREEN}✓ No address issues found!{RESET}\n")
    
    # Print summary statistics
    print(f"{CYAN}{'='*80}{RESET}")
    print(f"{CYAN}SUMMARY STATISTICS{RESET}")
    print(f"{CYAN}{'='*80}{RESET}\n")
    
    total_files = len(json_files)
    files_with_issues = len([r for r in results if r["has_issues"]])
    
    print(f"Total files analyzed: {total_files}")
    print(f"Files with address issues: {files_with_issues} ({files_with_issues/total_files*100:.1f}%)")
    print(f"Files without issues: {total_files - files_with_issues} ({(total_files - files_with_issues)/total_files*100:.1f}%)")
    print()
    
    print("Issue breakdown:")
    sorted_issues = sorted(issue_counts.items(), key=lambda x: x[1], reverse=True)
    for issue, count in sorted_issues:
        if issue != "OK":
            print(f"  {issue:20s}: {count:4d} ({count/total_files*100:5.1f}%)")
    
    # Save detailed report if requested
    if args.output:
        report_data = {
            "total_files": total_files,
            "files_with_issues": files_with_issues,
            "issue_counts": dict(issue_counts),
            "results": results
        }
        
        with open(args.output, "w") as f:
            json.dump(report_data, f, indent=2)
        
        print(f"\n{GREEN}✓ Detailed report saved to: {args.output}{RESET}")
    
    return 0


if __name__ == "__main__":
    exit(main())
