"""People, organizations, the timeline and decisions.

Part of :class:`server.core.memory_engine.MemoryEngine`, split out to keep
each file readable. Mixins rather than separate services: the methods use the
engine's own state throughout, and moving the bodies unchanged is what makes
the split verifiable.
"""

from __future__ import annotations


from .helpers import _event_date


import asyncio


class MemoryWorkspaceMixin:
    """People, organizations, the timeline and decisions."""

    # People/orgs cached aggregates, keyed by the store's data_version. Every
    # page load polls both endpoints; rebuilding them from ten thousand
    # memories per request (text scanning over the whole corpus) is the kind
    # of lag that makes the dashboard look frozen. Same semantics as the
    # derived-state views: one cheap PRAGMA guard per read, an O(corpus)
    # rebuild only when the data version moved.
    _people_orgs_cache_version: int | None = None
    _people_orgs_cache: tuple[list[dict], list[dict]] | None = None

    async def _cached_people_orgs(self) -> tuple[list[dict], list[dict]]:
        from ..people import aggregate_people
        from ..organizations import aggregate_organizations

        version = await self.db.data_version()
        if self._people_orgs_cache_version == version and self._people_orgs_cache is not None:
            return self._people_orgs_cache
        # Content and metadata only: these scans aggregate people/organizations
        # and timeline entries, and skip the embedding column so a 10k-row read
        # does not JSON-decode vectors it never looks at.
        memories = await self.episodic.search(limit=10000, fetch_embeddings=False)
        # Text-entity extraction is pure CPU over the whole corpus; on the
        # event loop it starves every concurrent request for seconds (the
        # dashboard polls people/orgs on every page), so it runs in a worker
        # thread and only the await points stay on the loop.
        people = await asyncio.to_thread(aggregate_people, memories)
        orgs = await asyncio.to_thread(aggregate_organizations, memories)
        self._people_orgs_cache_version = version
        self._people_orgs_cache = (people, orgs)
        return self._people_orgs_cache

    async def list_people(self, limit: int = 200) -> list[dict]:
        """Distinct people mentioned across memories (calendar attendees,
        email senders/recipients, transcript speakers), most-frequent first.
        Each entry drops the internal ``memory_ids`` list for the summary view."""
        people, _orgs = await self._cached_people_orgs()
        return [{k: v for k, v in p.items() if k != "memory_ids"} for p in people[:limit]]

    async def get_person(self, query: str) -> dict | None:
        """Resolve a name/email to a person and return their profile plus the
        memories that mention them (most recent first)."""
        from ..people import aggregate_people, find_person_key

        # Content and metadata only: these scans aggregate people/organizations
        # and timeline entries, and skip the embedding column so a 10k-row read
        # does not JSON-decode vectors it never looks at.
        memories = await self.episodic.search(limit=10000, fetch_embeddings=False)
        people = await asyncio.to_thread(aggregate_people, memories)
        key = find_person_key(people, query)
        if key is None:
            return None
        person = next(p for p in people if p["key"] == key)
        ids = set(person["memory_ids"])
        by_id = {m.id: m for m in memories}
        person_memories = [by_id[i] for i in ids if i in by_id]
        person_memories.sort(key=lambda m: m.created_at or "", reverse=True)
        profile = {k: v for k, v in person.items() if k != "memory_ids"}
        return {
            "person": profile,
            "memories": [m.model_dump(exclude={"embedding"}) for m in person_memories],
        }

    async def list_organizations(self, limit: int = 200) -> list[dict]:
        """Distinct organizations across all memories, grouped by the email
        domain of the people mentioned (calendar attendees, email
        senders/recipients, transcript speakers), most-frequent first. Each
        entry drops the internal ``memory_ids`` list and caps ``people`` to
        50 names so large organizations don't blow up the summary view."""
        _people, orgs = await self._cached_people_orgs()
        result = []
        for o in orgs[:limit]:
            entry = {k: v for k, v in o.items() if k != "memory_ids"}
            entry["people"] = entry["people"][:50]
            result.append(entry)
        return result

    async def get_organization(self, query: str) -> dict | None:
        """Resolve a domain/name to an organization and return its profile
        plus the memories that reference someone from it (most recent
        first)."""
        from ..organizations import aggregate_organizations, find_org_key

        # Content and metadata only: these scans aggregate people/organizations
        # and timeline entries, and skip the embedding column so a 10k-row read
        # does not JSON-decode vectors it never looks at.
        memories = await self.episodic.search(limit=10000, fetch_embeddings=False)
        orgs = await asyncio.to_thread(aggregate_organizations, memories)
        key = find_org_key(orgs, query)
        if key is None:
            return None
        org = next(o for o in orgs if o["key"] == key)
        ids = set(org["memory_ids"])
        by_id = {m.id: m for m in memories}
        org_memories = [by_id[i] for i in ids if i in by_id]
        org_memories.sort(key=lambda m: m.created_at or "", reverse=True)
        profile = {k: v for k, v in org.items() if k != "memory_ids"}
        profile["people"] = profile["people"][:50]
        return {
            "organization": profile,
            "memories": [m.model_dump(exclude={"embedding"}) for m in org_memories],
        }

    async def timeline(self, days: int = 30, project: str | None = None) -> list[dict]:
        """Group episodic memories by day so a user can see "what happened
        this/last week". Uses ``metadata.captured_at`` (when a calendar event
        or email actually happened) over ``created_at`` (when it was
        captured into LEVH) if present. Returns day-groups sorted
        most-recent-first."""
        from datetime import datetime, timedelta, timezone

        memories = await self.episodic.search(
            project=project, limit=10000, fetch_embeddings=False
        )

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()

        days_map: dict[str, list] = {}
        for mem in memories:
            day = _event_date(mem)
            if not day or day < cutoff:
                continue
            days_map.setdefault(day, []).append(mem)

        groups = []
        for day, mems in days_map.items():
            mems.sort(key=lambda m: m.created_at or "", reverse=True)
            items = []
            for m in mems[:20]:
                mtype = m.memory_type.value if hasattr(m.memory_type, "value") else str(m.memory_type)
                items.append(
                    {
                        "id": m.id,
                        "summary": m.content.split("\n", 1)[0][:100],
                        "source": m.source,
                        "memory_type": mtype,
                    }
                )
            groups.append({"date": day, "count": len(mems), "items": items})

        groups.sort(key=lambda g: g["date"], reverse=True)
        return groups

    async def list_decisions(
        self,
        project: str | None = None,
        days: int = 90,
        limit: int = 50,
    ) -> list[dict]:
        """Detect decision statements in episodic memory content within the
        last ``days`` days — "what did we decide, and when/where". Mirrors
        ``briefing()``'s commitment-detection logic exactly (same event-date
        convention, cutoff window, and sentence-extraction approach) but with
        a marker regex tuned for decisions ("we decided", "agreed to",
        "karar verdik", ...) instead of open action items.

        Args:
            project: Optional project filter.
            days: Lookback window (in days). Default 90 (max 365).
            limit: Max decisions to return. Default 50.
        """
        import re
        from datetime import datetime, timedelta, timezone

        days = min(max(days, 1), 365)
        limit = min(max(limit, 1), 200)
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=days)).date().isoformat()

        memories = await self.episodic.search(
            project=project, limit=10000, fetch_embeddings=False
        )

        marker_pattern = re.compile(
            r"\bwe decided\b|\bdecided to\b|\bdecision\b|\bwe agreed\b|\bagreed to\b|"
            r"\bwe['’]?re going with\b|\bgoing with\b|\bwe chose\b|\bchose to\b|"
            r"\bwe will use\b|\bkarar ver|kararlaştır|\bkarar:|üzerinde anlaş|seçtik",
            re.IGNORECASE,
        )

        recent = [m for m in memories if _event_date(m) and _event_date(m) >= cutoff]
        recent.sort(key=lambda m: _event_date(m), reverse=True)

        decisions: list[dict] = []
        seen_text: set[str] = set()
        for m in recent:
            content = m.content or ""
            if not marker_pattern.search(content):
                continue
            segments: list[str] = []
            for line in content.split("\n"):
                segments.extend(line.split(". "))
            sentence = next((s for s in segments if marker_pattern.search(s)), content)
            text = sentence.strip()[:180]
            if not text or text in seen_text:
                continue
            seen_text.add(text)
            decisions.append(
                {
                    "id": m.id,
                    "text": text,
                    "source": m.source,
                    "date": _event_date(m),
                    "project": m.project,
                }
            )
            if len(decisions) >= limit:
                break

        return decisions
