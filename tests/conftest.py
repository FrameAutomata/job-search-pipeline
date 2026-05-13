"""Shared fixtures for job-search-pipeline tests."""

from pathlib import Path
from datetime import datetime, timedelta

import pytest

from pipeline import scrape as scrape_mod
from pipeline import filter as filter_mod
from pipeline import bridge as bridge_mod


# Synthetic resume used by filter tests. Includes a Skills section so
# extract_keywords produces a non-empty result.
SYNTHETIC_RESUME = """
Thomas Thirlwall
Software Engineer
Dallas, TX

PROFESSIONAL SUMMARY
Software Engineer with experience building full-stack applications.

SKILLS
Languages: Java, TypeScript, Python, SQL
Backend/Data: PostgreSQL, Supabase, Spring Boot, Express.js, Node.js, REST APIs
Frontend/Mobile: Angular, React Native, Expo, Flutter
Cloud/DevOps: AWS, Docker, Terraform, GitHub Actions

PROJECTS
Arcade Radar
Built a cross-platform arcade locator app using Expo Router and Supabase.

PROFESSIONAL EXPERIENCE
Capital One
Software Engineer
Developed Angular frontend and Java/Spring Boot REST APIs.
Deployed and supported Dockerized services running on AWS ECS.

EDUCATION
University of Texas - Rio Grande Valley
Bachelor's, Computer Science
"""


@pytest.fixture
def cfg_file(tmp_path):
    """Write a minimal multi-pass search.yml to tmp_path and return its path."""
    search_yml = tmp_path / "search.yml"
    search_yml.write_text("""
searches:
  - name: "test pass"
    search_terms:
      - "software engineer"
    sites:
      - indeed
    location: "Dallas, TX"
    country_indeed: "USA"
    results_wanted: 2

filter:
  target_titles:
    - "software engineer"
  negative_titles:
    - "senior"
  min_score: 5
""")
    return search_yml


@pytest.fixture
def jobs_csv(tmp_path):
    """Write a 5-row test jobs.csv with real column names and return its path."""
    jobs_file = tmp_path / "jobs.csv"

    # Real column order from JobSpy output
    header = "id,site,job_url,job_url_direct,title,company,location,date_posted," \
             "job_type,salary_currency,min_amount,max_amount,interval,benefits,description," \
             "emails,skills,is_remote\n"

    rows = [
        '1,indeed,https://indeed.com/job1,,,software engineer,acme,Dallas TX,2026-05-12,2026-05-11,fulltime,,,'
        f'USD,100000,120000,yearly,,rest apis python,,""\n',

        '2,indeed,https://indeed.com/job2,,,backend engineer,globex,Remote,2026-05-12,2026-05-11,fulltime,,,'
        f'USD,90000,110000,yearly,,spring boot java,,"true"\n',

        '3,indeed,https://indeed.com/job3,,,senior engineer,acme,New York,2026-05-11,2026-05-10,fulltime,,,'
        f'USD,120000,150000,yearly,,python,""\n',

        '4,linkedin,https://linkedin.com/job4,,,developer,initech,San Francisco,2026-05-01,2026-04-30,fulltime,,,'
        f'USD,80000,100000,yearly,,javascript react,,"false"\n',

        '5,glassdoor,https://glassdoor.com/job5,,,application developer,vandalay,Chicago,2026-05-12,2026-05-11,'
        f'fulltime,,,USD,70000,90000,yearly,,postgresql sql,,""\n',
    ]

    jobs_file.write_text(header + "".join(rows))
    return jobs_file


@pytest.fixture
def filtered_csv(tmp_path):
    """Write a 3-row test filtered_jobs.csv and return its path."""
    filtered_file = tmp_path / "filtered_jobs.csv"

    header = "title,company,location,is_remote,min_amount,max_amount,interval,job_url,date_posted," \
             "relevance_score,matched_keywords\n"

    rows = [
        'software engineer,acme,Dallas TX,false,100000,120000,yearly,https://indeed.com/job1,2026-05-12,8,"keyword:python, title:software engineer"\n',
        'backend engineer,globex,Remote,true,90000,110000,yearly,https://indeed.com/job2,2026-05-12,7,"keyword:spring boot"\n',
        'developer,initech,San Francisco,false,80000,100000,yearly,https://linkedin.com/job4,2026-05-01,4,"keyword:javascript"\n',
    ]

    filtered_file.write_text(header + "".join(rows))
    return filtered_file


@pytest.fixture
def career_ops_dir(tmp_path):
    """Create a mock career-ops directory structure and return the root."""
    career_ops = tmp_path / "career-ops"
    data_dir = career_ops / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return career_ops


@pytest.fixture
def fake_pdf(tmp_path):
    """Write a dummy PDF file to tmp_path and return its path."""
    pdf_file = tmp_path / "resume.pdf"
    # pdfplumber is mocked in tests, so content doesn't matter
    pdf_file.write_bytes(b"%PDF-1.4\n%dummy pdf content")
    return pdf_file


@pytest.fixture
def patch_scrape_paths(monkeypatch, tmp_path):
    """Patch scrape.OUTPUT_PATH to write to tmp_path, create output dir, and return the path."""
    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "jobs.csv"
    monkeypatch.setattr(scrape_mod, "OUTPUT_PATH", output_path)
    return output_path


@pytest.fixture
def patch_filter_paths(monkeypatch, tmp_path):
    """Patch filter.JOBS_PATH and filter.OUTPUT_PATH to tmp_path, return (jobs_path, output_path)."""
    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    jobs_path = output_dir / "jobs.csv"
    output_path = output_dir / "filtered_jobs.csv"

    monkeypatch.setattr(filter_mod, "JOBS_PATH", jobs_path)
    monkeypatch.setattr(filter_mod, "OUTPUT_PATH", output_path)

    return jobs_path, output_path


@pytest.fixture
def patch_bridge_paths(monkeypatch, tmp_path):
    """Patch bridge.FILTERED_PATH to tmp_path and return the path."""
    filtered_path = tmp_path / "filtered_jobs.csv"
    monkeypatch.setattr(bridge_mod, "FILTERED_PATH", filtered_path)
    return filtered_path
