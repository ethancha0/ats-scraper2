import pytest

from poll_discord import is_software_intern_role, is_software_intern_title


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer Intern",
        "Software Engineering Intern - Summer 2027",
        "SWE Intern",
        "SDE Intern",
        "Software Development Engineer Intern",
        "Backend Engineer Intern",
        "Back-End Developer Intern",
        "Frontend Engineer Intern",
        "Front End Engineering Internship",
        "Full-Stack Engineer Intern",
        "Fullstack Developer Intern",
        "Mobile Engineer Intern",
        "iOS Engineer Intern",
        "Android Developer Intern",
        "ML Engineer Intern",
        "Machine Learning Engineer Intern",
        "AI Engineer Intern",
        "AI/ML Intern",
        "Platform Engineer Intern",
        "Infrastructure Engineer Intern",
        "Cloud Engineer Intern",
        "Embedded Software Intern",
        "Data Engineer Intern",
        "Data Engineering Internship",
        "DevOps Intern",
        "SRE Intern",
        "Web Developer Intern",
        "Software Engineer Co-op",
        "Software Developer Coop - Fall 2027",
        "Software Engineering Interns",
        "Salesforce Developer Intern",
        "Programmer Intern",
        "Intern - Artificial Intelligence",
        "Computer Science and Engineering Co-op - Summer 2027",
        "Forward Deployed Engineering Intern, BS/MS, Summer 2027",
    ],
)
def test_matches_software_intern_titles(title):
    assert is_software_intern_title(title)


@pytest.mark.parametrize(
    "title",
    [
        "",
        "Software Engineer",
        "Software Engineer, Internal Tools",
        "Software Engineer - International Payments",
        "Senior Backend Engineer",
        "Marketing Intern",
        "Sales Engineering Intern",
        "Software Sales Intern",
        "Finance Intern",
        "Accounting Intern",
        "Recruiting Intern",
        "Technical Recruiter Intern",
        "Human Resources Intern",
        "Mechanical Engineering Intern",
        "Civil Engineering Intern",
        "Marketing Software Intern",
        "Business Analyst Intern",
        "Internal Medicine Physician",
    ],
)
def test_rejects_non_software_intern_titles(title):
    assert not is_software_intern_title(title)


def test_role_filter_reads_title_field():
    assert is_software_intern_role({"title": "SWE Intern"})
    assert not is_software_intern_role({"title": None})
    assert not is_software_intern_role({})
