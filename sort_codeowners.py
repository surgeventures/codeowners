#!/usr/bin/env python3
"""
Sort CODEOWNERS file according to target structure:
1. Ignore patterns (no team assignments)
2. Catch-all patterns (broad patterns like *.ext)
3. Team-based patterns (grouped by underlying team, removing -be/-fe suffixes)

This script automatically processes .github/CODEOWNERS and uses an intermediate temp file
for atomic writes. It counts matching files for each section using git ls-files.

Usage (from repository root):
    python3 sort_codeowners.py

One-liner execution:
    python3 <(curl -s https://raw.githubusercontent.com/surgeventures/codeowners/main/sort_codeowners.py)

Requirements:
    - Python 3 (comes pre-installed on macOS)
    - Git repository (for file counting)

Note: After editing CODEOWNERS, you must run this script before committing.
CI will verify that the file matches the sorted version.
"""

import re
import sys
import subprocess
import fnmatch
from collections import defaultdict
from typing import List, Tuple


def normalize_team_name(team: str) -> str:
    """
    Normalize team name by removing -be and -fe suffixes.
    Examples:
        @surgeventures/team-apollo-be -> team-apollo
        @surgeventures/team-apollo-fe -> team-apollo
        @surgeventures/team-apollo -> team-apollo
        @surgeventures/staff-engineers-be -> staff-engineers
    """
    # Remove @surgeventures/ prefix if present
    if team.startswith('@surgeventures/'):
        team = team[len('@surgeventures/'):]
    
    # Remove -be or -fe suffix
    if team.endswith('-be'):
        team = team[:-3]
    elif team.endswith('-fe'):
        team = team[:-3]
    
    return team


def extract_teams_from_line(line: str) -> List[str]:
    """Extract all team names from a CODEOWNERS line."""
    # Match all @surgeventures/team-name patterns
    matches = re.findall(r'@surgeventures/([\w-]+)', line)
    return matches


def get_tracked_files() -> List[str]:
    """
    Get list of all tracked files in the git repository.
    Returns empty list if not in a git repo or if git command fails.
    """
    try:
        result = subprocess.run(
            ['git', 'ls-files'],
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip().split('\n') if result.stdout.strip() else []
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []


def pattern_matches_file(pattern: str, file_path: str) -> bool:
    """
    Check if a CODEOWNERS pattern matches a file path.
    CODEOWNERS patterns use gitignore-style matching.
    """
    pattern = pattern.strip()
    if not pattern:
        return False
    
    # Normalize file path - remove leading slash
    file_path = file_path.lstrip('/')
    
    # Handle patterns that start with /
    if pattern.startswith('/'):
        # Absolute pattern - must match from repo root
        pattern = pattern[1:]
        # Check exact match or prefix match
        if fnmatch.fnmatch(file_path, pattern):
            return True
        # Check if pattern ends with / and file_path starts with pattern
        if pattern.endswith('/') and file_path.startswith(pattern):
            return True
        # Check directory match
        if pattern.endswith('/*') and file_path.startswith(pattern[:-2]):
            return True
        return False
    else:
        # Relative pattern - can match anywhere in the path
        # Handle ** for recursive matching
        if '**' in pattern:
            # Convert ** to regex-style matching
            # Replace **/ with match-any-directory pattern
            regex_pattern = pattern.replace('**/', '**').replace('/**', '**')
            
            # Simple recursive matching: check if pattern matches any part of the path
            parts = regex_pattern.split('**')
            if len(parts) == 2:
                prefix, suffix = parts[0], parts[1]
                if prefix:
                    # Check if file starts with prefix
                    if not file_path.startswith(prefix):
                        # Check if prefix appears anywhere in path
                        idx = file_path.find(prefix)
                        if idx == -1:
                            return False
                        file_path = file_path[idx:]
                if suffix:
                    # Check if remaining path matches suffix
                    return fnmatch.fnmatch(file_path, prefix + '*' + suffix) if prefix else fnmatch.fnmatch(file_path, '*' + suffix)
                else:
                    return True
            else:
                # Multiple ** or complex pattern - use simpler matching
                # Replace ** with * for fnmatch
                simple_pattern = pattern.replace('**', '*')
                return fnmatch.fnmatch(file_path, simple_pattern) or any(
                    fnmatch.fnmatch(file_path[i:], simple_pattern) 
                    for i in range(len(file_path))
                )
        else:
            # No ** - use fnmatch directly
            return fnmatch.fnmatch(file_path, pattern) or any(
                fnmatch.fnmatch(file_path[i:], pattern) 
                for i in range(len(file_path))
            )


def count_matching_files(pattern: str, tracked_files: List[str]) -> int:
    """
    Count how many files match a given CODEOWNERS pattern.
    """
    if not tracked_files:
        return 0
    
    count = 0
    for file_path in tracked_files:
        if pattern_matches_file(pattern, file_path):
            count += 1
    return count


def is_catch_all_pattern(pattern: str) -> bool:
    """
    Determine if a pattern is a catch-all pattern.
    Catch-all patterns are typically:
    - File extension patterns like *.jpg, *.png
    - Very broad patterns that match many files
    """
    pattern = pattern.strip()
    
    # Patterns starting with *. are likely catch-all
    if pattern.startswith('*.'):
        return True
    
    # Very broad patterns
    if pattern in ['**/*', '*', '**']:
        return True
    
    return False


def parse_codeowners(content: str) -> Tuple[List[str], List[str], dict, List[str]]:
    """
    Parse CODEOWNERS content into:
    - ignore_patterns: lines without team assignments
    - catch_all_patterns: broad patterns with team assignments
    - team_patterns: dict mapping normalized team name to list of lines
    - comments: header comments and section dividers
    """
    ignore_patterns = []
    catch_all_patterns = []
    team_patterns = defaultdict(list)
    comments = []
    
    lines = content.split('\n')
    current_section = None
    
    for line in lines:
        stripped = line.strip()
        
        # Skip empty lines (we'll add them back later)
        if not stripped:
            continue
        
        # Collect comments
        if stripped.startswith('#'):
            comments.append(line)
            # Check if this is a section header
            if 'IGNORED' in stripped.upper() or 'IGNORE' in stripped.upper():
                current_section = 'ignore'
            elif 'CATCH ALL' in stripped.upper() or 'CATCH-ALL' in stripped.upper():
                current_section = 'catch_all'
            elif re.match(r'^#\s+[A-Z-]+', stripped):
                # Team section header like "# TEAM-APOLLO"
                current_section = 'team'
            continue
        
        # Parse actual CODEOWNERS rules
        # Format: pattern [@team1 @team2 ...]
        teams = extract_teams_from_line(line)
        
        if not teams:
            # No team assignment - this is an ignore pattern
            ignore_patterns.append(line)
        else:
            # Extract the pattern (first word before any @)
            pattern_part = line.split()[0] if line.split() else ''
            
            if is_catch_all_pattern(pattern_part):
                # Has team assignment but is a catch-all pattern
                catch_all_patterns.append(line)
            else:
                # Team-based pattern - group by normalized team name
                # Use the first team to determine grouping
                # (all teams on the line will be in the same normalized group)
                primary_team = teams[0]
                normalized_team = normalize_team_name(primary_team)
                team_patterns[normalized_team].append(line)
    
    return ignore_patterns, catch_all_patterns, team_patterns, comments


def sort_team_patterns(team_patterns: dict) -> List[Tuple[str, List[str]]]:
    """
    Sort team patterns by team name (case-insensitive).
    Returns list of (team_name, patterns) tuples.
    """
    sorted_teams = sorted(team_patterns.items(), key=lambda x: x[0].lower())
    return sorted_teams


def extract_pattern_from_line(line: str) -> str:
    """Extract the pattern (path) from a CODEOWNERS line."""
    # Pattern is everything before the first @ symbol
    parts = line.split('@')
    return parts[0].strip() if parts else line.strip()


def generate_output(ignore_patterns: List[str], catch_all_patterns: List[str],
                   team_patterns: List[Tuple[str, List[str]]], tracked_files: List[str]) -> str:
    """
    Generate sorted CODEOWNERS output with file counts.
    """
    output_lines = []
    
    # Header
    output_lines.append("# CODEOWNERS - Reorganized by Team")
    output_lines.append("#")
    output_lines.append("# Structure: Ignored paths first, then Catch-All rules, followed by Specific Team rules.")
    output_lines.append("# This ensures specific team assignments override global defaults.")
    output_lines.append("#")
    output_lines.append("# IMPORTANT: After editing this file, you MUST run the sort script to maintain proper structure.")
    output_lines.append("#")
    output_lines.append("# To sort this file (run from repository root):")
    output_lines.append("#   python3 <(curl -s https://raw.githubusercontent.com/surgeventures/codeowners/main/sort_codeowners.py)")
    output_lines.append("#")
    output_lines.append("# CI will automatically verify that this file matches the sorted version.")
    output_lines.append("# If you commit changes without running the script, CI will fail.")
    output_lines.append("#")
    
    # Count files for ignore patterns
    ignore_file_count = 0
    if ignore_patterns and tracked_files:
        for pattern_line in ignore_patterns:
            pattern = extract_pattern_from_line(pattern_line)
            ignore_file_count += count_matching_files(pattern, tracked_files)
    
    # Ignore patterns section
    if ignore_patterns:
        output_lines.append("# ********************************************************************************")
        if tracked_files:
            output_lines.append(f"# ********* IGNORED (generated files - no review required) ({len(ignore_patterns)} rules, {ignore_file_count} files) *********")
        else:
            output_lines.append(f"# ********* IGNORED (generated files - no review required) ({len(ignore_patterns)} rules) *********")
        output_lines.append("# ********************************************************************************")
        for pattern in sorted(ignore_patterns):
            output_lines.append(pattern)
        output_lines.append("")
    
    # Count files for catch-all patterns
    catch_all_file_count = 0
    if catch_all_patterns and tracked_files:
        for pattern_line in catch_all_patterns:
            pattern = extract_pattern_from_line(pattern_line)
            catch_all_file_count += count_matching_files(pattern, tracked_files)
    
    # Catch-all patterns section
    if catch_all_patterns:
        output_lines.append("# ********************************************************************************")
        if tracked_files:
            output_lines.append(f"# ********* CATCH ALL ({len(catch_all_patterns)} rules, {catch_all_file_count} files) *********")
        else:
            output_lines.append(f"# ********* CATCH ALL ({len(catch_all_patterns)} rules) *********")
        output_lines.append("# ********************************************************************************")
        for pattern in sorted(catch_all_patterns):
            output_lines.append(pattern)
        output_lines.append("")
    
    # Team-based patterns sections
    for team_name, patterns in team_patterns:
        # Format team name for section header (uppercase, replace hyphens with spaces)
        section_name = team_name.replace('-', ' ').upper()
        
        # Count files for this team
        team_file_count = 0
        if tracked_files:
            for pattern_line in patterns:
                pattern = extract_pattern_from_line(pattern_line)
                team_file_count += count_matching_files(pattern, tracked_files)
        
        output_lines.append("# ********************************************************************************")
        if tracked_files:
            output_lines.append(f"# {section_name} ({len(patterns)} rules, {team_file_count} files)")
        else:
            output_lines.append(f"# {section_name} ({len(patterns)} rules)")
        output_lines.append("# ********************************************************************************")
        
        # Sort patterns within team section
        for pattern in sorted(patterns):
            output_lines.append(pattern)
        output_lines.append("")
    
    return '\n'.join(output_lines)


def main():
    import os
    import shutil
    
    # Hardcoded paths
    CODEOWNERS_FILE = '.github/CODEOWNERS'
    CODEOWNERS_TMP = '.github/CODEOWNERS.tmp'
    
    # Read the CODEOWNERS file
    try:
        with open(CODEOWNERS_FILE, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"Error: File '{CODEOWNERS_FILE}' not found.")
        print("Make sure you're running this from the repository root.")
        sys.exit(1)
    except Exception as e:
        print(f"Error reading file: {e}")
        sys.exit(1)
    
    # Get tracked files for counting
    print("Getting tracked files from git...")
    tracked_files = get_tracked_files()
    if tracked_files:
        print(f"Found {len(tracked_files)} tracked files")
    else:
        print("Warning: Could not get tracked files. File counts will not be included.")
    
    # Parse the CODEOWNERS file
    ignore_patterns, catch_all_patterns, team_patterns_dict, comments = parse_codeowners(content)
    
    # Sort team patterns
    team_patterns = sort_team_patterns(team_patterns_dict)
    
    # Generate output
    print("Counting matching files...")
    output = generate_output(ignore_patterns, catch_all_patterns, team_patterns, tracked_files)
    
    # Write to temporary file first
    try:
        with open(CODEOWNERS_TMP, 'w', encoding='utf-8') as f:
            f.write(output)
    except Exception as e:
        print(f"Error writing temporary file: {e}")
        sys.exit(1)
    
    # Move temp file to actual location (atomic operation)
    try:
        shutil.move(CODEOWNERS_TMP, CODEOWNERS_FILE)
        print(f"Successfully sorted CODEOWNERS file: {CODEOWNERS_FILE}")
        print(f"  - Ignore patterns: {len(ignore_patterns)}")
        print(f"  - Catch-all patterns: {len(catch_all_patterns)}")
        print(f"  - Team sections: {len(team_patterns)}")
    except Exception as e:
        print(f"Error moving file: {e}")
        # Clean up temp file if move failed
        if os.path.exists(CODEOWNERS_TMP):
            os.remove(CODEOWNERS_TMP)
        sys.exit(1)


if __name__ == '__main__':
    main()