import pytest

from crawler.parse import build_catalog, extract_refs, parse_course_heading, parse_page


@pytest.mark.parametrize(
    "heading, subject, numbers, title",
    [
        ("10. Introduction to Computer Science", None, ["10"], "Introduction to Computer Science"),
        ("140B: Black Cinema", None, ["140B"], "Black Cinema"),
        ("ARTH 82. Weaving the World", "ARTH", ["82"], "Weaving the World"),
        ("ASCI 150: “Catholic 101”", "ASCI", ["150"], "“Catholic 101”"),
        ("MATH 9S Precalculus with support", "MATH", ["9S"], "Precalculus with support"),
        ("MATH 9A and MATH 9B Prelude to Calculus", "MATH", ["9A", "9B"], "Prelude to Calculus"),
        ("1A. and 2A. Critical Thinking & Writing I and II", None, ["1A", "2A"], "Critical Thinking & Writing I and II"),
        ("NEW 177. Race and the Environment", None, ["177"], "Race and the Environment"),
        ("136. 20th-Century Economic History", None, ["136"], "20th-Century Economic History"),
        ("32. 2D Fundamentals", None, ["32"], "2D Fundamentals"),
        ("154. \u200bEconomics of Health Care", None, ["154"], "Economics of Health Care"),
        ("185.Data Mining", None, ["185"], "Data Mining"),
        ("11a. Culture, Tech, and the Common Good I", None, ["11A"], "Culture, Tech, and the Common Good I"),
        ("176 – Interracial Intimacy", None, ["176"], "Interracial Intimacy"),
        ("151A. and B. Food and Poverty", None, ["151A", "151B"], "Food and Poverty"),
        ("AERO 1A. and B. Heritage and Values", "AERO", ["1A", "1B"], "Heritage and Values"),
        ("150 L. Information Security Laboratory", None, ["150L"], "Information Security Laboratory"),
    ],
)
def test_course_heading_formats(heading, subject, numbers, title):
    assert parse_course_heading(heading) == {"subject": subject, "numbers": numbers, "title": title}


@pytest.mark.parametrize("heading", ["Emphasis Programs in Anthropology", "Art History Courses", "Minors in Related Areas"])
def test_ordinary_headings_are_not_courses(heading):
    assert parse_course_heading(heading) is None


def numbers(text):
    return [r.get("number", f"{r.get('from')}-{r.get('to')}") for r in extract_refs(text)]


def test_reference_shorthand():
    assert numbers("MATH 11, 12, 13, and 53") == ["11", "12", "13", "53"]
    assert numbers("PHYS 31 and 32. Students planning") == ["31", "32"]
    assert numbers("ECON 170–174") == ["170", "170-174"]
    assert [r["subject"] for r in extract_refs("CSCI 184 or CSEN 178")] == ["CSCI", "CSEN"]
    assert extract_refs("NEW 177 and THE 5") == []


PAGE = """<html><body><div>
  <h1>Department of Mathematics and Computer Science</h1>
  <h2>Requirements for the Major</h2>
  <p>Major in Mathematics</p><p>MATH 11, 12 and CSCI 10</p>
  <h3>Major in Computer Science</h3><p>CSCI 10, 60</p><p>MATH 11</p>
  <h2>Requirements for the Minors</h2><p>MATH 99</p>
  <h2>Lower-Division Courses: Mathematics</h2>
  <h3>11. Calculus I</h3><p>Limits and derivatives.</p><p>(4 units)</p>
  <h3>12. Calculus II</h3><p>Integrals. (4 units)</p>
  <h2>Lower-Division Courses: Computer Science</h2>
  <h3>10. Intro to CS</h3><p>Python. (5 units)</p>
  <h3>60. C++</h3><p>Objects. (5 units)</p>
</div></body></html>"""


def test_infers_subjects_and_links_majors():
    page = parse_page(PAGE, "https://www.scu.edu/bulletin/undergraduate/chapter-3-x/macs.html")
    assert page["department"] == "Mathematics and Computer Science"
    assert page["school"] == "College of Arts and Sciences"

    catalog = build_catalog([page])
    assert catalog["unresolved"] == []
    courses = catalog["courses"]
    assert sorted(f"{c['subject']} {c['number']}" for c in courses) == ["CSCI 10", "CSCI 60", "MATH 11", "MATH 12"]
    calc = next(c for c in courses if c["number"] == "11")
    assert calc["units"] == "4"
    assert calc["description"] == "Limits and derivatives. (4 units)"

    majors = {m["name"]: sorted(m["courses"]) for m in catalog["majors"]}
    assert sorted(majors) == ["Computer Science", "Mathematics"]
    assert majors["Computer Science"] == ["CSCI 10", "CSCI 60", "MATH 11"]


def test_cross_listed_courses_borrow_description():
    page = parse_page(
        """<div><h1>Department of Gender Studies</h1>
        <h2>Upper-Division Courses: GNSX</h2>
        <h3>GNSX 121. Gender Issues</h3><p>Also listed as ECON 135. For course description see ECON 135.</p>
        <h2>Upper-Division Courses: Economics</h2>
        <h3>ECON 135. Gender Issues</h3><p>The real description. (5 units)</p></div>""",
        "https://www.scu.edu/bulletin/undergraduate/chapter-3-x/a.html",
    )
    gnsx = next(c for c in build_catalog([page])["courses"] if c["subject"] == "GNSX")
    assert gnsx["description"] == "The real description. (5 units) (Also listed as ECON 135.)"
    assert gnsx["units"] == "5"


URL = "https://www.scu.edu/bulletin/undergraduate/chapter-3-x/a.html"


def codes(html, sections=None, departments=None):
    return sorted(f"{c['subject']} {c['number']}" for c in build_catalog([parse_page(html, URL)], sections, departments)["courses"])


def test_same_number_in_different_languages_stays_separate():
    html = """<div><h1>Department of Modern Languages</h1>
      <h2>Lower-Division Courses: Italian Studies</h2><h3>1. Elementary Italian</h3><p>Ciao.</p>
      <h2>Lower-Division Courses: Spanish Studies</h2><h3>1. Elementary Spanish</h3><p>Hola.</p></div>"""
    assert codes(html, {"Italian Studies": "ITAL", "Spanish Studies": "SPAN"}) == ["ITAL 1", "SPAN 1"]


def test_declared_department_beats_other_departments_in_the_text():
    html = """<div><h1>Department of Political Science</h1>
      <h2>Requirements for the Major</h2><p>ECON 107 and ECON 105</p>
      <h2>Upper-Division Courses: Political Philosophy</h2><h3>107. American Political Thought</h3><p>Text.</p></div>"""
    assert codes(html, departments={"Political Science": "POLI"}) == ["POLI 107"]


def test_subject_stated_in_section_name():
    html = """<div><h1>Department of Religious Studies</h1>
      <h2>Upper-Division Courses: Religion and Society (RSOC)</h2><h3>150. Faith and Power</h3><p>Text.</p></div>"""
    assert codes(html) == ["RSOC 150"]


def test_heading_with_its_own_subject_is_kept():
    html = """<div><h1>Department of Art</h1>
      <h2>Lower-Division Courses: Art History</h2><h3>THTR 82. Stagecraft</h3><p>Text.</p></div>"""
    assert codes(html, {"Art History": "ARTH"}) == ["THTR 82"]


def test_courses_under_top_level_headings():
    html = """<div><h1>LEAD Scholars Program</h1>
      <h2>1. LEAD Scholars Seminar</h2><p>Welcome. (1 unit)</p>
      <h2>100. Advanced Seminar</h2><p>LEAD 100 continues the work. (2 units)</p></div>"""
    assert codes(html, departments={"LEAD Scholars Program": "LEAD"}) == ["LEAD 1", "LEAD 100"]
