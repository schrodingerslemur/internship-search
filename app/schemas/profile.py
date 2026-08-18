"""Candidate profile transport schema."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CandidateProfileData(BaseModel):
    """The candidate that jobs are scored against."""

    school: str | None = None
    degree: str | None = None
    major: str | None = None
    minor: str | None = None
    graduation_year: int | None = None
    graduation_month: int | None = None
    gpa: float | None = None

    technical_skills: list[str] = Field(default_factory=list)
    programming_languages: list[str] = Field(default_factory=list)
    hardware_skills: list[str] = Field(default_factory=list)
    software_skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)

    research_experience: str | None = None
    previous_internships: list[str] = Field(default_factory=list)
    projects: list[str] = Field(default_factory=list)
    publications: list[str] = Field(default_factory=list)
    coursework: list[str] = Field(default_factory=list)

    preferred_industries: list[str] = Field(default_factory=list)
    work_authorization: str | None = None
    requires_sponsorship: bool | None = None
    security_clearance: str | None = None
    preferred_locations: list[str] = Field(default_factory=list)
    willing_to_relocate: bool = True
    summary: str | None = None

    def all_skills(self) -> list[str]:
        """Every skill token across all buckets, de-duplicated, order-preserved."""
        merged: list[str] = []
        for bucket in (
            self.technical_skills,
            self.programming_languages,
            self.hardware_skills,
            self.software_skills,
            self.tools,
        ):
            merged.extend(bucket)
        seen: set[str] = set()
        out: list[str] = []
        for item in merged:
            key = str(item).strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(str(item).strip())
        return out

    @classmethod
    def from_orm_profile(cls, profile: object | None) -> CandidateProfileData:
        """Build from a ``CandidateProfile`` row, tolerating ``None``."""
        if profile is None:
            return cls()
        data = {}
        for name in cls.model_fields:
            if hasattr(profile, name):
                data[name] = getattr(profile, name)
        return cls(**{k: v for k, v in data.items() if v is not None})


def default_profile() -> CandidateProfileData:
    """A starter profile matching the hardware focus this system was built for.

    Fully editable from the profile page; nothing else in the codebase depends
    on these values.
    """
    return CandidateProfileData(
        school="Carnegie Mellon University",
        degree="Bachelors",
        major="Electrical and Computer Engineering",
        graduation_year=2027,
        technical_skills=[
            "FPGA", "RTL", "SystemVerilog", "Verilog", "UVM",
            "Hardware Verification", "Computer Architecture", "Digital Design",
            "Embedded Systems", "SoC",
        ],
        programming_languages=["Python", "C++", "C", "SystemVerilog", "Verilog"],
        hardware_skills=["FPGA", "Vivado", "Quartus", "RTL", "Timing Analysis", "PCIe"],
        software_skills=["Git", "Linux", "Docker"],
        tools=["Vivado", "ModelSim", "Verilator", "Git"],
        preferred_industries=["Semiconductor", "AI", "Quant trading", "Robotics"],
        work_authorization="US Citizen",
        requires_sponsorship=False,
        willing_to_relocate=True,
    )
