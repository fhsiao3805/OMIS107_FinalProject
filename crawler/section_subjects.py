"""Subject codes the bulletin leaves implicit.

Most pages list courses as "107. Systems Programming" and never state the code
next to it. Guessing from nearby text fails on topical subsections, and on pages
that cite other departments more than their own, so the codes are declared here.
Each was checked against how the department's own page refers to its courses.
"""

# The subject of every course on a single-subject department page.
DEPARTMENT_SUBJECTS: dict[str, str] = {
    "Accounting": "ACTG",
    "Applied Mathematics": "AMTH",
    "Bioengineering": "BIOE",
    "Civil, Environmental and Sustainable Engineering": "CENG",
    "Computer Science and Engineering": "CSEN",
    "Electrical and Computer Engineering": "ECEN",
    "General Engineering": "ENGR",
    "Marketing": "MKTG",
    "Mechanical Engineering": "MECH",
    "Military Science Program": "MILS",
    "Anthropology": "ANTH",
    "Biology": "BIOL",
    "Chemistry and Biochemistry": "CHEM",
    "Child Studies": "CHST",
    "Classics and Ancient Studies": "CLAS",
    "Communication": "COMM",
    "Economics": "ECON",
    "English": "ENGL",
    "Environmental Studies and Sciences": "ENVS",
    "Ethnic Studies": "ETHN",
    "Finance": "FNCE",
    "Gender and Sexuality Studies": "GNSX",
    "History": "HIST",
    "Information Systems and Analytics": "OMIS",
    "LEAD Scholars Program": "LEAD",
    "Management and Entrepreneurship": "MGMT",
    "Music": "MUSC",
    "Neuroscience Program": "NEUR",
    "Philosophy": "PHIL",
    "Physics and Engineering Physics": "PHYS",
    "Political Science": "POLI",
    "Psychology": "PSYC",
    "Public Health": "PHSC",
    "Sociology": "SOCI",
}

# Sections on pages that hold several subjects. Keys are "Department|Section" or just the section label.
SECTION_SUBJECTS: dict[str, str] = {
    "General Business Courses": "BUSN",
    "Art History": "ARTH",
    "Studio Art": "ARTS",
    "Mathematics": "MATH",
    "Computer Science": "CSCI",
    "Theatre": "THTR",
    "Dance": "DANC",
    "Arabic Studies": "ARAB",
    "Chinese Studies": "CHIN",
    "French and Francophone Studies": "FREN",
    "German Studies": "GERM",
    "Italian Studies": "ITAL",
    "Japanese Studies": "JAPN",
    "Spanish Studies": "SPAN",
    # Modern Languages lists these directly after the language they belong to.
    "Literature and Culture in Translation Courses": "FREN",
    "Literature in Translation Courses Taught in English": "GERM",
    "Literature, Film, and Culture Courses Taught in English": "ITAL",
    "Taught in English: Modern Literature and Culture": "SPAN",
}
