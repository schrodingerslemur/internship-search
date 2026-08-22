"""Cross-source deduplication.

Ten listings for the same job must collapse into one canonical job -- while two
genuinely different positions must stay apart. Those goals pull in opposite
directions, so merging happens in escalating stages of decreasing certainty,
and every stage above the authoritative ones must first clear
:func:`merge_guard`.

Stages
------
0. ``(source, source_job_id)``  -- same listing seen twice in one run.
1. **Canonical URL** -- authoritative; tracking noise already stripped.
2. **ATS identity** -- authoritative; provider + board + requisition.
3. **Fingerprint** -- company + title core + location + employment type.
4. **Similarity** -- title tokens, description shingles, requisition, dates.
5. **LLM adjudication** -- only the narrow uncertain band, and only when
   explicitly enabled.

Stages 0-2 are exact-identity matches and bypass the guard. Stages 3-5 are
inferential and are gated by :func:`merge_guard`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from app.models.base import EmploymentType, utcnow
from app.pipeline.extract import locations_are_compatible, parse_location
from app.pipeline.textutil import jaccard, token_set_ratio
from app.schemas.job import JobCluster, NormalizedJob

#: Similarity at or above this merges without asking anything further.
MERGE_THRESHOLD = 0.88
#: Below this, two postings are considered different and never merged.
DIFFERENT_THRESHOLD = 0.70

#: Discriminator groups that are mutually exclusive: if two postings each
#: declare a *different* member of the same axis, they are different jobs.
EXCLUSIVE_AXES: tuple[frozenset[str], ...] = (
    frozenset({"summer", "fall", "spring", "winter"}),
    frozenset({"verification", "design", "physical", "analog"}),
    frozenset({"software", "hardware", "firmware"}),
    frozenset({"intern", "coop"}),
    frozenset({"phd", "masters"}),
)


class UnionFind:
    """Disjoint-set forest with path compression."""

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))
        self._rank = [0] * size

    def find(self, x: int) -> int:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1
        return True

    def groups(self) -> dict[int, list[int]]:
        out: dict[int, list[int]] = {}
        for i in range(len(self._parent)):
            out.setdefault(self.find(i), []).append(i)
        return out


@dataclass
class MergeRecord:
    """Provenance for one merge decision, persisted for debuggability."""

    left: str
    right: str
    stage: str
    verdict: str
    confidence: float
    reason: str


@dataclass
class DedupResult:
    clusters: list[JobCluster]
    records: list[MergeRecord] = field(default_factory=list)
    llm_calls: int = 0

    @property
    def duplicates_removed(self) -> int:
        return sum(c.size - 1 for c in self.clusters)


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------


def merge_guard(a: NormalizedJob, b: NormalizedJob) -> tuple[bool, str]:
    """Veto merges that would conflate distinct positions.

    Returns ``(allowed, reason)``. This is the safety net behind requirement
    "avoid over-deduplication": an FPGA intern role in Austin and one in Santa
    Clara, or a verification role and a design role, must never collapse.
    """
    if a.company_slug != b.company_slug:
        return False, "different company"

    # Distinct, explicitly-stated requisition IDs mean distinct openings.
    if a.requisition_id and b.requisition_id:
        if a.requisition_id.strip().lower() != b.requisition_id.strip().lower():
            return False, "different requisition ids"

    # Conflicting ATS identities: both known and unequal.
    if a.ats_identity and b.ats_identity and a.ats_identity != b.ats_identity:
        return False, "different ATS identities"

    # Two listings from the *same* source, at different URLs, are separate
    # openings rather than duplicates: a board does not advertise one job twice
    # under two ids. Without this, a company posting many same-titled roles in
    # one city (13 "Software Engineer Intern" reqs at one employer, say)
    # collapses into a single job and the rest are lost. Cross-source
    # duplicates -- the ones this system exists to merge -- are unaffected,
    # and the exact-identity stages bypass this guard entirely.
    if (
        a.source == b.source
        and a.canonical_url
        and b.canonical_url
        and a.canonical_url != b.canonical_url
    ):
        return False, "same source, distinct postings"

    # Mutually exclusive discriminator axes (summer vs fall, DV vs design...).
    for axis in EXCLUSIVE_AXES:
        left = a.discriminators & axis
        right = b.discriminators & axis
        if left and right and left != right:
            return False, f"conflicting {sorted(left)} vs {sorted(right)}"

    # Incompatible employment types, when both are known.
    if (
        a.employment_type is not EmploymentType.UNKNOWN
        and b.employment_type is not EmploymentType.UNKNOWN
        and a.employment_type != b.employment_type
    ):
        return False, "different employment types"

    # Different metros are different jobs.
    if not locations_are_compatible(parse_location(a.location_raw), parse_location(b.location_raw)):
        return False, "incompatible locations"

    return True, "ok"


def similarity(a: NormalizedJob, b: NormalizedJob) -> tuple[float, str]:
    """Weighted similarity in ``[0, 1]`` with a human-readable rationale."""
    title_sim = token_set_ratio(a.title, b.title)
    desc_sim = jaccard(a.description_shingles, b.description_shingles)

    have_desc = bool(a.description_shingles and b.description_shingles)
    if have_desc:
        score = 0.55 * title_sim + 0.45 * desc_sim
    else:
        # With no description to compare, the title carries the decision, so
        # demand more of it and stay conservative.
        score = title_sim * 0.82

    bits = [f"title={title_sim:.2f}"]
    if have_desc:
        bits.append(f"desc={desc_sim:.2f}")

    # Matching requisition IDs are near-proof.
    if a.requisition_id and b.requisition_id and a.requisition_id.lower() == b.requisition_id.lower():
        score = min(1.0, score + 0.25)
        bits.append("req-id match")

    # Same normalised location adds confidence; unknown never penalises.
    if a.location_key and a.location_key == b.location_key:
        score = min(1.0, score + 0.05)
        bits.append("same location")

    # Postings within a few days of each other are more likely one job.
    if a.date_posted and b.date_posted:
        delta = abs((a.date_posted - b.date_posted).days)
        if delta <= 3:
            score = min(1.0, score + 0.03)
            bits.append(f"posted {delta}d apart")
        elif delta > 90:
            score -= 0.05
            bits.append("posted far apart")

    # Agreeing salaries corroborate.
    if a.salary_min and b.salary_min and abs(a.salary_min - b.salary_min) < 0.01:
        score = min(1.0, score + 0.04)
        bits.append("same salary")

    return max(0.0, min(1.0, score)), ", ".join(bits)


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

#: Signature of an optional LLM adjudicator: returns (verdict, confidence).
LlmAdjudicator = Callable[[NormalizedJob, NormalizedJob], tuple[str, float]]


def deduplicate(
    jobs: Iterable[NormalizedJob],
    *,
    llm_adjudicator: LlmAdjudicator | None = None,
    max_llm_calls: int = 0,
    max_block_size: int = 60,
) -> DedupResult:
    """Collapse listings into canonical jobs.

    ``llm_adjudicator`` is consulted only for pairs scoring in the uncertain
    band, and at most ``max_llm_calls`` times per run.
    """
    items = list(jobs)
    n = len(items)
    records: list[MergeRecord] = []
    if n == 0:
        return DedupResult(clusters=[])

    uf = UnionFind(n)
    methods: dict[str, str] = {}
    confidences: dict[str, float] = {}

    def link(i: int, j: int, stage: str, confidence: float, reason: str) -> None:
        if uf.union(i, j):
            key = items[j].key
            methods.setdefault(key, stage)
            confidences.setdefault(key, confidence)
            records.append(
                MergeRecord(items[i].key, items[j].key, stage, "same", confidence, reason)
            )

    # ---- Stage 0/1/2: exact identity keys (authoritative) ----
    exact_stages: tuple[tuple[str, Callable[[NormalizedJob], str | None]], ...] = (
        ("exact_id", lambda j: f"{j.source}|{j.source_job_id}"),
        ("url", lambda j: j.canonical_url_hash),
        ("ats_identity", lambda j: j.ats_identity),
    )
    for stage, keyfn in exact_stages:
        buckets: dict[str, int] = {}
        for idx, job in enumerate(items):
            key = keyfn(job)
            if not key:
                continue
            first = buckets.get(key)
            if first is None:
                buckets[key] = idx
            else:
                link(first, idx, stage, 1.0, f"identical {stage}")

    # ---- Stage 3: deterministic fingerprint (guarded) ----
    fp_buckets: dict[str, list[int]] = {}
    for idx, job in enumerate(items):
        if job.fingerprint:
            fp_buckets.setdefault(job.fingerprint, []).append(idx)
    for _, idxs in fp_buckets.items():
        if len(idxs) < 2:
            continue
        head = idxs[0]
        for other in idxs[1:]:
            if uf.find(head) == uf.find(other):
                continue
            allowed, reason = merge_guard(items[head], items[other])
            if allowed:
                link(head, other, "fingerprint", 0.95, "identical fingerprint")
            else:
                records.append(
                    MergeRecord(
                        items[head].key, items[other].key, "fingerprint", "different", 0.0, reason
                    )
                )

    # ---- Stage 3b: identical content (guarded) ----
    # Same company, title, location and body text. Distinct from the
    # fingerprint stage, which ignores the description: a listing that arrives
    # from an aggregator as a bare title with no body shares a fingerprint with
    # every other opening of that name, but shares a *content hash* only with
    # the posting it is actually a copy of.
    #
    # The guard does the important work here. Employers routinely advertise
    # many separate requisitions under one boilerplate description -- eleven
    # "Graduate Engineer Trainee" openings with identical text and eleven
    # distinct requisition IDs -- and those are real, separate jobs. Merging
    # them on text alone would silently delete ten of them.
    content_buckets: dict[str, list[int]] = {}
    for idx, job in enumerate(items):
        if job.content_hash:
            content_buckets.setdefault(job.content_hash, []).append(idx)
    for _, idxs in content_buckets.items():
        if len(idxs) < 2:
            continue
        head = idxs[0]
        for other in idxs[1:]:
            if uf.find(head) == uf.find(other):
                continue
            allowed, reason = merge_guard(items[head], items[other])
            if allowed:
                link(head, other, "content_hash", 0.97, "identical content")
            else:
                records.append(
                    MergeRecord(
                        items[head].key, items[other].key, "content_hash", "different", 0.0, reason
                    )
                )

    # ---- Stage 4/5: similarity within company blocks (guarded) ----
    company_blocks: dict[str, list[int]] = {}
    for idx, job in enumerate(items):
        if job.company_slug:
            company_blocks.setdefault(job.company_slug, []).append(idx)

    llm_calls = 0
    for _, idxs in company_blocks.items():
        if len(idxs) < 2 or len(idxs) > max_block_size:
            # Oversized blocks are skipped: an O(n^2) sweep over a huge employer
            # adds cost without accuracy, and stages 0-3 already caught the
            # confident matches.
            continue
        for pos, i in enumerate(idxs):
            for j in idxs[pos + 1 :]:
                if uf.find(i) == uf.find(j):
                    continue
                allowed, guard_reason = merge_guard(items[i], items[j])
                if not allowed:
                    continue
                score, reason = similarity(items[i], items[j])
                if score >= MERGE_THRESHOLD:
                    link(i, j, "similarity", score, reason)
                elif score >= DIFFERENT_THRESHOLD:
                    if llm_adjudicator and llm_calls < max_llm_calls:
                        llm_calls += 1
                        try:
                            verdict, confidence = llm_adjudicator(items[i], items[j])
                        except Exception as exc:  # pragma: no cover - provider errors
                            records.append(
                                MergeRecord(
                                    items[i].key, items[j].key, "llm", "uncertain", 0.0,
                                    f"adjudicator error: {exc}",
                                )
                            )
                            continue
                        if verdict == "SAME" and confidence >= 0.8:
                            link(i, j, "llm", confidence, f"LLM: same ({reason})")
                        else:
                            records.append(
                                MergeRecord(
                                    items[i].key, items[j].key, "llm", verdict.lower(),
                                    confidence, reason,
                                )
                            )
                    else:
                        records.append(
                            MergeRecord(
                                items[i].key, items[j].key, "similarity", "uncertain", score, reason
                            )
                        )

    # ---- Build clusters ----
    clusters: list[JobCluster] = []
    for _, members in uf.groups().items():
        member_jobs = [items[i] for i in members]
        member_jobs.sort(key=_authority_rank)
        clusters.append(
            JobCluster(
                members=member_jobs,
                merge_methods={m.key: methods.get(m.key, "primary") for m in member_jobs},
                merge_confidence={m.key: confidences.get(m.key, 1.0) for m in member_jobs},
            )
        )
    clusters.sort(key=lambda c: (-c.size, c.members[0].company_slug, c.members[0].title_core))
    return DedupResult(clusters=clusters, records=records, llm_calls=llm_calls)


def _authority_rank(job: NormalizedJob) -> tuple:
    """Order cluster members so the most authoritative listing leads.

    The leader supplies the canonical title/description, and its URL is the
    default apply link (a company/ATS page beats an aggregator).
    """
    from app.models.base import SOURCE_KIND_RANK

    kind_rank = SOURCE_KIND_RANK.get(str(job.source_kind), 5)
    has_desc = 0 if (job.description and len(job.description) > 200) else 1
    return (kind_rank, has_desc, -(len(job.description or "")), job.source)


def elect_application_url(cluster: JobCluster) -> tuple[str, str | None]:
    """Pick the best application URL and posting URL for a cluster.

    Priority: the employer's own careers page, then the ATS page, then a major
    board, then an aggregator -- so ``Apply`` sends the user to the company
    whenever the company was among the sources.
    """
    from app.models.base import SOURCE_KIND_RANK
    from app.pipeline.identity import classify_url_host

    host_rank = {"company_careers": 0, "ats": 1, "job_board": 2, "unknown": 4}

    best: tuple[tuple[int, int, int], str] | None = None
    posting_url: str | None = None
    for member in cluster.members:
        for candidate in (member.apply_url, member.url):
            if not candidate:
                continue
            rank = (
                host_rank.get(classify_url_host(candidate), 4),
                SOURCE_KIND_RANK.get(str(member.source_kind), 5),
                0 if candidate == member.apply_url else 1,
            )
            if best is None or rank < best[0]:
                best = (rank, candidate)
        if posting_url is None and member.url:
            posting_url = member.url

    if best is None:
        leader = cluster.members[0]
        return (leader.url or leader.apply_url or ""), leader.url
    return best[1], posting_url


def merge_cluster_facts(cluster: JobCluster, *, now: datetime | None = None) -> NormalizedJob:
    """Fold a cluster into one best-informed representative listing.

    Field-level rule: prefer the authoritative member, but fill any gap from
    whichever member has the fact. A confident value from an aggregator beats
    a missing value from the company site.
    """
    now = now or utcnow()
    members = cluster.members
    base = members[0].model_copy(deep=True)

    for member in members[1:]:
        for field_name in (
            "description", "requirements", "responsibilities", "preferred_qualifications",
            "salary_raw", "location_raw", "city", "state", "country", "requisition_id",
            "company_url", "salary_currency", "salary_period",
        ):
            if not getattr(base, field_name) and getattr(member, field_name):
                setattr(base, field_name, getattr(member, field_name))

        if base.salary_min is None and member.salary_min is not None:
            base.salary_min = member.salary_min
            base.salary_max = member.salary_max

        # Earliest posting date wins: it is when the job really appeared.
        if member.date_posted and (base.date_posted is None or member.date_posted < base.date_posted):
            base.date_posted = member.date_posted
        if member.date_updated and (base.date_updated is None or member.date_updated > (base.date_updated or member.date_updated)):
            base.date_updated = member.date_updated

        # An explicit deadline always beats no deadline; never invent one.
        if member.deadline and member.deadline_is_explicit and not base.deadline_is_explicit:
            base.deadline = member.deadline
            base.deadline_is_explicit = True

        # Any concrete sponsorship statement beats UNKNOWN.
        from app.models.base import SponsorshipStatus

        if base.sponsorship is SponsorshipStatus.UNKNOWN and member.sponsorship is not SponsorshipStatus.UNKNOWN:
            base.sponsorship = member.sponsorship
            base.sponsorship_evidence = member.sponsorship_evidence

        if base.employment_type is EmploymentType.UNKNOWN and member.employment_type is not EmploymentType.UNKNOWN:
            base.employment_type = member.employment_type

        from app.models.base import RemoteStatus

        if base.remote_status is RemoteStatus.UNKNOWN and member.remote_status is not RemoteStatus.UNKNOWN:
            base.remote_status = member.remote_status

        if base.experience_required_years is None and member.experience_required_years is not None:
            base.experience_required_years = member.experience_required_years

        base.skills = list(dict.fromkeys(base.skills + member.skills))
        base.terms = list(dict.fromkeys(base.terms + member.terms))
        base.locations = list(dict.fromkeys(base.locations + member.locations))
        base.degree_requirements = list(
            dict.fromkeys(base.degree_requirements + member.degree_requirements)
        )
        if not base.ats_identity and member.ats_identity:
            base.ats_identity = member.ats_identity

    apply_url, posting_url = elect_application_url(cluster)
    base.apply_url = apply_url or base.apply_url
    base.url = posting_url or base.url
    return base
