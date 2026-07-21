/**
 * Inbox rotation — selects which inbox to send from.
 *
 * getInboxHealth() and getAvailableInboxes() match the signatures fixed in
 * /docs/api-contract.md section 3, owned by Agent B's /data/health-score.js.
 *
 * TODO(Agent B): Both functions below are STUBS. They read Health
 * Score / Status / Current Daily Cap / Emails Sent Today directly off the
 * Inboxes table's existing formula/number fields instead of recomputing
 * anything. Once /data/health-score.js exists on feature/data-reporting,
 * swap these two function bodies for `require('../data/health-score')` and
 * delete the stub logic — the call sites in this file (selectInboxForSend)
 * should not need to change since the return shape is already contract-compliant.
 */

const { TABLES, listRecords } = require('./airtable-client');

function mapInboxRecord(record) {
  const f = record.fields || {};
  return {
    // Not part of the documented api-contract.md return shape, but included
    // because gmail-send.js needs the Inboxes record ID to write the
    // "Assigned Inbox" link field (see airtable-client.recordSendResult).
    recordId: record.id,
    inboxEmail: f['Inbox Email'],
    healthScore: f['Health Score'],
    status: f['Status'],
    currentDailyCap: f['Current Daily Cap'],
    sentToday: f['Emails Sent Today'] || 0,
  };
}

/** @returns {Promise<{inboxEmail, healthScore, status, currentDailyCap, sentToday}|null>} */
async function getInboxHealth(inboxEmail) {
  const records = await listRecords(TABLES.INBOXES, {
    filterByFormula: `{Inbox Email} = '${inboxEmail.replace(/'/g, "\\'")}'`,
    maxRecords: 1,
  });
  if (records.length === 0) return null;
  return mapInboxRecord(records[0]);
}

/**
 * Active inboxes, sorted by lowest Emails Sent Today first. Never includes
 * Paused (or Warming) inboxes — status filter is a hard 'Active' match.
 * @returns {Promise<Array<{inboxEmail, healthScore, status, currentDailyCap, sentToday}>>}
 */
async function getAvailableInboxes() {
  const records = await listRecords(TABLES.INBOXES, {
    filterByFormula: "{Status} = 'Active'",
  });
  return records
    .map(mapInboxRecord)
    .sort((a, b) => (a.sentToday || 0) - (b.sentToday || 0));
}

/**
 * Picks the single best inbox for the next send: lowest Emails Sent Today
 * among Active inboxes that haven't hit their Current Daily Cap yet.
 * Logs a warning and returns null if nothing is available.
 * @returns {Promise<object|null>}
 */
async function selectInboxForSend() {
  const available = await getAvailableInboxes();
  const withCapacity = available.filter(
    (inbox) => (inbox.sentToday || 0) < (inbox.currentDailyCap ?? Infinity),
  );
  if (withCapacity.length === 0) {
    console.warn(
      '[rotation] No inbox available — either no inbox is Active, or every Active inbox has hit its Current Daily Cap.',
    );
    return null;
  }
  return withCapacity[0]; // getAvailableInboxes() already sorted by lowest sentToday
}

module.exports = {
  getInboxHealth,
  getAvailableInboxes,
  selectInboxForSend,
};
