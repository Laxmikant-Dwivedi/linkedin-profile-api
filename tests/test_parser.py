import pytest

from app.linkedin_client import extract_public_identifier
from app.parser import parse_profile_view

SAMPLE_RAW = {
    "included": [
        {
            "$type": "com.linkedin.voyager.identity.profile.Profile",
            "firstName": "Ada",
            "lastName": "Lovelace",
            "headline": "Mathematician & Writer at Analytical Engines Inc.",
            "geoLocationName": "London, England, United Kingdom",
            "summary": "Working on the first algorithm intended for machine processing.",
            "connectionsCount": 500,
            "profilePicture": {
                "displayImageReference": {
                    "vectorImage": {
                        "rootUrl": "https://media.licdn.com/dms/image/abc/",
                        "artifacts": [
                            {"width": 100, "height": 100, "fileIdentifyingUrlPathSegment": "100x100.jpg"},
                            {"width": 400, "height": 400, "fileIdentifyingUrlPathSegment": "400x400.jpg"},
                        ],
                    }
                }
            },
        },
        {
            "$type": "com.linkedin.voyager.identity.profile.Position",
            "title": "Founder",
            "companyName": "Analytical Engines Inc.",
            "locationName": "London, UK",
            "timePeriod": {"startDate": {"year": 1843, "month": 1}},
            "description": "Wrote notes on the Analytical Engine.",
        },
        {
            "$type": "com.linkedin.voyager.identity.profile.Education",
            "schoolName": "Royal Institution",
            "degreeName": "Self-study",
            "fieldOfStudy": "Mathematics",
            "timePeriod": {
                "startDate": {"year": 1832},
                "endDate": {"year": 1840},
            },
        },
        {
            "$type": "com.linkedin.voyager.identity.profile.Skill",
            "name": "Algorithm Design",
            "endorsementCount": 42,
        },
        {
            "$type": "com.linkedin.voyager.identity.profile.Certification",
            "name": "Fellow of the Royal Society (hon.)",
            "authority": "Royal Society",
        },
        {
            "$type": "com.linkedin.voyager.identity.profile.Language",
            "name": "French",
            "proficiency": "Native or bilingual proficiency",
        },
    ]
}


def test_parse_profile_view_extracts_all_sections():
    profile = parse_profile_view(
        SAMPLE_RAW,
        public_identifier="ada-lovelace",
        profile_url="https://www.linkedin.com/in/ada-lovelace/",
    )

    assert profile.full_name == "Ada Lovelace"
    assert profile.headline.startswith("Mathematician")
    assert profile.location == "London, England, United Kingdom"
    assert profile.about.startswith("Working on")
    assert profile.connections_count == 500

    assert len(profile.profile_images) == 2
    assert profile.profile_images[0].width == 400  # largest first

    assert len(profile.experience) == 1
    assert profile.experience[0].title == "Founder"
    assert profile.experience[0].company == "Analytical Engines Inc."
    assert profile.experience[0].start_date == "1843-01"
    assert profile.experience[0].is_current is True

    assert len(profile.education) == 1
    assert profile.education[0].school == "Royal Institution"
    assert profile.education[0].start_date == "1832"
    assert profile.education[0].end_date == "1840"

    assert profile.skills[0].name == "Algorithm Design"
    assert profile.skills[0].endorsement_count == 42

    assert profile.certifications[0].issuing_organization == "Royal Society"
    assert profile.languages[0].name == "French"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.linkedin.com/in/ada-lovelace/", "ada-lovelace"),
        ("https://linkedin.com/in/ada-lovelace", "ada-lovelace"),
        ("https://www.linkedin.com/in/ada-lovelace?trk=public_profile", "ada-lovelace"),
        ("https://m.linkedin.com/in/ada-lovelace/", "ada-lovelace"),
    ],
)
def test_extract_public_identifier(url, expected):
    assert extract_public_identifier(url) == expected


def test_extract_public_identifier_rejects_non_profile_url():
    with pytest.raises(ValueError):
        extract_public_identifier("https://www.linkedin.com/company/some-company/")
