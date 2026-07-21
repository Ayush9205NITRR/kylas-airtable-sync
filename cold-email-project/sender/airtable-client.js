/**
 * Thin Airtable REST API client for the Email Contacts table (and generic
 * helpers reused by rotation.js for the Inboxes table).
 *
 * Required env vars:
 *   AIRTABLE_API_KEY  - Airtable personal access token
 *   AIRTABLE_BASE_ID  - the base ID (starts with "app...")
 *
 * Table names are the contract from /docs/schema.md — exact strings,
 * created by Agent B's /data/airtable-setup.js:
 *   People, Email Contacts, Email Templates, Inboxes, Daily Report
 *
 * Requires Node >=18 (uses global fetch).
 */

const AIRTABLE_API_BASE = 'https://api.airtable.com/v0';

const TABLES = {
  PEOPLE: 'People',
  EMAIL_CONTACTS: 'Email Contacts',
  EMAIL_TEMPLATES: 'Email Templates',
  INBOXES: 'Inboxes',
  DAILY_REPORT: 'Daily Report',
};

function getConfig() {
  const apiKey = process.env.AIRTABLE_API_KEY;
  const baseId = process.env.AIRTABLE_BASE_ID;
  if (!apiKey) throw new Error('Missing required env var: AIRTABLE_API_KEY');
  if (!baseId) throw new Error('Missing required env var: AIRTABLE_BASE_ID');
  return { apiKey, baseId };
}

async function airtableRequest(path, options = {}) {
  const { apiKey } = getConfig();
  const res = await fetch(`${AIRTABLE_API_BASE}${path}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${apiKey}`,
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const message = (body && body.error && body.error.message) || res.statusText;
    throw new Error(`Airtable API error (${res.status}) on ${path}: ${message}`);
  }
  return body;
}

/**
 * Lists all records matching a filter, following pagination automatically.
 * @param {string} tableName
 * @param {{filterByFormula?: string, fields?: string[], sort?: {field:string,direction?:string}[], maxRecords?: number}} [opts]
 */
async function listRecords(tableName, opts = {}) {
  const { baseId } = getConfig();
  const { filterByFormula, fields, sort, maxRecords } = opts;
  const records = [];
  let offset;
  do {
    const params = new URLSearchParams();
    if (filterByFormula) params.set('filterByFormula', filterByFormula);
    if (maxRecords) params.set('maxRecords', String(maxRecords));
    if (fields) fields.forEach((f) => params.append('fields[]', f));
    if (sort) {
      sort.forEach((s, i) => {
        params.append(`sort[${i}][field]`, s.field);
        params.append(`sort[${i}][direction]`, s.direction || 'asc');
      });
    }
    if (offset) params.set('offset', offset);

    const path = `/${baseId}/${encodeURIComponent(tableName)}?${params.toString()}`;
    const body = await airtableRequest(path, { method: 'GET' });
    records.push(...body.records);
    offset = body.offset;
  } while (offset);
  return records;
}

async function getRecord(tableName, recordId) {
  const { baseId } = getConfig();
  return airtableRequest(`/${baseId}/${encodeURIComponent(tableName)}/${recordId}`, { method: 'GET' });
}

async function createRecord(tableName, fields) {
  const { baseId } = getConfig();
  return airtableRequest(`/${baseId}/${encodeURIComponent(tableName)}`, {
    method: 'POST',
    body: JSON.stringify({ fields }),
  });
}

async function updateRecord(tableName, recordId, fields) {
  const { baseId } = getConfig();
  return airtableRequest(`/${baseId}/${encodeURIComponent(tableName)}/${recordId}`, {
    method: 'PATCH',
    body: JSON.stringify({ fields }),
  });
}

/**
 * Today's queued leads per agent-a-requirements.md:
 * Email Contacts where Status = Active, Is Primary = true, Email Sent = false,
 * and linked Person's Batch matches today's active batch.
 *
 * @param {string} [activeBatchName] - primary-field value of today's active Batches record.
 */
async function getQueuedEmailContacts(activeBatchName) {
  const filterParts = [
    "{Status} = 'Active'",
    '{Is Primary} = TRUE()',
    '{Email Sent} = FALSE()',
  ];
  // TODO(Agent A/B): schema.md doesn't define a "Batch" lookup field on Email
  // Contacts — Batch lives on the linked Person record. This filter assumes a
  // lookup field named "Batch" is added to Email Contacts (same pattern as
  // the Name/Company/Designation merge-field lookups) so batch matching can
  // happen server-side. Until that lookup exists, pass no activeBatchName and
  // filter by batch in application code after fetching Person links.
  if (activeBatchName) {
    filterParts.push(`{Batch} = '${activeBatchName.replace(/'/g, "\\'")}'`);
  }
  const filterByFormula = `AND(${filterParts.join(', ')})`;
  return listRecords(TABLES.EMAIL_CONTACTS, { filterByFormula });
}

/**
 * Send Event Logging — api-contract.md section 4.
 * Writes back to the Email Contacts row after a successful (or mock) send.
 * @param {string} recordId - Email Contacts record ID
 * @param {{assignedInboxRecordId: string, trackingId: string, sentDate?: string}} result
 */
async function recordSendResult(recordId, { assignedInboxRecordId, trackingId, sentDate = new Date().toISOString() }) {
  return updateRecord(TABLES.EMAIL_CONTACTS, recordId, {
    'Email Sent': true,
    'Sent Date': sentDate,
    'Assigned Inbox': [assignedInboxRecordId],
    'Tracking ID': trackingId,
  });
}

module.exports = {
  TABLES,
  listRecords,
  getRecord,
  createRecord,
  updateRecord,
  getQueuedEmailContacts,
  recordSendResult,
};
