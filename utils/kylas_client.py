import os
import re
import json
import time
import threading
import requests
from datetime import datetime, timezone
from typing import Dict, List, Optional

_TAG = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    if not text:
        return ""
    return _TAG.sub(" ", str(text)).replace("&nbsp;", " ").strip()

KYLAS_BASE = "https://api.kylas.io/v1"
PAGE_SIZE  = 200

_COMPANY_FIELDS = [
    "id", "name", "industry", "ownedBy", "ownerId",
    "createdAt", "updatedAt", "customFieldValues",
]

_CONTACT_FIELDS = [
    "id", "name", "firstName", "lastName", "emails", "phoneNumbers",
    "company", "designation", "ownedBy", "ownerId", "updatedBy",
    "linkedin", "city", "state", "country", "source",
    "createdAt", "updatedAt", "customFieldValues",
]

_DEAL_FIELDS = [
    "id", "name", "estimatedValue", "actualValue", "pipeline", "pipelineStage",
    "associatedContacts", "company", "estimatedClosureOn",
    "ownedBy", "ownerId", "source", "forecastingType",
    "createdAt", "updatedAt", "customFieldValues",
]


class KylasClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "api-key": os.environ["KYLAS_API_KEY"],
            "Content-Type": "application/json",
        })
        self._delay = 0.12       # min seconds between request starts (global)
        self._max_retries = 5    # automatic retries on HTTP 429 (rate limit)
        self._pick_style = {}    # cf_key -> dropdown encoding that worked ("id"/"idobj"/"idname")
        self._pace_lock = threading.Lock()   # serialises slot assignment across threads
        self._next_call_at = 0.0             # monotonic time of the next allowed request

    def _pace(self):
        """Thread-safe global rate gate.

        Hands each caller a request slot spaced >= self._delay apart, then
        sleeps (outside the lock) until that slot. This caps the aggregate
        request rate at ~1/_delay no matter how many worker threads are running,
        so parallelism overlaps network round-trips without multiplying the rate
        and tripping Kylas's 429 limit.
        """
        with self._pace_lock:
            slot = max(time.monotonic(), self._next_call_at)
            self._next_call_at = slot + self._delay
        wait = slot - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """One Kylas HTTP call with pacing + automatic 429 (rate-limit) backoff.

        `path` is relative to KYLAS_BASE. On HTTP 429 it waits — honoring the
        Retry-After header when present, otherwise exponential backoff — and
        retries up to self._max_retries times, then returns the final response.
        Callers keep using r.raise_for_status()/r.ok exactly as before, so a
        transient rate limit no longer loses an update or aborts a whole sync.
        """
        kwargs.setdefault("timeout", 30)
        url = f"{KYLAS_BASE}/{path}"
        attempt = 0
        while True:
            self._pace()
            r = self.session.request(method, url, **kwargs)
            if r.status_code != 429 or attempt >= self._max_retries:
                return r
            ra = r.headers.get("Retry-After")
            try:
                wait = float(ra) if ra else min(2 ** attempt, 30)
            except (TypeError, ValueError):
                wait = min(2 ** attempt, 30)
            print(f"  [Kylas] 429 rate-limited on {method} {path} — "
                  f"retry {attempt + 1}/{self._max_retries} in {wait:.0f}s")
            time.sleep(wait)
            attempt += 1

    @staticmethod
    def _raise_for_status(r) -> None:
        """raise_for_status() that surfaces Kylas's error body in the message.

        Kylas returns the actual reason it rejected a write (which field/value
        was bad) in the response body; the stock HTTPError drops it, leaving an
        opaque '400 Bad Request' / '500 Server Error'. Including it is what makes
        a failed company/contact PUT diagnosable instead of guesswork.
        """
        if r.status_code < 400:
            return
        try:
            j = r.json()
            detail = (j.get("message") or j.get("error") or j.get("errorMessage")
                      or j.get("detail") or str(j)) if isinstance(j, dict) else str(j)
        except Exception:
            detail = r.text or ""
        detail = " ".join(str(detail).split())[:500]
        raise requests.HTTPError(
            f"{r.status_code} {r.reason} for url: {r.url}"
            + (f" — {detail}" if detail else ""),
            response=r,
        )

    def _get(self, path: str, params: dict = None) -> dict:
        r = self._request("GET", path, params=params or {})
        self._raise_for_status(r)
        return r.json()

    def _search_all(self, entity: str, fields: list, since: str = None) -> List[dict]:
        """
        Fetch all records of `entity` from Kylas.

        since (ISO string, e.g. "2026-06-03T12:00:00Z"):
            Stop fetching once we see records with updatedAt < since.
            Records are returned sorted updatedAt DESC, so the first record
            older than the cutoff means all subsequent pages are also older.
            Use for daily incremental syncs (~72 hours window) — drastically
            reduces API calls when only a small fraction was updated recently.
            Pass None for a full sync (initial import / after a gap).
        """
        cutoff = None
        if since:
            cutoff = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)

        records, page = [], 0
        while True:
            r = self._request(
                "POST", f"search/{entity}",
                params={"page": page, "size": PAGE_SIZE, "sort": "updatedAt,desc"},
                json={"fields": fields, "jsonRule": None},
                timeout=60,
            )
            r.raise_for_status()
            resp    = r.json()
            content = resp.get("content", [])

            if cutoff:
                kept, stop = [], False
                for item in content:
                    upd = item.get("updatedAt", "")
                    if upd:
                        try:
                            item_dt = datetime.fromisoformat(upd.replace("Z", "+00:00"))
                            if item_dt.tzinfo is None:
                                item_dt = item_dt.replace(tzinfo=timezone.utc)
                            if item_dt < cutoff:
                                stop = True
                                break   # all following records on this & later pages are older
                        except (ValueError, TypeError):
                            pass
                    kept.append(item)
                records.extend(kept)
                if stop:
                    print(f"  [{entity}] incremental stop at page {page} — {len(records)} records since cutoff")
                    break
            else:
                records.extend(content)

            if page >= resp.get("totalPages", 1) - 1 or not content:
                break
            page += 1

        return records

    def get_companies(self, since: str = None) -> List[dict]:
        return self._search_all("company", _COMPANY_FIELDS, since=since)

    def get_contacts(self, since: str = None) -> List[dict]:
        return self._search_all("contact", _CONTACT_FIELDS, since=since)

    def get_deals(self, since: str = None) -> List[dict]:
        return self._search_all("deal", _DEAL_FIELDS, since=since)

    def get_company(self, cid: int) -> dict:
        resp = self._get(f"companies/{cid}")
        return resp.get("data", resp)

    def get_contact(self, cid: int) -> dict:
        resp = self._get(f"contacts/{cid}")
        return resp.get("data", resp)

    def get_deal(self, did: int) -> dict:
        resp = self._get(f"deals/{did}")
        return resp.get("data", resp)

    def get_all_notes(self, max_pages: int = 20, page_size: int = 100) -> List[dict]:
        """
        Fetch all notes for the tenant via POST /notes/search, newest first.

        This is the only working notes endpoint on this tenant (GET /notes,
        /notes/relation, and /search/note all fail). Each note carries a
        `relations` array tying it to an entity — notably notes are attached to
        a COMPANY or CONTACT (or DEAL), so a deal's notes are the notes on the
        deal itself, its company, and its associated contacts. The request body
        filter is ignored (it returns all notes), so callers index by relation
        and match ids themselves.

        Returns (in createdAt-desc order):
            [{"text": str, "relations": [(ENTITY_TYPE, "id"), ...],
              "owner_id": int|None}, ...]
        """
        out = []
        for page in range(max_pages):
            self._pace()
            try:
                r = self.session.post(
                    f"{KYLAS_BASE}/notes/search",
                    params={"page": page, "size": page_size, "sort": "createdAt,desc"},
                    json={}, timeout=60,
                )
                r.raise_for_status()
                resp = r.json()
            except Exception:
                break
            content = resp.get("content", []) if isinstance(resp, dict) else []
            if not content:
                break
            for it in content:
                if not isinstance(it, dict):
                    continue
                rels = []
                for rel in (it.get("relations") or []):
                    et  = str(rel.get("entityType") or "").upper()
                    eid = rel.get("entityId")
                    if et and eid is not None:
                        rels.append((et, str(eid)))
                out.append({
                    "text":      _strip_html(it.get("description") or ""),
                    "relations": rels,
                    "owner_id":  it.get("ownerId"),
                })
            if page >= resp.get("totalPages", 1) - 1:
                break
        return out

    def get_user_email(self, user_id) -> str:
        """
        Fetch a single user's email via GET /users/{id}.

        This is the authoritative per-user lookup (the /users/{id} response
        carries a top-level "email" field) and covers EVERY user, unlike the
        team-members list which only returns the first page. Returns "" on
        failure so callers can fall back to other sources.
        """
        if not user_id:
            return ""
        try:
            resp = self._get(f"users/{user_id}")
            data = resp.get("data", resp) if isinstance(resp, dict) else {}
            email = data.get("email") or data.get("updatedEmail") or ""
            return str(email).strip().lower() if email else ""
        except Exception:
            return ""

    def get_users(self) -> Dict[int, str]:
        """Return {user_id: name} for contact owner resolution."""
        for path in ["tenant/team-members", "users"]:
            try:
                resp    = self._get(path)
                members = resp.get("content") or resp.get("data") or []
                if isinstance(members, list) and members:
                    result = {}
                    for m in members:
                        if "id" not in m:
                            continue
                        name = (m.get("name") or
                                f"{m.get('firstName', '')} {m.get('lastName', '')}".strip() or
                                f"User {m['id']}")
                        result[int(m["id"])] = name
                    if result:
                        return result
            except Exception:
                continue
        return {}

    def get_user_emails(self) -> Dict[str, str]:
        """Return {full_name: email} for all Kylas team members."""
        for path in ["tenant/team-members", "users"]:
            try:
                resp    = self._get(path)
                members = resp.get("content") or resp.get("data") or []
                if not isinstance(members, list) or not members:
                    continue
                result = {}
                for m in members:
                    name = (m.get("name") or
                            f"{m.get('firstName', '')} {m.get('lastName', '')}".strip())
                    # Kylas may expose email as a string or nested list
                    email = m.get("email") or m.get("emailId") or ""
                    if not email:
                        emails_field = m.get("emails") or []
                        if isinstance(emails_field, list) and emails_field:
                            first = emails_field[0]
                            email = (first.get("value") or first.get("email")
                                     or first if isinstance(first, str) else "")
                    if name and email:
                        result[name] = str(email).strip().lower()
                if result:
                    return result
            except Exception:
                continue
        return {}

    def get_users_by_email(self) -> Dict[str, int]:
        """Return {email: user_id} for ALL Kylas team members (all pages)."""
        result: Dict[str, int] = {}

        def _extract(members):
            for m in members:
                uid = m.get("id")
                if not uid:
                    continue
                email = m.get("email") or m.get("emailId") or ""
                if not email:
                    emails_field = m.get("emails") or []
                    if isinstance(emails_field, list) and emails_field:
                        first = emails_field[0]
                        email = (first.get("value") or first.get("email")
                                 or first if isinstance(first, str) else "")
                if email:
                    result[str(email).strip().lower()] = int(uid)

        for path in ["tenant/team-members", "users"]:
            try:
                page = 0
                while True:
                    self._pace()
                    r = self.session.get(
                        f"{KYLAS_BASE}/{path}",
                        params={"page": page, "size": 100},
                        timeout=30,
                    )
                    r.raise_for_status()
                    resp = r.json()
                    members = resp.get("content") or resp.get("data") or []
                    if not isinstance(members, list):
                        break
                    _extract(members)
                    total_pages = resp.get("totalPages", 1)
                    if page >= total_pages - 1 or not members:
                        break
                    page += 1
                if result:
                    return result
            except Exception:
                continue
        return result

    def find_user_id_by_email(self, email: str) -> Optional[int]:
        """
        Direct lookup of a single Kylas user by email.
        Tries POST /search/user first, then falls back to full user list.
        """
        email = email.strip().lower()
        # Try filtered search
        try:
            self._pace()
            r = self.session.post(
                f"{KYLAS_BASE}/search/user",
                params={"page": 0, "size": 10},
                json={"fields": ["id", "email"], "jsonRule": {
                    "condition": "AND",
                    "rules": [{"field": "email", "operator": "equal", "value": email}],
                }},
                timeout=30,
            )
            if r.ok:
                content = r.json().get("content", [])
                for m in content:
                    uid = m.get("id")
                    if uid:
                        return int(uid)
        except Exception:
            pass
        # Fall back to full list
        all_map = self.get_users_by_email()
        return all_map.get(email)

    def _put(self, path: str, body: dict) -> dict:
        r = self._request("PUT", path, json=body)
        self._raise_for_status(r)
        return r.json() if r.content else {}

    def _patch(self, path: str, body: dict) -> dict:
        r = self._request("PATCH", path, json=body)
        self._raise_for_status(r)
        return r.json() if r.content else {}

    def update_company_owner(self, company_id: int, user_id: int) -> bool:
        """
        Reassign a company to a different Kylas user. Returns True on success.

        Uses the dedicated reassignment endpoint (mirrors PUT /leads/{id}/owner):
            PUT /companies/{id}/owner  {"ownerId": <id>}
        Falls back to a full-object PUT with ownedBy if that is unavailable.
        """
        try:
            r = self._request(
                "PUT", f"companies/{company_id}/owner",
                json={"ownerId": user_id},
            )
            if r.ok:
                if not getattr(self, "_company_owner_logged", False):
                    print(f"  [INFO] company owner via PUT /companies/{{id}}/owner (ownerId)")
                    self._company_owner_logged = True
                return True
            if not getattr(self, "_company_owner_logged", False):
                print(f"  [INFO] /companies/{company_id}/owner -> HTTP {r.status_code}; "
                      f"falling back to full PUT")
                self._company_owner_logged = True
        except Exception:
            pass

        # Fallback: GET full object, set ownedBy, PUT back.
        try:
            body = self._get(f"companies/{company_id}")
            body = body.get("data", body)
            body["ownedBy"] = {"id": user_id}
            self._put(f"companies/{company_id}", body)
            return True
        except Exception as exc:
            print(f"[Kylas] ERROR updating company {company_id} owner: {exc}")
            return False

    def list_entity_fields(self, entity: str) -> List[dict]:
        """
        Return raw field-definition dicts for `entity` (e.g. "company") via
        GET /entities/{entity}/fields, including custom fields. Each dict
        carries "name" (the customFieldValues key, e.g. "cfOffsiteTimeline")
        and "displayName" (the human label shown in the Kylas UI).
        """
        out, page = [], 0
        while True:
            try:
                resp = self._get(f"entities/{entity}/fields", {
                    "entityType": entity, "custom-only": "false",
                    "sort": "createdAt,asc", "page": page, "size": 200,
                })
            except Exception:
                break
            if isinstance(resp, list):
                out.extend(resp)
                break
            content = resp.get("content") or resp.get("data") or []
            if not isinstance(content, list):
                break
            out.extend(content)
            total_pages = resp.get("totalPages", 1)
            if page >= total_pages - 1 or not content:
                break
            page += 1
        return out

    def update_company_custom_field(self, company_id: int, field_key: str, value) -> bool:
        """
        Set a single custom-field value on a company via full GET + PUT.

        Mirrors the update_company_owner fallback: fetch the full record and
        PUT it back unchanged except for the one customFieldValues key, so
        every other field — including ownedBy/owner — is left exactly as
        Kylas returned it.
        """
        try:
            body = self._get(f"companies/{company_id}")
            body = body.get("data", body)
            cf = dict(body.get("customFieldValues") or {})
            cf[field_key] = value
            body["customFieldValues"] = cf
            self._put(f"companies/{company_id}", body)
            return True
        except Exception as exc:
            print(f"[Kylas] ERROR updating company {company_id} custom field {field_key!r}: {exc}")
            return False

    def update_contact_owner(self, contact_id: int, user_id: int,
                             contact_data: dict = None) -> bool:
        """
        Reassign a contact to a different Kylas user. Returns True on success.

        Kylas ignores `ownedBy` in the plain PUT /contacts/{id} body (the
        Update Contact schema has no owner field). Owner changes go through the
        dedicated reassignment endpoint, mirroring PUT /leads/{id}/owner:
            PUT /contacts/{id}/owner  {"ownerId": <id>}
        Falls back to a full-object PUT only if that endpoint is unavailable.
        """
        # Preferred: dedicated owner-reassignment endpoint (fast, tiny body).
        try:
            r = self._request(
                "PUT", f"contacts/{contact_id}/owner",
                json={"ownerId": user_id},
            )
            if r.ok:
                if not getattr(self, "_contact_owner_logged", False):
                    print(f"  [INFO] contact owner via PUT /contacts/{{id}}/owner (ownerId)")
                    self._contact_owner_logged = True
                return True
            if not getattr(self, "_contact_owner_logged", False):
                print(f"  [INFO] /contacts/{contact_id}/owner -> HTTP {r.status_code}; "
                      f"falling back to full PUT")
                self._contact_owner_logged = True
        except Exception:
            pass

        # Fallback: full-object PUT with ownedBy (works for some Kylas tenants).
        try:
            if contact_data and len(contact_data) > 4:
                body = dict(contact_data)
            else:
                resp = self._get(f"contacts/{contact_id}")
                body = resp.get("data", resp)
            body["ownedBy"] = {"id": user_id}
            co = body.get("company")          # contact PUT wants company as an int id
            if isinstance(co, dict):
                body["company"] = co.get("id")
            for ro in ("id", "createdAt", "updatedAt", "updatedBy",
                       "createdBy", "ownerId", "recordActions"):
                body.pop(ro, None)
            self._put(f"contacts/{contact_id}", body)
            return True
        except Exception as exc:
            print(f"[Kylas] ERROR updating contact {contact_id} owner: {exc}")
            return False

    # ------------------------------------------------------------------
    # Generic field push (Airtable → Kylas) + read-only field inspection
    # ------------------------------------------------------------------

    @staticmethod
    def _is_custom_key(key: str) -> bool:
        """Kylas custom fields are conventionally named cfSomething."""
        return bool(key) and key[:2] == "cf" and len(key) > 2 and key[2].isupper()

    def _apply_fields(self, body: dict, fields: dict) -> bool:
        """Set mapped fields on a fetched entity body in place.

        Keys named cfXxx go under customFieldValues, everything else is a
        top-level standard field. Returns True if anything actually changed
        (so callers can skip a no-op PUT).

        A standard field whose current value is a structured object
        (e.g. numberOfEmployees / industry are {id, name} in Kylas) cannot be
        set from a bare scalar — doing so makes Kylas reject the whole PUT
        (400). Such mappings are skipped with a one-time warning so the rest of
        the fields still go through.
        """
        changed = False
        cfv = dict(body.get("customFieldValues") or {})
        warned = getattr(self, "_field_shape_warned", None)
        if warned is None:
            warned = self._field_shape_warned = set()
        for key, value in fields.items():
            if KylasClient._is_custom_key(key):
                if cfv.get(key) != value:
                    cfv[key] = value
                    changed = True
            else:
                current = body.get(key)
                if isinstance(current, (dict, list)) and not isinstance(value, (dict, list)):
                    if key not in warned:
                        print(f"  [Kylas] WARN: standard field '{key}' is an object in "
                              f"Kylas; can't set it from scalar {value!r} — skipping "
                              f"(this mapping would 400 the whole record)")
                        warned.add(key)
                    continue
                if current != value:
                    body[key] = value
                    changed = True
        if cfv:
            body["customFieldValues"] = cfv
        return changed

    # Fields Kylas populates on GET but rejects (or ignores) on a PUT update.
    # Echoing a fetched object straight back including these is what produced
    # the "400 Bad Request" on company updates.
    _READONLY_PUT_FIELDS = (
        "id", "createdAt", "updatedAt", "createdBy", "updatedBy",
        "ownerId", "ownedBy", "recordActions", "tenantId",
    )

    @staticmethod
    def _clean_for_put(body: dict) -> dict:
        """Return a copy of an entity body safe to PUT back to Kylas.

        Drops the read-only/audit fields Kylas sets on GET but rejects on
        update. Custom field values and all writable fields are preserved.
        """
        return {k: v for k, v in body.items()
                if k not in KylasClient._READONLY_PUT_FIELDS}

    def _without_owner_keys(self, fields: dict, entity: str) -> dict:
        """Strip ownedBy/ownerId from a field-push map (owner isn't writable here).

        Owner reassignment goes through the dedicated PUT /{entity}/{id}/owner
        endpoint (use --mode owner / both), not the generic field PUT, where
        Kylas ignores or rejects an owner value. Warns once so a stray mapping
        gets noticed and fixed rather than silently failing every record.
        """
        owner_keys = [k for k in fields if k in ("ownedBy", "ownerId")]
        if not owner_keys:
            return fields
        if not getattr(self, "_owner_field_warned", False):
            print(f"  [Kylas] WARN: {owner_keys} in the {entity} field map is not "
                  f"writable via field push — assign owners with --mode owner/both")
            self._owner_field_warned = True
        return {k: v for k, v in fields.items() if k not in owner_keys}

    # Kylas custom-field "type" names (from /entities/{entity}/fields).
    _PICKLIST_TYPES = {"PICK_LIST", "PICKLIST", "DROPDOWN", "DROP_DOWN", "SELECT", "RADIO"}
    _BOOLEAN_TYPES  = {"TOGGLE", "CHECKBOX", "BOOLEAN", "BOOL", "SWITCH"}

    def _load_picklist_config(self) -> dict:
        """Load config/kylas_picklists.json: {entity: {cf_key: {label: id}}}.

        A manual fallback for dropdown options the API can't surface — notably
        company custom fields, whose /entities/company/fields endpoint returns
        no picklist values on this tenant. Optional; returns {} if absent.
        """
        cfg = getattr(self, "_pick_cfg", None)
        if cfg is not None:
            return cfg
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "config", "kylas_picklists.json")
        try:
            with open(path) as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
        self._pick_cfg = cfg if isinstance(cfg, dict) else {}
        return self._pick_cfg

    def _field_defs_from_layout(self, entity: str) -> dict:
        """Return {cf_key: defn} by walking layout endpoints for ENTITY_UPPER.

        Tries layout types in order (CREATE, EDIT, DETAIL, VIEW), each wrapped
        in its own try/except so a 400/404 on one type doesn't block the rest.
        Results are merged across all successful responses: a key is added if
        not yet present; if the stored entry has empty options and a later type
        returns non-empty options, the options/labels are upgraded.

        The layout endpoint is the authoritative source for field definitions on
        tenants where /entities/{entity}/fields returns no picklist data. The
        response nests fields arbitrarily deep under repeated layoutItems/item
        structures; we recurse generically so any depth works.

        Returns {} if all layout types fail (best-effort).
        """
        entity_upper = entity.upper()
        defs: dict = {}

        def _walk(node):
            if isinstance(node, list):
                for item in node:
                    _walk(item)
            elif isinstance(node, dict):
                # A field object has both an internalName (or fieldName) and a type.
                iname = node.get("internalName") or node.get("fieldName") or ""
                ftype = node.get("type") or ""
                if iname and ftype:
                    display = (node.get("displayName") or node.get("label")
                               or node.get("name") or iname)
                    options, labels = {}, {}
                    for opt in (node.get("pickLists") or node.get("picklists")
                                or node.get("options") or node.get("pickList") or []):
                        if not isinstance(opt, dict):
                            continue
                        oid = opt.get("id")
                        lbl = (opt.get("value") or opt.get("displayName")
                               or opt.get("name") or opt.get("label") or "")
                        if oid is not None and str(lbl).strip():
                            options[str(lbl).strip().lower()] = oid
                            labels[oid] = str(lbl).strip()
                    multi = bool(node.get("multiValue"))
                    new_def = {
                        "type":        str(ftype).upper(),
                        "displayName": str(display),
                        "multiValue":  multi,
                        "options":     options,
                        "labels":      labels,
                    }
                    key = str(iname)
                    existing = defs.get(key)
                    if existing is None:
                        defs[key] = new_def
                    elif not existing.get("options") and new_def.get("options"):
                        # Upgrade: keep existing but fill in options/labels.
                        existing["options"] = new_def["options"]
                        existing["labels"]  = new_def["labels"]
                # Always recurse into child values regardless of whether this node
                # was itself a field object (sections contain fields, etc.).
                for v in node.values():
                    if isinstance(v, (dict, list)):
                        _walk(v)

        for layout_type in ("CREATE", "EDIT", "DETAIL", "VIEW"):
            try:
                resp = self._get(f"ui/layouts/{layout_type}/{entity_upper}")
                _walk(resp)
            except Exception:
                continue

        return defs

    def get_custom_field_defs(self, entity: str) -> dict:
        """Return {cf_key: {"type", "displayName", "options"(label_lower->id), "labels"(id->label), "multiValue"}}.

        Cached per entity. Powers outgoing value formatting: Kylas wants a
        dropdown field's option *id* (not its label, e.g. cfOffsiteTimeline
        "Jan - Mar" -> 257199) and a real boolean for toggle fields — pushing
        the raw Airtable string otherwise trips an opaque 500.

        Source priority (lowest to highest):
          1. /entities/{entity}/fields (two passes: custom-only=false then true)
          2. ui/layouts/CREATE/{ENTITY_UPPER} — fills gaps in display names and
             picklist options for tenants where the /entities endpoint is sparse
          3. config/kylas_picklists.json (manual fallback; config wins for opts it specifies)
        Best-effort: returns partial results rather than raising.
        """
        cache = getattr(self, "_cf_defs_cache", None)
        if cache is None:
            cache = self._cf_defs_cache = {}
        if entity in cache:
            return cache[entity]
        defs = {}
        for custom_only in ("false", "true"):   # 'false' tends to return the richer payload
            try:
                resp = self._get(f"entities/{entity}/fields", {
                    "entityType": entity, "custom-only": custom_only,
                    "sort": "createdAt,asc", "page": 0, "size": 200,
                })
            except Exception:
                continue
            items = resp if isinstance(resp, list) else (
                resp.get("content") or resp.get("data") or [])
            for fld in items:
                if not isinstance(fld, dict):
                    continue
                key = fld.get("fieldName") or fld.get("apiName") or fld.get("name") or ""
                if not str(key).startswith("cf"):
                    continue
                ftype = str(fld.get("type") or fld.get("fieldType") or "").upper()
                display = (fld.get("displayName") or fld.get("label")
                           or fld.get("name") or str(key))
                options, labels = {}, {}
                for opt in (fld.get("pickLists") or fld.get("picklists")
                            or fld.get("options") or fld.get("pickList") or []):
                    if not isinstance(opt, dict):
                        continue
                    oid   = opt.get("id")
                    label = (opt.get("value") or opt.get("displayName")
                             or opt.get("name") or opt.get("label") or "")
                    if oid is not None and str(label).strip():
                        options[str(label).strip().lower()] = oid
                        labels[oid] = str(label).strip()
                multi = bool(fld.get("multiValue"))
                if str(key) not in defs or (options and not defs[str(key)].get("options")):
                    defs[str(key)] = {"type": ftype, "displayName": str(display),
                                      "options": options, "labels": labels,
                                      "multiValue": multi}
            if any(d.get("options") for d in defs.values()):
                break   # got picklist data — second pass unnecessary

        # Supplement from layout endpoint — authoritative source for tenants
        # where /entities returns no picklist data or missing display names.
        layout_defs = self._field_defs_from_layout(entity)
        # DEBUG: show what the layout actually returns for key fields BEFORE picklist override
        for _dbg_key in ("cfAccountStatus", "cfLastCalledAtDate"):
            if _dbg_key in layout_defs:
                _d = layout_defs[_dbg_key]
                print(f"[debug] layout {entity}/{_dbg_key}: type={_d.get('type')!r} "
                      f"display={_d.get('displayName')!r} options={_d.get('options')}")
            else:
                print(f"[debug] layout {entity}/{_dbg_key}: NOT FOUND in layout endpoints")
        for key, ldef in layout_defs.items():
            existing = defs.get(key)
            if existing is None:
                # Layout-only key: adopt wholesale.
                defs[key] = ldef
            else:
                # Fill missing/empty options and display name from layout.
                if not existing.get("options") and ldef.get("options"):
                    existing["options"] = ldef["options"]
                    existing["labels"]  = ldef["labels"]
                if not existing.get("displayName") or existing["displayName"] == key:
                    existing["displayName"] = ldef.get("displayName") or existing["displayName"]
                if not existing.get("multiValue") and ldef.get("multiValue"):
                    existing["multiValue"] = ldef["multiValue"]

        # Supplement with the config picklist map (fills in what the API omits;
        # config wins for options it specifies).
        for key, opt_map in (self._load_picklist_config().get(entity) or {}).items():
            d = defs.get(str(key)) or {"type": "PICK_LIST", "displayName": str(key),
                                       "options": {}, "labels": {}, "multiValue": False}
            opts, labs = dict(d.get("options") or {}), dict(d.get("labels") or {})
            for label, oid in opt_map.items():
                opts[str(label).strip().lower()] = oid
                labs[oid] = str(label).strip()
            d.update({"type": d.get("type") or "PICK_LIST", "options": opts, "labels": labs})
            defs[str(key)] = d

        cache[entity] = defs
        return defs

    def cf_key_for_display(self, entity: str, display_name: str):
        """Return the cf_key whose display name matches display_name (case-insensitive).

        Checks list_custom_field_keys (entity fields endpoint) first, then
        scans get_custom_field_defs (which includes layout-sourced displayNames)
        so fields that only appear in the layout endpoint are also matched.
        Returns None if no match is found.
        """
        target = display_name.strip().lower()
        for key, name in self.list_custom_field_keys(entity).items():
            if str(name).strip().lower() == target:
                return key
        # Secondary scan: layout-sourced displayNames in get_custom_field_defs.
        for key, defn in self.get_custom_field_defs(entity).items():
            dn = defn.get("displayName") or ""
            if str(dn).strip().lower() == target:
                return key
        return None

    @staticmethod
    def _as_id_set(raw) -> set:
        """Normalise a customFieldValues multi-select raw value to a set of int ids.

        Accepts: None, a bare int, {"id": int}, or a list of any of those.
        Silently skips entries that can't be coerced to int.
        """
        if raw is None:
            return set()
        if not isinstance(raw, list):
            raw = [raw]
        out = set()
        for item in raw:
            try:
                if isinstance(item, dict):
                    out.add(int(item["id"]))
                else:
                    out.add(int(item))
            except (KeyError, TypeError, ValueError):
                pass
        return out

    def merge_company_multiselect(self, company_id: int, cf_key: str,
                                  add_labels: list, dry_run: bool = False) -> str:
        """Union add_labels into a company's multi-select custom field.

        Steps:
          1. GET the company and read its current multi-select value.
          2. Map each label in add_labels to an option id (via get_custom_field_defs).
          3. Compute union; if no change return "unchanged".
          4. PUT [{id,name}, ...] matching GET shape; fallback to bare-id then {id}-only.
          5. dry_run=True prints the intended change without writing.
        Returns "updated" | "unchanged" | "failed".
        """
        defn    = self.get_custom_field_defs("company").get(cf_key, {})
        options = defn.get("options") or {}   # label_lower -> id

        # GET the company.
        try:
            resp = self._get(f"companies/{company_id}")
            body = resp.get("data", resp)
        except Exception as exc:
            print(f"[Kylas] ERROR fetching company {company_id}: {exc}")
            return "failed"

        cfv         = body.get("customFieldValues") or {}
        current_ids = self._as_id_set(cfv.get(cf_key))

        # Map labels to ids; warn once per key for unmapped ones.
        add_ids = set()
        warned_labels = getattr(self, "_multiselect_label_warned", None)
        if warned_labels is None:
            warned_labels = self._multiselect_label_warned = set()
        for lbl in add_labels:
            norm = str(lbl).strip().lower()
            oid  = options.get(norm)
            if oid is not None:
                add_ids.add(int(oid))
            else:
                wkey = (cf_key, norm)
                if wkey not in warned_labels:
                    warned_labels.add(wkey)
                    known = ", ".join(sorted(str(v) for v in options.keys())[:8])
                    print(f"  [Kylas] WARN: {cf_key} label {lbl!r} matches no option"
                          + (f" (options: {known})" if known else "") + " — skipping")

        union = current_ids | add_ids
        if union == current_ids:
            return "unchanged"

        sorted_ids = sorted(union)
        if dry_run:
            added = sorted(add_ids - current_ids)
            print(f"  [DRY RUN] company {company_id} {cf_key}: "
                  f"current={sorted(current_ids)} add={added} result={sorted_ids}")
            return "updated"

        # Build PUT body from the GET response, stripping read-only/audit fields
        # but keeping ownerId so Kylas doesn't reset the owner to the API user.
        base = self._clean_for_put(body)
        if body.get("ownerId"):
            base["ownerId"] = body["ownerId"]
        base_cfv = dict(base.get("customFieldValues") or {})

        # Build {id, name} objects — matches the shape GET returns (and PUT requires).
        labels_rev   = defn.get("labels") or {}   # {option_id: label}
        id_name_list = [{"id": i, "name": labels_rev.get(i, "")} for i in sorted_ids]

        # Try {id,name} first (GET/PUT native shape), then bare-id, then {id}-only.
        for attempt, value in enumerate([
            id_name_list,
            sorted_ids,
            [{"id": i} for i in sorted_ids],
        ]):
            base_cfv[cf_key] = value
            base["customFieldValues"] = base_cfv
            try:
                self._put(f"companies/{company_id}", base)
                return "updated"
            except Exception as exc:
                if attempt < 2:
                    print(f"  [Kylas] multi-select attempt {attempt + 1} failed for {cf_key} "
                          f"— retrying ({self._short_err(exc)})")
                else:
                    print(f"[Kylas] ERROR {cf_key} on company {company_id}: "
                          f"{self._short_err(exc)}")
        return "failed"

    def contact_label_map(self) -> dict:
        """Return {option_id: label} for the contact's Offsite Timeline field.

        Resolves the contact field key by display name "Offsite Timeline" at
        runtime; falls back to the conventional key cfOffsiteTimeline. Returns
        {} if the field definitions can't be fetched.
        """
        key = self.cf_key_for_display("contact", "Offsite Timeline") or "cfOffsiteTimeline"
        defs = self.get_custom_field_defs("contact")
        return dict(defs.get(key, {}).get("labels") or {})

    def _warn_unmatched(self, key: str, value, defn: dict) -> None:
        warned = getattr(self, "_pick_warned", None)
        if warned is None:
            warned = self._pick_warned = set()
        if key in warned:
            return
        warned.add(key)
        opts = ", ".join(sorted((defn.get("labels") or {}).values())[:8])
        print(f"  [Kylas] WARN: {key} value {value!r} matches no dropdown option"
              + (f" (options: {opts})" if opts else "") + " — leaving as-is")

    @staticmethod
    def _encode_pick(oid, label, style):
        """Encode a resolved dropdown option id in the style Kylas accepts."""
        if style == "idobj":
            return {"id": oid}
        if style == "idname" and label:
            return {"id": oid, "name": label}
        return oid

    def _format_cf_value(self, key: str, defn: dict, value):
        """Coerce one Airtable value to the shape Kylas wants for this cf type.

        Dropdown -> the option id, encoded in the style already proven to work
        for this field this run (see _pick_style) so only the first record pays
        the isolation cost. Boolean -> a real bool. Anything unmatched / other
        types pass through, so isolation can still surface a genuine problem.
        """
        if not defn or value is None:
            return value
        if isinstance(value, list):          # a single-select may arrive as a 1-item list
            value = value[0] if value else ""
        ftype   = defn.get("type", "")
        options = defn.get("options") or {}
        if ftype in self._PICKLIST_TYPES or options:
            if isinstance(value, bool):
                return value
            oid = None
            if isinstance(value, (int, float)):
                oid = int(value)
            else:
                s = str(value).strip()
                if s.lower() in options:
                    oid = options[s.lower()]
                elif s.isdigit():
                    oid = int(s)
            if oid is None:
                self._warn_unmatched(key, value, defn)
                return value
            label = (defn.get("labels") or {}).get(oid)
            return self._encode_pick(oid, label, self._pick_style.get(key, "id"))
        if ftype in self._BOOLEAN_TYPES:
            if isinstance(value, bool):
                return value
            s = str(value).strip().lower()
            if s in ("true", "yes", "y", "1", "checked", "on"):
                return True
            if s in ("false", "no", "n", "0", "unchecked", "off", ""):
                return False
        return value

    def _format_fields(self, fields: dict, defs: dict) -> dict:
        """Coerce custom-field values to Kylas's expected shapes (dropdown id, bool)."""
        if not defs:
            return fields
        out = dict(fields)
        for k in list(out):
            if self._is_custom_key(k) and k in defs:
                out[k] = self._format_cf_value(k, defs[k], out[k])
        return out

    def _cf_candidates(self, key: str, value, defs: dict) -> list:
        """Value encodings to try for one field, best first.

        PICK_LIST: tries id / {"id"} / {"id","name"} (integer-ID styles), then
        falls back to raw string labels and list-wrapped forms in case Kylas
        needs the label directly or a single-element list (like multi-select).
        DATE-looking strings: tries bare date then full ISO-8601 variants.
        Non-dropdown/non-date values yield just themselves.
        """
        import re as _re
        defn = (defs or {}).get(key)
        ftype = (defn or {}).get("type", "")
        if isinstance(value, bool):
            return [value]

        # ── PICK_LIST ─────────────────────────────────────────────────────────
        is_pick = ftype in self._PICKLIST_TYPES or (defn or {}).get("options")
        oid = value.get("id") if isinstance(value, dict) else (
            value if isinstance(value, int) else None)
        if is_pick and oid is not None:
            label = ((defn or {}).get("labels") or {}).get(oid)
            by_style = {"id": oid, "idobj": {"id": oid}}
            if label:
                by_style["idname"] = {"id": oid, "name": label}
            pref  = self._pick_style.get(key, "id")
            order = [pref] + [s for s in ("id", "idobj", "idname") if s != pref]
            candidates = [by_style[s] for s in order if s in by_style]
            # Fallback: raw string labels (bypasses option-ID lookup entirely)
            if label:
                candidates.append(label)
                candidates.append(label.lower())
            # Fallback: list-wrapped (single-select may need [{id,name}] shape)
            candidates.append([oid])
            if label:
                candidates.append([{"id": oid, "name": label}])
            candidates.append([{"id": oid}])
            return candidates

        # ── DATE string ───────────────────────────────────────────────────────
        if isinstance(value, str):
            s = value.strip()
            if _re.match(r'^\d{4}-\d{2}-\d{2}$', s):
                return [
                    s,
                    s + "T00:00:00.000Z",
                    s + "T00:00:00",
                    s + "T00:00:00.000+05:30",
                ]

        return [value]

    @staticmethod
    def _short_err(exc) -> str:
        """Compact one-line form of an error for per-field diagnostics."""
        s = str(exc)
        return s.split(" — ", 1)[1] if " — " in s else s[:140]

    def _put_fields(self, path: str, entity_id: int, base: dict,
                    fields: dict, dry_run: bool, defs: dict = None) -> str:
        """Apply mapped `fields` onto a clean `base` body and PUT to {path}/{id}.

        The body never carries ownedBy (owner is set separately via the
        dedicated /{entity}/{id}/owner endpoint, after fields — a full field PUT
        with ownedBy is rejected, and one without it would reset the owner).

        Fast path: apply everything and PUT once. If Kylas rejects that — it
        often answers a single bad custom-field value (e.g. a raw string sent
        to a dropdown/boolean field) with an opaque 500 naming no field — fall
        back to isolation:
          1. PUT the unchanged base; if even that fails, the record itself
             can't round-trip and it is not a mapped-field problem.
          2. Apply the fields one at a time, keeping the ones Kylas accepts and
             reporting which it rejects. Dropdown values are retried across a
             few encodings (id / {id} / {id,name}) so a wrong-shape guess self-
             corrects.
        Returns "updated" | "unchanged" | "failed".
        """
        body = dict(base)
        if not self._apply_fields(body, fields):
            return "unchanged"
        if dry_run:
            return "updated"
        try:
            self._put(f"{path}/{entity_id}", body)
            return "updated"
        except Exception as _fast_exc:
            print(f"[debug] _put_fields: fast-path PUT failed for {path}/{entity_id} — {_fast_exc!s}")
            pass  # isolate below to find the offending field(s)

        # Does the record round-trip unchanged at all?
        try:
            self._put(f"{path}/{entity_id}", dict(base))
        except Exception as base_err:
            print(f"[Kylas] ERROR {path}/{entity_id}: unchanged PUT also fails — Kylas "
                  f"can't round-trip this record, not a mapped-field issue "
                  f"({self._short_err(base_err)})")
            return "failed"

        # Base is fine — find which mapped field(s) Kylas rejects, keeping the good ones.
        good, applied, rejected = dict(base), [], {}
        for key, value in fields.items():
            candidates = self._cf_candidates(key, value, defs)
            probe = dict(good)
            if not self._apply_fields(probe, {key: candidates[0]}):
                continue  # no-op or skipped (e.g. object-from-scalar guard)
            err = None
            for cand in candidates:
                trial = dict(good)
                self._apply_fields(trial, {key: cand})
                try:
                    self._put(f"{path}/{entity_id}", trial)
                    good, err = trial, None
                    applied.append(key)
                    if len(candidates) > 1:   # dropdown: remember the encoding that worked
                        self._pick_style[key] = ("idname" if isinstance(cand, dict) and "name" in cand
                                                 else "idobj" if isinstance(cand, dict) else "id")
                    break
                except Exception as exc:
                    print(f"[debug] _put_fields: key={key!r} cand={cand!r} full_error={exc!s}")
                    err = self._short_err(exc)
            if err is not None:
                rejected[key] = err
        if rejected:
            detail = ", ".join(f"{k} [{v}]" for k, v in rejected.items())
            print(f"[Kylas] {path}/{entity_id}: Kylas rejected {detail}"
                  + (f"; applied {applied}" if applied else "; applied none"))
        return "updated" if applied else "failed"

    def update_company_fields(self, company_id: int, fields: dict,
                              dry_run: bool = False) -> str:
        """Push mapped fields onto a company (GET full object, set, PUT back).

        Returns "updated", "unchanged", or "failed". On a Kylas rejection the
        failing field(s) are isolated and named (see _put_fields). Owner is NOT
        touched here — it is assigned separately via update_company_owner.
        """
        fields = self._without_owner_keys(fields, "company")
        if not fields:
            return "unchanged"
        defs   = self.get_custom_field_defs("company")
        fields = self._format_fields(fields, defs)
        try:
            body = self._get(f"companies/{company_id}")
            body = body.get("data", body)
        except Exception as exc:
            print(f"[Kylas] ERROR fetching company {company_id}: {exc}")
            return "failed"
        return self._put_fields("companies", company_id,
                                self._clean_for_put(body), fields, dry_run, defs)

    def update_contact_fields(self, contact_id: int, fields: dict,
                              contact_data: dict = None, dry_run: bool = False) -> str:
        """Push mapped fields onto a contact. Returns updated/unchanged/failed.

        On a Kylas rejection the failing field(s) are isolated and named
        (see _put_fields). Owner is NOT touched here — it is assigned separately
        via update_contact_owner.
        """
        fields = self._without_owner_keys(fields, "contact")
        if not fields:
            return "unchanged"
        defs   = self.get_custom_field_defs("contact")
        fields = self._format_fields(fields, defs)
        try:
            if contact_data and len(contact_data) > 4:
                body = dict(contact_data)
            else:
                resp = self._get(f"contacts/{contact_id}")
                body = resp.get("data", resp)
        except Exception as exc:
            print(f"[Kylas] ERROR fetching contact {contact_id}: {exc}")
            return "failed"
        co = body.get("company")              # contact PUT wants company as an int id
        if isinstance(co, dict):
            body["company"] = co.get("id")
        return self._put_fields("contacts", contact_id,
                                self._clean_for_put(body), fields, dry_run, defs)

    def fetch_sample(self, entity: str) -> dict:
        """Return the full detail record of one recent entity ('company'/'contact')."""
        try:
            r = self.session.post(
                f"{KYLAS_BASE}/search/{entity}",
                params={"page": 0, "size": 1, "sort": "updatedAt,desc"},
                json={"fields": ["id"], "jsonRule": None}, timeout=30,
            )
            r.raise_for_status()
            content = r.json().get("content", [])
            if not content:
                return {}
            rid  = content[0].get("id")
            path = "companies" if entity == "company" else "contacts"
            resp = self._get(f"{path}/{rid}")
            return resp.get("data", resp)
        except Exception as exc:
            print(f"[Kylas] ERROR fetching sample {entity}: {exc}")
            return {}

    def list_custom_field_keys(self, entity: str) -> dict:
        """Return {cf_key: display_name} for ALL defined custom fields on entity.

        Uses GET /entities/{entity}/fields so even null-valued fields appear.
        Falls back to empty dict on error so inspect still works.
        """
        out = {}
        # 1. Try the entity fields definition endpoint (works for contact).
        try:
            resp = self._get(f"entities/{entity}/fields", {
                "entityType": entity, "custom-only": "true",
                "sort": "createdAt,asc", "page": 0, "size": 200,
            })
            items = resp if isinstance(resp, list) else (resp.get("content") or resp.get("data") or [])
            for fld in items:
                if not isinstance(fld, dict):
                    continue
                key  = fld.get("fieldName") or fld.get("apiName") or fld.get("name") or fld.get("id") or ""
                if not str(key).startswith("cf"):
                    continue
                name = fld.get("displayName") or fld.get("label") or fld.get("name") or key
                out[str(key)] = str(name)
        except Exception:
            pass

        if out:
            return out

        # 2. Fallback: scan recent records and aggregate all cf* keys found.
        #    Covers company (no fields endpoint) by sampling 20 recent records.
        try:
            search_entity = "company" if entity == "company" else entity
            r = self.session.post(
                f"{KYLAS_BASE}/search/{search_entity}",
                params={"page": 0, "size": 20, "sort": "updatedAt,desc"},
                json={"fields": ["id", "customFieldValues"], "jsonRule": None},
                timeout=30,
            )
            r.raise_for_status()
            path = "companies" if entity == "company" else f"{entity}s"
            for rec in r.json().get("content", []):
                rid = rec.get("id")
                if not rid:
                    continue
                try:
                    detail = self._get(f"{path}/{rid}")
                    cfv = (detail.get("data", detail) if isinstance(detail, dict) else {}).get("customFieldValues") or {}
                    for k in cfv:
                        if str(k).startswith("cf") and k not in out:
                            out[str(k)] = str(k)
                except Exception:
                    pass
                if len(out) > 30:
                    break
        except Exception:
            pass

        if not out:
            print(f"[Kylas] WARN: could not fetch {entity} custom field definitions")
        return out

    # Candidate jsonRule shapes for filtering contacts by their company.
    # Kylas has no documented company filter for /search/contact, so we try
    # several field/value shapes and keep whichever actually returns the
    # company's contacts. Every result is validated locally (see below), so a
    # filter that is silently ignored can never reassign the wrong contacts.
    _CONTACT_COMPANY_FILTERS = [
        lambda cid: {"id": "company.id", "field": "company.id", "type": "double",  "operator": "equal", "value": cid},
        lambda cid: {"id": "company.id", "field": "company.id", "type": "integer", "operator": "equal", "value": cid},
        lambda cid: {"id": "company",    "field": "company",    "type": "double",  "operator": "in",    "value": [cid]},
        lambda cid: {"id": "company.id", "field": "company.id", "type": "double",  "operator": "in",    "value": [cid]},
        lambda cid: {"id": "company",    "field": "company",    "type": "double",  "operator": "equal", "value": cid},
        lambda cid: {"id": "companyId",  "field": "companyId",  "type": "integer", "operator": "equal", "value": cid},
    ]

    def list_contact_filter_fields(self) -> list:
        """
        Diagnostic: return the searchable field names Kylas exposes for
        contacts (from /entities/contact/fields), used to discover the correct
        company filter when the candidate shapes all fail.
        """
        names = []
        try:
            resp = self._get("entities/contact/fields", {
                "entityType": "contact", "custom-only": "false",
                "sort": "createdAt,asc", "page": 0, "size": 200,
            })
            for fld in (resp.get("content") or resp.get("data") or []):
                nm = fld.get("name") or fld.get("fieldName") or fld.get("id")
                if nm and ("compan" in str(nm).lower()):
                    names.append(str(nm))
        except Exception:
            pass
        return names

    @staticmethod
    def _contact_company_id(ct: dict) -> str:
        """Plain int-string of a contact's linked company id, or ''."""
        co = ct.get("company")
        raw = co.get("id") if isinstance(co, dict) else co
        try:
            return str(int(float(str(raw))))
        except (ValueError, TypeError):
            return ""

    def get_contacts_by_company(self, company_id: int) -> List[dict]:
        """
        Return all contacts linked to company_id, validated locally.

        Tries candidate filter shapes (caching the one that works on this
        client) and keeps only contacts whose own company id matches — so it
        is both cap-free (targeted query, not the global 10k-capped scan) and
        safe against a filter Kylas might ignore.
        Best-effort: returns [] on error instead of raising.
        """
        cid = int(company_id)
        cid_str = str(cid)
        fields = ["id", "name", "company", "ownedBy", "customFieldValues"]
        # No single company has anywhere near this many contacts; a response
        # larger than this means Kylas ignored our filter and returned the
        # whole contact set, so that filter shape is not usable.
        IGNORED_THRESHOLD = 2000

        def _list_endpoint() -> tuple:
            """
            Try GET /contacts?companyId=<id> — a direct list filter that is not
            subject to the 10k /search cap. Returns (validated_records, ok, targeted).
            """
            records, page = [], 0
            while True:
                try:
                    r = self._request(
                        "GET", "contacts",
                        params={"companyId": cid, "page": page, "size": PAGE_SIZE},
                        timeout=60,
                    )
                    r.raise_for_status()
                    resp = r.json() if r.content else {}
                except Exception:
                    return records, False, False
                content = resp.get("content", [])
                total   = resp.get("totalElements")
                if total is None:
                    total = resp.get("totalPages", 1) * PAGE_SIZE
                if total > IGNORED_THRESHOLD:
                    return records, True, False  # filter ignored — reject
                records.extend(c for c in content if self._contact_company_id(c) == cid_str)
                if resp.get("last", True) or page >= resp.get("totalPages", 1) - 1 or not content:
                    break
                page += 1
            return records, True, True

        def _run(make_rule) -> tuple:
            """
            Returns (validated_records, ok, targeted).
              ok       — got HTTP 200 responses (False on error)
              targeted — filter looks real (small result set), not ignored
            """
            records, page = [], 0
            while True:
                try:
                    r = self._request(
                        "POST", "search/contact",
                        params={"page": page, "size": PAGE_SIZE, "sort": "updatedAt,desc"},
                        json={"fields": fields,
                              "jsonRule": {"condition": "AND", "valid": True,
                                           "rules": [make_rule(cid)]}},
                        timeout=60,
                    )
                    r.raise_for_status()
                except Exception:
                    return records, False, False
                resp    = r.json()
                content = resp.get("content", [])
                total   = resp.get("totalElements")
                if total is None:
                    total = resp.get("totalPages", 1) * PAGE_SIZE
                if total > IGNORED_THRESHOLD:
                    # Filter ignored — don't scan the entire (capped) contact set.
                    return records, True, False
                records.extend(c for c in content if self._contact_company_id(c) == cid_str)
                if page >= resp.get("totalPages", 1) - 1 or not content:
                    break
                page += 1
            return records, True, True

        # Fast path: a method already proven to work on this client.
        method = getattr(self, "_contact_method", None)
        if method == "list":
            records, _, _ = _list_endpoint()
            return records
        if callable(method):
            records, _, _ = _run(method)
            return records

        # Discovery — prefer the GET list endpoint (no 10k cap), then fall back
        # to POST /search filter shapes. Lock in whichever first returns a real,
        # targeted result.
        records, ok, targeted = _list_endpoint()
        if ok and targeted and records:
            self._contact_method = "list"
            return records
        for make_rule in self._CONTACT_COMPANY_FILTERS:
            records, ok, targeted = _run(make_rule)
            if ok and targeted and records:
                self._contact_method = make_rule
                return records
        # None worked — company has no contacts, or no method is supported.
        # Don't cache; a later company may reveal the working method.
        return []
