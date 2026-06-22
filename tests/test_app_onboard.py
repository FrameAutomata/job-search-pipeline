"""Tests for pipeline/app/onboard.py.

Pure mapping/parsing logic runs everywhere; the node round-trip and pdfplumber
extraction are guarded so the suite still passes without node / pdfplumber.
"""

import base64
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline.app import onboard


def _node_deps_available() -> bool:
    """True only if node AND the npm packages setup-profile.mjs imports
    (yaml, pdf-parse) resolve. CI has node but doesn't `npm install`, so the
    round-trip test skips there; it runs locally where setup has installed deps."""
    if shutil.which("node") is None:
        return False
    repo = Path(__file__).resolve().parent.parent
    try:
        r = subprocess.run(
            ["node", "-e", "require.resolve('yaml'); require.resolve('pdf-parse')"],
            cwd=str(repo), capture_output=True, timeout=15,
        )
        return r.returncode == 0
    except Exception:
        return False


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
            "eeo_gender": "Female",
            "eeo_disability": "No, I do not have a disability",
        }
        payload = onboard.build_onboarding_json(form, resume_text="resume body")
        assert payload["resumeText"] == "resume body"
        assert payload["info"]["name"] == "Jane Dev"
        assert payload["info"]["eeoGender"] == "Female"
        assert payload["info"]["eeoDisability"] == "No, I do not have a disability"
        assert payload["info"]["eeoVeteran"] == ""        # unset → blank → declines
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

    def test_maps_indeed_consent_toggles(self):
        info = onboard.build_onboarding_json(
            {"data_processing_consent": "no", "save_answers": "yes"}, "")["info"]
        assert info["eeoConsent"] is False        # explicit decline honored
        assert info["eeoSaveAnswers"] is True
        assert info["eeoShareAnswers"] is False

    def test_indeed_consent_defaults(self):
        info = onboard.build_onboarding_json({}, "")["info"]
        assert info["eeoConsent"] is True         # auto-agree the required consent
        assert info["eeoSaveAnswers"] is False and info["eeoShareAnswers"] is False


class TestParseResumeInfo:
    SAMPLE = (
        "Thomas Thirlwall\n"
        "+1 (956) 525-3015 | ththirlwall99@gmail.com | Dallas, TX | "
        "linkedin.com/in/thomas-thirlwall | https://github.com/FrameAutomata | "
        "https://thomas.dev\n"
        "PROFESSIONAL SUMMARY\nEngineer.\n"
    )

    def test_extracts_all_fields(self):
        info = onboard.parse_resume_info(self.SAMPLE)
        assert info["name"] == "Thomas Thirlwall"
        assert info["email"] == "ththirlwall99@gmail.com"
        assert info["phone"] == "+1 (956) 525-3015"
        assert info["location"] == "Dallas, TX"
        assert info["linkedin"] == "linkedin.com/in/thomas-thirlwall"
        assert info["github"] == "github.com/FrameAutomata"
        # website = first non-social URL
        assert info["website"] == "https://thomas.dev"

    def test_website_skips_social_urls(self):
        info = onboard.parse_resume_info("Jane\nhttps://github.com/jane only")
        assert info["website"] is None

    def test_missing_fields_are_none(self):
        info = onboard.parse_resume_info("Just Some Text\nno contacts here")
        assert info["email"] is None and info["phone"] is None
        assert info["name"] == "Just Some Text"

    def test_first_line_with_at_is_not_treated_as_name(self):
        info = onboard.parse_resume_info("me@example.com\nReal Name")
        assert info["name"] is None  # first line has '@'
        assert info["email"] == "me@example.com"


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


@pytest.mark.skipif(not _node_deps_available(),
                    reason="node or its npm deps (yaml/pdf-parse) not installed")
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
             "locations": "US Remote, Dallas, TX", "sites": ["indeed"],
             "citizenship": "Canadian", "requires_sponsorship": "yes",
             "work_auth_regions": "Canada", "eligible_countries": "Canada, United States",
             "work_permit_type": "Needs sponsorship"},
            resume_text="Jane Dev\nSKILLS\nPython, AWS\nEXPERIENCE\nAcme",
        )
        result = onboard.run_generation(work, payload)
        assert result.get("ok") is True
        profile_path = work / "career-ops" / "config" / "profile.yml"
        assert profile_path.exists()
        assert (work / "career-ops" / "cv.md").exists()
        assert (work / "career-ops" / "modes" / "_profile.md").exists()
        assert (work / "config" / "search.yml").exists()

        # Work-authorization answers flow form -> JSON -> generated profile.yml.
        import yaml
        profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
        wa = profile["work_authorization"]
        assert wa["citizenship"] == "Canadian"
        assert wa["requires_sponsorship"] is True
        assert wa["legally_authorized_to_work_in"] == ["Canada"]
        assert wa["eligible_countries"] == ["Canada", "United States"]
        assert wa["work_permit_type"] == "Needs sponsorship"


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


class TestSidecar:
    """The sidecar lets the wizard prefill every field on a revisit so the user
    only touches the knob they want to change. api_key must never land in it —
    the sidecar lives on disk; the key belongs in GitHub Secrets only."""

    def test_load_returns_none_when_missing(self, tmp_path):
        assert onboard.load_sidecar(tmp_path) is None

    def test_save_then_load_round_trips(self, tmp_path):
        payload = {"name": "Jane", "results_wanted": 5, "sites": ["indeed", "linkedin"]}
        onboard.save_sidecar(tmp_path, payload)
        assert onboard.load_sidecar(tmp_path) == payload

    def test_save_strips_api_key(self, tmp_path):
        onboard.save_sidecar(tmp_path, {"name": "Jane", "api_key": "secret-xyz"})
        loaded = onboard.load_sidecar(tmp_path)
        assert "api_key" not in loaded
        # Belt-and-braces: the raw file shouldn't contain the key either.
        raw = (tmp_path / ".ui-cache" / "onboarding.json").read_text(encoding="utf-8")
        assert "secret-xyz" not in raw

    def test_load_returns_none_on_corrupt_file(self, tmp_path):
        # If someone hand-edits the sidecar into invalid JSON, fall back to
        # "no prefill" rather than crash the wizard.
        sidecar = tmp_path / ".ui-cache" / "onboarding.json"
        sidecar.parent.mkdir(parents=True)
        sidecar.write_text("{ not valid json", encoding="utf-8")
        assert onboard.load_sidecar(tmp_path) is None

    def test_save_swallows_io_errors(self, tmp_path, mocker):
        # Sidecar persistence is a UX nicety. If we can't write (read-only fs,
        # antivirus lock, whatever), don't fail the onboarding submission —
        # the secrets have already been written at that point.
        mocker.patch("pipeline.app.onboard.Path.write_text",
                     side_effect=OSError("read-only"))
        # No exception:
        onboard.save_sidecar(tmp_path, {"name": "Jane"})


class TestArticleDigestSecretWiring:
    """article-digest.md is generated during onboarding (LLM-grounded) and must
    ride along to the cloud as an OPTIONAL secret — present when generation
    succeeded, silently omitted when it didn't (so onboarding never hard-depends
    on the digest)."""

    def _seed_required(self, root):
        (root / "config").mkdir()
        (root / "resumes").mkdir()
        (root / "career-ops" / "config").mkdir(parents=True)
        (root / "career-ops" / "modes").mkdir(parents=True)
        (root / "config" / "search.yml").write_text("searches: []", encoding="utf-8")
        (root / "resumes" / "resume.txt").write_text("resume", encoding="utf-8")
        (root / "career-ops" / "cv.md").write_text("# CV", encoding="utf-8")
        (root / "career-ops" / "config" / "profile.yml").write_text("candidate: {}", encoding="utf-8")

    def test_article_digest_is_an_optional_secret(self):
        assert onboard.SECRET_FILES.get("ARTICLE_DIGEST_B64") == "career-ops/article-digest.md"
        assert "ARTICLE_DIGEST_B64" not in onboard.REQUIRED_SECRETS

    def test_collect_includes_article_digest_when_present(self, tmp_path):
        self._seed_required(tmp_path)
        (tmp_path / "career-ops" / "article-digest.md").write_text("## Proj", encoding="utf-8")
        blobs = onboard.collect_secret_blobs(tmp_path)
        assert "ARTICLE_DIGEST_B64" in blobs
        assert base64.b64decode(blobs["ARTICLE_DIGEST_B64"]).decode() == "## Proj"

    def test_collect_omits_article_digest_when_absent(self, tmp_path):
        self._seed_required(tmp_path)
        blobs = onboard.collect_secret_blobs(tmp_path)
        assert "ARTICLE_DIGEST_B64" not in blobs


class TestEeoDatalists:
    """The four voluntary self-ID fields offer standard answers as dropdown
    suggestions, but must stay free-text so a user can type the employer's exact
    wording (the answers are fuzzy-matched per form) or a non-US category. That
    means an <input list="..."> wired to a <datalist> — never a hard <select>,
    which would lock in one (US-centric) option set and drop free typing."""

    EEO_FIELDS = ["eeo_gender", "eeo_race", "eeo_veteran", "eeo_disability"]

    @pytest.fixture
    def html(self):
        root = Path(__file__).resolve().parent.parent
        return (root / "pipeline" / "app" / "static" / "onboard.html").read_text(encoding="utf-8")

    def _input_tag(self, html, name):
        m = re.search(rf'<input\b[^>]*\bname="{re.escape(name)}"[^>]*>', html)
        assert m, f"no <input name={name!r}> found"
        return m.group(0)

    def test_each_eeo_field_is_input_not_select(self, html):
        # A <select name="eeo_*"> would mean we lost free-text entry.
        for name in self.EEO_FIELDS:
            assert not re.search(rf'<select\b[^>]*\bname="{re.escape(name)}"', html), \
                f"{name} must stay an <input> (free-text), not a <select>"
            self._input_tag(html, name)  # the <input> still exists

    def test_each_eeo_input_references_an_existing_datalist(self, html):
        datalist_ids = set(re.findall(r'<datalist\b[^>]*\bid="([^"]+)"', html))
        for name in self.EEO_FIELDS:
            tag = self._input_tag(html, name)
            m = re.search(r'\blist="([^"]+)"', tag)
            assert m, f"{name} input is missing a list= attribute"
            assert m.group(1) in datalist_ids, \
                f"{name} list={m.group(1)!r} has no matching <datalist id>"

    def test_datalists_offer_standard_and_decline_options(self, html):
        # Spot-check that the suggestion sets carry the usual wording, including
        # an explicit "prefer not to say" so declining is one click, not just blank.
        assert "Non-binary" in html
        assert "Black or African American" in html
        assert "protected veteran" in html
        assert "do not have a disability" in html
        assert re.search(r"[Pp]refer not to (say|answer)|don't wish to answer|do not want to answer", html)


class TestPortfolioUrls:
    def test_combines_portfolio_csv_and_github(self):
        urls = onboard.portfolio_urls(
            {"portfolio": "https://a.com, https://b.com", "github": "https://github.com/me"})
        assert urls == ["https://a.com", "https://b.com", "https://github.com/me"]

    def test_accepts_list_and_skips_blank_github(self):
        assert onboard.portfolio_urls({"portfolio": ["https://a.com"], "github": ""}) == ["https://a.com"]

    def test_empty_when_nothing_supplied(self):
        assert onboard.portfolio_urls({}) == []
