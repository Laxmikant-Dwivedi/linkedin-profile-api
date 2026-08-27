"""Turns a raw `profileView` Voyager response into our clean `LinkedInProfile`.

LinkedIn's internal API returns a normalized payload: a top-level `data`
object holding references (entityUrns) to fully-detailed objects that live
flat in an `included` array, each tagged with a `$type` like
`com.linkedin.voyager.identity.profile.Position`. This is undocumented and
has changed shape before (and will again) — if fields start coming back
empty, the fix is almost always: open a profile in a browser with dev tools
open, find the `profileView` XHR response, and adjust the `$type` suffixes
/ key names below to match. See README "Known limitations".
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.schemas import (
    Certification,
    Education,
    Experience,
    Language,
    LinkedInProfile,
    ProfileImage,
    Skill,
)


def _index_by_type_suffix(included: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Group every element of `included` by the last component of its
    `$type`, e.g. "com.linkedin.voyager.identity.profile.Position" -> "Position"."""
    index: Dict[str, List[Dict[str, Any]]] = {}
    for element in included:
        type_str = element.get("$type", "")
        key = type_str.rsplit(".", 1)[-1]
        index.setdefault(key, []).append(element)
    return index


def _format_date(date_obj: Optional[Dict[str, Any]]) -> Optional[str]:
    if not date_obj:
        return None
    year = date_obj.get("year")
    month = date_obj.get("month")
    if not year:
        return None
    if month:
        return f"{year:04d}-{month:02d}"
    return str(year)


def _best_profile_pictures(profile_obj: Dict[str, Any]) -> List[ProfileImage]:
    images: List[ProfileImage] = []
    picture = profile_obj.get("profilePicture") or {}
    display_image = picture.get("displayImageReference") or picture.get("displayImage")
    vector_image = None
    if isinstance(display_image, dict):
        vector_image = display_image.get("vectorImage")
    if vector_image:
        root_url = vector_image.get("rootUrl", "")
        for artifact in vector_image.get("artifacts", []):
            segment = artifact.get("fileIdentifyingUrlPathSegment", "")
            width = artifact.get("width")
            height = artifact.get("height")
            if root_url and segment:
                images.append(ProfileImage(url=root_url + segment, width=width, height=height))
    # Largest first is more useful to API consumers.
    images.sort(key=lambda img: (img.width or 0) * (img.height or 0), reverse=True)
    return images


def parse_profile_view(
    raw: Dict[str, Any],
    public_identifier: str,
    profile_url: str,
    cache_hit: bool = False,
) -> LinkedInProfile:
    included = raw.get("included", [])
    by_type = _index_by_type_suffix(included)

    profiles = by_type.get("Profile", [])
    core = profiles[0] if profiles else {}

    first_name = core.get("firstName")
    last_name = core.get("lastName")
    full_name = " ".join(part for part in [first_name, last_name] if part) or public_identifier

    location = (
        core.get("geoLocationName")
        or core.get("locationName")
        or (core.get("geoCountryName"))
    )

    experience: List[Experience] = []
    for pos in by_type.get("Position", []):
        experience.append(
            Experience(
                title=pos.get("title") or "",
                company=pos.get("companyName"),
                company_linkedin_url=(
                    f"https://www.linkedin.com/company/{pos['company']['universalName']}"
                    if isinstance(pos.get("company"), dict) and pos["company"].get("universalName")
                    else None
                ),
                location=pos.get("locationName"),
                start_date=_format_date(pos.get("timePeriod", {}).get("startDate")),
                end_date=_format_date(pos.get("timePeriod", {}).get("endDate")),
                is_current=pos.get("timePeriod", {}).get("endDate") is None,
                description=pos.get("description"),
                employment_type=pos.get("employmentType"),
            )
        )

    education: List[Education] = []
    for edu in by_type.get("Education", []):
        school = edu.get("schoolName") or (edu.get("school") or {}).get("schoolName") or ""
        education.append(
            Education(
                school=school,
                school_linkedin_url=(
                    f"https://www.linkedin.com/school/{edu['school']['universalName']}"
                    if isinstance(edu.get("school"), dict) and edu["school"].get("universalName")
                    else None
                ),
                degree=edu.get("degreeName"),
                field_of_study=edu.get("fieldOfStudy"),
                start_date=_format_date(edu.get("timePeriod", {}).get("startDate")),
                end_date=_format_date(edu.get("timePeriod", {}).get("endDate")),
                description=edu.get("description"),
            )
        )

    skills: List[Skill] = []
    for skill in by_type.get("Skill", []):
        name = skill.get("name")
        if not name:
            continue
        skills.append(
            Skill(
                name=name,
                endorsement_count=skill.get("endorsementCount"),
            )
        )

    certifications: List[Certification] = []
    for cert in by_type.get("Certification", []):
        name = cert.get("name")
        if not name:
            continue
        certifications.append(
            Certification(
                name=name,
                issuing_organization=cert.get("authority"),
                issue_date=_format_date(cert.get("timePeriod", {}).get("startDate")),
                credential_id=cert.get("licenseNumber"),
                credential_url=cert.get("url"),
            )
        )

    languages: List[Language] = []
    for lang in by_type.get("Language", []):
        name = lang.get("name")
        if not name:
            continue
        languages.append(
            Language(name=name, proficiency=lang.get("proficiency"))
        )

    return LinkedInProfile(
        public_identifier=public_identifier,
        profile_url=profile_url,
        full_name=full_name,
        first_name=first_name,
        last_name=last_name,
        headline=core.get("headline"),
        location=location,
        about=core.get("summary"),
        profile_images=_best_profile_pictures(core),
        background_image_url=None,
        experience=experience,
        education=education,
        skills=skills,
        certifications=certifications,
        languages=languages,
        connections_count=core.get("connectionsCount") or core.get("numConnections"),
        follower_count=core.get("followersCount"),
        fetched_at=datetime.now(timezone.utc).isoformat(),
        cache_hit=cache_hit,
    )
