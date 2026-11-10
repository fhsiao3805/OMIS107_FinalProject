"""Turns bulletin HTML into courses and majors."""
import re
from collections import Counter

from bs4 import BeautifulSoup

SCHOOLS = {
    "3": "College of Arts and Sciences",
    "4": "Leavey School of Business",
    "5": "School of Engineering",
    "6": "University Programs",
}

NUM = r"\d{1,3}[A-Z]{0,2}"
SUBJECT = r"[A-Z]{3,4}"

# A number list like "9A and MATH 9B" or "151A. and B.", then a separator, then the title.
# A bare space only separates when a word follows; "." ":" or a dash also allow "20th-Century".
COURSE_HEADING = re.compile(
    rf"""^(?:({SUBJECT})\s+)?({NUM})((?:\.?\s*(?:and|&|,|/)\s*(?:(?:{SUBJECT}\s+)?{NUM}|[A-Z]\b(?=\.)))*)"""
    rf"""(?:\s*[.:]\s*(?=["“'‘(A-Z0-9])|\s+[–—-]\s+(?=["“'‘(A-Z0-9])|\s+(?=["“'‘(A-Z]))(.+)$"""
)
SPLIT_SUFFIX = re.compile(rf"^((?:{SUBJECT}\s+)?\d{{1,3}})\s+([A-Z])\.\s")
HEAD_REF = re.compile(rf"\b({SUBJECT})\s?({NUM})\b")
NEXT_ITEM = re.compile(rf"\s*(?:,\s*(?:and\s+|or\s+)?|and\s+|or\s+|&\s*|/\s*)({NUM})\b")
RANGE_END = re.compile(rf"\s*[–—-]\s*({NUM})\b")
UNITS = re.compile(r"\(([\d.]+(?:\s*(?:[-–]|to|or)\s*[\d.]+)?)\s+units?\b[^)]*\)", re.I)
POINTER = re.compile(rf"For (?:the )?course description,? see ({SUBJECT}) ?({NUM})")

MAJOR_LINE = re.compile(
    r"^(?:(?:Requirements for (?:the |a )?)?Major in|(?:Bachelor|B\.?S\.?|B\.?A\.?)[^:]*?\bin)\s+([A-Z][^.:;(]{2,70})\s*$",
    re.I,
)
REQUIREMENTS_H2 = re.compile(r"^Requirements for the (?:\w+ )?Majors?\b(?:\s+in\s+(.+))?", re.I)

# Capitalized words that look like subject codes in the bulletin's text.
NOT_SUBJECTS = {"NEW", "THE", "AND", "FOR", "ALL", "ONE", "TWO", "ANY", "GPA", "UNIT", "PART", "YEAR", "SAT", "ACT", "NOT", "WITH"}


def clean(text):
    return re.sub(r"\s+", " ", text.replace("\u200b", "").replace("\ufeff", "")).strip()


def numeric_part(number):
    """Numeric part of a course number: "140B" -> 140."""
    return int(re.match(r"\d+", number).group())


def parse_course_heading(heading):
    # "NEW 177. Race and the Environment" flags a new course; NEW is not a subject.
    text = re.sub(r"^NEW\s+(?=\d)", "", clean(heading))
    text = SPLIT_SUFFIX.sub(r"\1\2. ", text)  # "150 L. Security Lab" is course 150L
    text = re.sub(r"^(\d{1,3})([a-z])\.", lambda m: f"{m[1]}{m[2].upper()}.", text)  # "11a." is course 11A
    match = COURSE_HEADING.match(text)
    if not match:
        return None
    subject, number, rest, title = match.groups()
    if subject in NOT_SUBJECTS:
        return None
    numbers = [number]
    for item in re.findall(rf"{NUM}|\b[A-Z]\b(?!\s+\d)", re.sub(SUBJECT, "", rest)):
        # "151A. and B." means 151A and 151B.
        numbers.append(item if item[0].isdigit() else re.match(r"\d+", number)[0] + item)
    return {"subject": subject, "numbers": numbers, "title": clean(title)}


def extract_refs(text):
    """Finds course references in prose, expanding shorthand.

    "MATH 11, 12, and 53" gives three references; "ECON 170–174" gives a range.
    """
    normalized = clean(text)
    refs = []
    for match in HEAD_REF.finditer(normalized):
        subject, last = match.groups()
        if subject in NOT_SUBJECTS:
            continue
        refs.append({"subject": subject, "number": last})
        cursor = match.end()
        while True:
            found = RANGE_END.match(normalized, cursor)
            if found:
                refs.append({"subject": subject, "from": numeric_part(last), "to": numeric_part(found[1])})
                last, cursor = found[1], found.end()
                continue
            found = NEXT_ITEM.match(normalized, cursor)
            if not found:
                break
            refs.append({"subject": subject, "number": found[1]})
            last, cursor = found[1], found.end()
    return refs


def _school_for(url):
    match = re.search(r"chapter-(\d+)-", url)
    return SCHOOLS.get(match[1] if match else "", "Santa Clara University")


def _department_name(text):
    return re.sub(r"\s+Department$", "", re.sub(r"^(The\s+)?Department of\s+", "", clean(text), flags=re.I), flags=re.I)


def parse_page(html, url):
    """Extracts one bulletin page.

    Course sections rarely state their subject code, so each course keeps its
    section label and build_catalog infers the code.
    """
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    page = {"url": url, "department": "", "school": _school_for(url), "courses": [], "majors": [], "refs": []}
    if h1 is None:
        return page
    department = page["department"] = _department_name(h1.get_text())

    section = ""
    current = None
    in_majors = False
    major = None

    def start_major(name):
        nonlocal major
        normalized = re.sub(r"\s+\((?:B\.?S\.?|B\.?A\.?)\)$", "", clean(name), flags=re.I)
        major = next((m for m in page["majors"] if m["name"] == normalized), None)
        if major is None:
            major = {"name": normalized, "refs": []}
            page["majors"].append(major)

    for element in h1.parent.find_all(["h2", "h3", "h4", "p", "li"]):
        # A list item's text already includes any paragraphs or sub-items inside it.
        if element.name in ("p", "li") and element.find_parent("li") is not None:
            continue
        text = clean(element.get_text())
        if not text:
            continue

        # A few program pages use top-level headings for their courses.
        if element.name == "h2" and (parsed := parse_course_heading(text)):
            in_majors = False
            current = {**parsed, "section": "", "description": "", "units": None}
            page["courses"].append(current)
            continue

        if element.name == "h2":
            section = text
            current = None
            requirements = REQUIREMENTS_H2.match(text)
            in_majors = requirements is not None
            major = None
            if requirements and requirements[1]:
                start_major(requirements[1])
            continue

        if element.name in ("h3", "h4"):
            parsed = None if in_majors else parse_course_heading(text)
            if parsed:
                current = {**parsed, "section": section, "description": "", "units": None}
                page["courses"].append(current)
                continue
            current = None
            if re.match(r"Requirements for the Minor|Minors? ", text, re.I):
                in_majors = False
            if in_majors:
                line = MAJOR_LINE.match(text)
                if line:
                    start_major(line[1])
            continue

        if in_majors:
            line = MAJOR_LINE.match(text)
            if line:
                start_major(line[1])
                continue
            if major is None:
                start_major(department)
            major["refs"].extend(extract_refs(text))
        if current is not None:
            current["description"] = clean(f"{current['description']} {text}")
        page["refs"].extend(extract_refs(text))

    for course in page["courses"]:
        units = UNITS.search(course["description"])
        course["units"] = re.sub(r"\s+", "", units[1]) if units else None
    return page


def _best_subject(numbers, refs):
    """Picks the subject whose references best cover a set of course numbers."""
    wanted = set(numbers)
    score = Counter(ref["subject"] for ref in refs if ref.get("number") in wanted)
    return score.most_common(1)[0][0] if score else None


def _section_label(section, department):
    """"Lower-" and "Upper-Division Courses: X" share a subject, and pooling gives more evidence."""
    return re.sub(r"^(Lower|Upper)-Division Courses:?\s*", "", section, flags=re.I) or department


def _subject_in_label(label):
    match = re.search(rf"\(({SUBJECT})\)\s*$", label)
    return match[1] if match else None


def build_catalog(pages, section_subjects=None, department_subjects=None):
    """Combines parsed pages into de-duplicated courses and majors with resolved references.

    A section's subject comes from, in order: the declared tables, a code in the
    section's own name like "Religion and Society (RSOC)", then inference for
    pages the tables don't cover yet.
    """
    section_subjects = section_subjects or {}
    department_subjects = department_subjects or {}
    all_refs = [ref for page in pages for ref in page["refs"]]
    courses = {}
    unresolved = []

    for page in pages:
        pooled = {}
        for course in page["courses"]:
            pooled.setdefault(_section_label(course["section"], page["department"]), []).append(course)

        for key, group in pooled.items():
            stated = next((c["subject"] for c in group if c["subject"]), None)
            numbers = [n for c in group for n in c["numbers"]]
            inferred = (
                section_subjects.get(f"{page['department']}|{key}")
                or section_subjects.get(key)
                or _subject_in_label(key)
                or department_subjects.get(page["department"])
                or stated
                or _best_subject(numbers, page["refs"])
                or _best_subject(numbers, all_refs)
            )
            if not inferred:
                unresolved.append({"url": page["url"], "section": key, "count": len(group)})
                continue
            for course in group:
                for number in course["numbers"]:
                    subject = course["subject"] or inferred
                    existing = courses.get(f"{subject} {number}")
                    if existing is None or len(existing["description"]) < len(course["description"]):
                        courses[f"{subject} {number}"] = {
                            "subject": subject,
                            "number": number,
                            "title": course["title"],
                            "description": course["description"],
                            "units": course["units"],
                            "department": page["department"],
                            "school": page["school"],
                            "sourceUrl": page["url"],
                        }

    # Cross-listed entries often say only "For course description see ECON 135".
    for course in courses.values():
        pointer = POINTER.search(course["description"])
        target = courses.get(f"{pointer[1]} {pointer[2]}") if pointer else None
        if target and not POINTER.search(target["description"]):
            course["description"] = f"{target['description']} (Also listed as {target['subject']} {target['number']}.)"
            course["units"] = course["units"] or target["units"]

    by_subject = {}
    for course in courses.values():
        by_subject.setdefault(course["subject"], []).append(course)

    def resolve(refs):
        ids = set()
        for ref in refs:
            if "number" in ref:
                key = f"{ref['subject']} {ref['number']}"
                if key in courses:
                    ids.add(key)
            else:
                for course in by_subject.get(ref["subject"], []):
                    if ref["from"] <= numeric_part(course["number"]) <= ref["to"]:
                        ids.add(f"{course['subject']} {course['number']}")
        return ids

    majors = {}
    for page in pages:
        for parsed in page["majors"]:
            ids = resolve(parsed["refs"])
            if not ids:
                continue
            # A major also covers its home subject, including electives the requirements only imply.
            home = Counter(key.split(" ")[0] for key in ids).most_common(1)[0][0]
            ids.update(f"{c['subject']} {c['number']}" for c in by_subject.get(home, []))

            entry = majors.setdefault(
                parsed["name"],
                {"name": parsed["name"], "school": page["school"], "sourceUrl": page["url"], "courses": set()},
            )
            entry["courses"].update(ids)

    return {"courses": list(courses.values()), "majors": list(majors.values()), "unresolved": unresolved}
