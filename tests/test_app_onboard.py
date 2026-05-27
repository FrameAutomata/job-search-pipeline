"""Tests for pipeline/app/onboard.py.

Pure mapping/parsing logic runs everywhere; the node round-trip and pdfplumber
extraction are guarded so the suite still passes without node / pdfplumber.
"""

import base64
import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline.app import onboard


class TestParseLocations:
    @pytest.mark.parametrize("text,expected", [
        ("", []),
        ("Dallas, TX", ["Dallas, TX"]),
        ("US Remote, Dallas, TX, Fort Worth, TX",
         ["US Remote", "Dallas, TX", "Fort Worth, TX"]),
        ("Toronto, ON, Montreal, QC", ["Toronto, ON", "Montreal, QC"]),
        ("London, UK", ["London, UK"]),
        ("Remote", ["Remote"]),
    ])
    def test_keeps_city_state_pairs(self, text, expected):
        assert onboard.parse_locations(text) == expected


class TestInferCountry:
    @pytest.mark.parametrize("loc,country", [
        ("Dallas, TX", "USA"),
        ("Toronto, ON", "Canada"),
        ("London, UK", "UK"),
        ("Sydney, Australia", "Australia"),
        ("US Remote", "USA"),
        ("somewhere weird", "USA"),  # safe default
    ])
    def test_infer(self, loc, country):
        assert onboard.infer_country(loc) == country

    def test_remote_location_map(self):
        assert onboard.infer_remote_location("Canada") == "Canada"
        assert onboard.infer_remote_location("USA") == "United States"
        assert onboard.infer_remote_location("UK") == "United Kingdom"


class TestBuildOnboardingJson:
    def test_maps_form_and_marks_remote_vs_local(self):
        form = {
            "name": "Jane Dev",
            "target_roles": "Senior Full-Stack Engineer, Backend Engineer",
            "negative_roles": "Intern, Manager",
            "comp_target": "$150K",
            "locations": "US Remote, Dallas, TX",
            "distance": "40",
            "hours_old": "48",
            "results_wanted": "100",
            "sites": ["indeed", "linkedin"],
            "include_easy_apply": True,
        }
        payload = onboard.build_onboarding_json(form, resume_text="resume body")
        assert payload["resumeText"] == "resume body"
        assert payload["info"]["name"] == "Jane Dev"
        assert payload["criteria"]["targetRoles"] == ["Senior Full-Stack Engineer", "Backend Engineer"]
        assert payload["criteria"]["negativeRoles"] == ["Intern", "Manager"]
        locs = payload["searchSettings"]["locations"]
        assert locs[0]["isRemote"] is True and locs[0]["location"] == "United States"
        assert locs[1]["isRemote"] is False and locs[1]["distance"] == 40
        assert payload["searchSettings"]["hoursOld"] == 48
        assert payload["searchSettings"]["includeEasyApply"] is True

    def test_defaults_when_empty(self):
        payload = onboard.build_onboarding_json({}, resume_text="")
        # No locations -> a single US-Remote default entry.
        locs = payload["searchSettings"]["locations"]
        assert len(locs) == 1 and locs[0]["isRemote"] is True
        assert payload["searchSettings"]["sites"] == ["indeed", "linkedin", "glassdoor"]
        assert payload["searchSettings"]["resultsWanted"] == 100


class TestCollectSecretBlobs:
    def test_reads_and_base64s_present_files(self, tmp_path):
        (tmp_path / "config").mkdir()
        (tmp_path / "resumes").mkdir()
        (tmp_path / "career-ops" / "config").mkdir(parents=True)
        (tmp_path / "career-ops" / "modes").mkdir(parents=True)
        (tmp_path / "config" / "search.yml").write_text("searches: []", encoding="utf-8")
        (tmp_path / "resumes" / "resume.txt").write_text("resume", encoding="utf-8")
        (tmp_path / "career-ops" / "cv.md").write_text("# CV", encoding="utf-8")
        (tmp_path / "career-ops" / "config" / "profile.yml").write_text("candidate: {}", encoding="utf-8")
        # PROFILE_MD intentionally absent — it's optional.
        blobs = onboard.collect_secret_blobs(tmp_path)
        assert set(blobs) >= set(onboard.REQUIRED_SECRETS)
        assert "PROFILE_MD_B64" not in blobs
        assert base64.b64decode(blobs["CV_MD_B64"]).decode() == "# CV"

    def test_raises_when_required_missing(self, tmp_path):
        with pytest.raises(onboard.OnboardError, match="search.yml"):
            onboard.collect_secret_blobs(tmp_path)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
class TestNodeRoundTrip:
    """Guards against the interactive and --from-json paths drifting: run the
    real generator on a fixture and assert the four artifacts appear."""

    def test_from_json_produces_artifacts(self, tmp_path):
        repo = Path(__file__).resolve().parent.parent
        # Run node in an isolated cwd with only the example config available, so
        # we don't touch the real config/career-ops.
        work = tmp_path
        (work / "config").mkdir()
        shutil.copy(repo / "config" / "search.example.yml", work / "config" / "search.example.yml")
        (work / "career-ops" / "config").mkdir(parents=True)
        (work / "career-ops" / "modes").mkdir(parents=True)

        payload = onboard.build_onboarding_json(
            {"name": "Jane Dev", "target_roles": "Backend Engineer",
             "locations": "US Remote, Dallas, TX", "sites": ["indeed"]},
            resume_text="Jane Dev\nSKILLS\nPython, AWS\nEXPERIENCE\nAcme",
        )
        result = onboard.run_generation(work, payload)
        assert result.get("ok") is True
        assert (work / "career-ops" / "config" / "profile.yml").exists()
        assert (work / "career-ops" / "cv.md").exists()
        assert (work / "career-ops" / "modes" / "_profile.md").exists()
        assert (work / "config" / "search.yml").exists()


class TestRunGenerationErrors:
    def test_node_missing_raises_clear_error(self, tmp_path, mocker):
        mocker.patch("pipeline.app.onboard.subprocess.run", side_effect=FileNotFoundError())
        with pytest.raises(onboard.OnboardError, match="node not found"):
            onboard.run_generation(tmp_path, {"info": {}})

    def test_nonzero_exit_surfaces_stderr(self, tmp_path, mocker):
        cp = subprocess.CompletedProcess(args=["node"], returncode=1, stdout="", stderr="kaboom")
        mocker.patch("pipeline.app.onboard.subprocess.run", return_value=cp)
        with pytest.raises(onboard.OnboardError, match="kaboom"):
            onboard.run_generation(tmp_path, {"info": {}})
