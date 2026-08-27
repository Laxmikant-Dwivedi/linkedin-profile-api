from typing import List, Optional

from pydantic import BaseModel, Field


class ProfileImage(BaseModel):
    url: str
    width: Optional[int] = None
    height: Optional[int] = None


class Experience(BaseModel):
    title: str
    company: Optional[str] = None
    company_linkedin_url: Optional[str] = None
    location: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    is_current: bool = False
    description: Optional[str] = None
    employment_type: Optional[str] = None


class Education(BaseModel):
    school: str
    school_linkedin_url: Optional[str] = None
    degree: Optional[str] = None
    field_of_study: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    description: Optional[str] = None


class Certification(BaseModel):
    name: str
    issuing_organization: Optional[str] = None
    issue_date: Optional[str] = None
    credential_id: Optional[str] = None
    credential_url: Optional[str] = None


class Language(BaseModel):
    name: str
    proficiency: Optional[str] = None


class Skill(BaseModel):
    name: str
    endorsement_count: Optional[int] = None


class LinkedInProfile(BaseModel):
    public_identifier: str
    profile_url: str
    full_name: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    headline: Optional[str] = None
    location: Optional[str] = None
    about: Optional[str] = None
    profile_images: List[ProfileImage] = Field(default_factory=list)
    background_image_url: Optional[str] = None
    experience: List[Experience] = Field(default_factory=list)
    education: List[Education] = Field(default_factory=list)
    skills: List[Skill] = Field(default_factory=list)
    certifications: List[Certification] = Field(default_factory=list)
    languages: List[Language] = Field(default_factory=list)
    connections_count: Optional[int] = None
    follower_count: Optional[int] = None
    fetched_at: str
    cache_hit: bool = False


class ProfileRequest(BaseModel):
    url: str


class ProfileUrlMatch(BaseModel):
    public_identifier: str
    profile_url: str
    matched_query: str
    fetched_at: str


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None
    alert: Optional[str] = Field(
        default=None,
        description="Present when the failure is a known, documented limitation "
        "(see README) rather than an unexpected error.",
    )
