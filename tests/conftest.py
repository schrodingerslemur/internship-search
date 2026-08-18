"""Shared fixtures. Every test runs against an isolated in-memory database."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

# Point the app at a throwaway database before anything imports settings.
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("LLM_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("HTTP_CACHE_TTL_SECONDS", "0")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.models import Base  # noqa: E402
from app.models.base import SourceKind  # noqa: E402
from app.schemas.job import RawJob  # noqa: E402
from app.schemas.preferences import default_preferences  # noqa: E402
from app.schemas.profile import default_profile  # noqa: E402

NOW = datetime(2026, 8, 18, 12, 0, 0)


@pytest.fixture(autouse=True)
def isolate_data_dir(tmp_path, monkeypatch):
    """Keep tests out of the real ./data directory.

    The file notification provider and resume storage both default to the
    application data directory; without this a test run would litter (or
    overwrite) the user's actual files.
    """
    import app.config as config
    import app.notify.providers as providers
    import app.services.resumes as resumes

    data_dir = tmp_path / "data"
    (data_dir / "resumes").mkdir(parents=True, exist_ok=True)
    (data_dir / "cache").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "RESUME_DIR", data_dir / "resumes")
    monkeypatch.setattr(config, "CACHE_DIR", data_dir / "cache")
    monkeypatch.setattr(providers, "DATA_DIR", data_dir)
    monkeypatch.setattr(resumes, "RESUME_DIR", data_dir / "resumes")
    yield


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as s:
        yield s


@pytest.fixture
def prefs():
    return default_preferences()


@pytest.fixture
def profile():
    return default_profile()


# --------------------------------------------------------------------------
# Realistic fixture data
# --------------------------------------------------------------------------

FPGA_DESCRIPTION = """
About the role

You will join our silicon engineering team as an intern for Summer 2026.

Responsibilities
* Write SystemVerilog RTL for high-speed data paths
* Build UVM testbenches and run pre-silicon verification
* Work with FPGA prototyping on Xilinx devices

Minimum Qualifications
* Currently pursuing a BS in Computer Engineering or Electrical Engineering
* Experience with Verilog or SystemVerilog
* Familiarity with Python and C++

Preferred Qualifications
* Exposure to PCIe or high-speed networking
* Prior FPGA project experience
"""

SOFTWARE_DESCRIPTION = """
Software Engineering Internship, Summer 2026.

You will build backend services in Python and Go, working on distributed
systems at scale.

Requirements
* Pursuing a BS in Computer Science
* Strong programming fundamentals
"""

SENIOR_DESCRIPTION = """
We are seeking a Senior Staff Engineer to lead our RTL design team.
Requires 10+ years of experience in ASIC design and team leadership.
"""


def make_raw(
    *,
    source: str = "greenhouse",
    kind: SourceKind = SourceKind.ATS,
    source_job_id: str = "1",
    title: str = "FPGA Design Intern",
    company: str = "NVIDIA",
    url: str | None = "https://job-boards.greenhouse.io/nvidia/jobs/12345",
    apply_url: str | None = None,
    location: str | None = "Santa Clara, CA",
    description: str | None = FPGA_DESCRIPTION,
    days_ago: int = 1,
    **kwargs,
) -> RawJob:
    """Build a realistic raw listing."""
    return RawJob(
        source=source,
        source_kind=kind,
        source_job_id=source_job_id,
        title=title,
        company=company,
        url=url,
        apply_url=apply_url,
        location=location,
        description=description,
        date_posted=NOW - timedelta(days=days_ago),
        **kwargs,
    )


@pytest.fixture
def raw_fpga():
    return make_raw()


@pytest.fixture
def cross_source_duplicates() -> list[RawJob]:
    """One NVIDIA job as advertised on five different websites."""
    return [
        make_raw(
            source="greenhouse",
            kind=SourceKind.ATS,
            source_job_id="nvidia:12345",
            url="https://job-boards.greenhouse.io/nvidia/jobs/12345",
        ),
        make_raw(
            source="linkedin",
            kind=SourceKind.JOB_BOARD,
            source_job_id="li-99",
            title="FPGA Design Intern - Summer 2026",
            company="NVIDIA Corporation",
            url="https://www.linkedin.com/jobs/view/99?trk=public&refId=abc",
            apply_url="https://boards.greenhouse.io/nvidia/jobs/12345?utm_source=linkedin",
        ),
        make_raw(
            source="adzuna",
            kind=SourceKind.AGGREGATOR,
            source_job_id="ad-7",
            url="https://www.adzuna.com/land/ad/7?utm_medium=api&v=abc",
            apply_url="https://job-boards.greenhouse.io/nvidia/jobs/12345",
        ),
        make_raw(
            source="themuse",
            kind=SourceKind.AGGREGATOR,
            source_job_id="muse-3",
            title="FPGA Design Intern (Summer)",
            url="https://www.themuse.com/jobs/nvidia/fpga-design-intern",
        ),
        make_raw(
            source="company_careers",
            kind=SourceKind.COMPANY_CAREERS,
            source_job_id="nv-1",
            url="https://www.nvidia.com/en-us/about-nvidia/careers/jobs/12345?gh_jid=12345",
        ),
    ]


@pytest.fixture
def distinct_jobs() -> list[RawJob]:
    """Jobs that look similar but must never be merged."""
    return [
        make_raw(source_job_id="a1", title="FPGA Engineer Intern",
                 company="Acme Semi", location="Austin, TX",
                 url="https://job-boards.greenhouse.io/acme/jobs/777"),
        make_raw(source_job_id="a2", title="FPGA Engineer Intern",
                 company="Acme Semi", location="Santa Clara, CA",
                 url="https://job-boards.greenhouse.io/acme/jobs/778"),
        make_raw(source_job_id="a3", title="Hardware Verification Intern",
                 company="Acme Semi", location="Austin, TX",
                 url="https://job-boards.greenhouse.io/acme/jobs/779"),
        make_raw(source_job_id="a4", title="Hardware Design Intern",
                 company="Acme Semi", location="Austin, TX",
                 url="https://job-boards.greenhouse.io/acme/jobs/780"),
        make_raw(source_job_id="a5", title="FPGA Engineer Intern - Fall 2026",
                 company="Acme Semi", location="Austin, TX",
                 description="Fall 2026 internship for FPGA engineering.",
                 url="https://job-boards.greenhouse.io/acme/jobs/781"),
    ]
