#!/usr/bin/env node
/**
 * Main orchestrator — run daily by /.github/workflows/send-daily.yml.
 *
 * For each queued Email Contact:
 *   1. picks an available inbox via rotation.js (respecting Current Daily Cap)
 *   2. renders the assigned Email Template with merge fields
 *   3. generates a Tracking ID, embeds the pixel + wraps links
 *   4. sends via gmail-sender.js (real) or mock-sender.js (simulated) —
 *      chosen per-inbox based on inboxes.config.js
 *   5. writes the Send Event Logging fields back to Airtable (api-contract.md §4)
 *   6. increments the inbox's Emails Sent Today so rotation stays accurate
 *      for the rest of this run and subsequent runs today
 *
 * Env vars used directly by this file:
 *   TRACKING_BASE_URL - base URL of the deployed tracking worker, e.g.
 *                        https://track.yourdomain.workers.dev
 *                        (tracking/*-worker.js aren't deployed yet — falls
 *                        back to a placeholder and logs a warning so mock
 *                        runs still work end-to-end)
 *   MOCK_SEND, GMAIL_INBOX_N_* - see inboxes.config.js
 *   AIRTABLE_API_KEY, AIRTABLE_BASE_ID - see airtable-client.js
 */

const crypto = require('crypto');
const { TABLES, getRecord, updateRecord, getQueuedEmailContacts, recordSendResult } = require('./airtable-client');
const { loadConfiguredInboxes, isMockMode } = require('./inboxes.config');
const { selectInboxForSend } = require('./rotation');
const gmailSender = require('./gmail-sender');
const mockSender = require('./mock-sender');

const TRACKING_BASE_URL = process.env.TRACKING_BASE_URL || 'https://track.example.workers.dev';
if (!process.env.TRACKING_BASE_URL) {
  console.warn(
    `[gmail-send] TRACKING_BASE_URL not set — using placeholder "${TRACKING_BASE_URL}". ` +
      'Pixel/click URLs embedded in sent emails will not resolve until the tracking worker is deployed and this is set.',
  );
}

const URL_PATTERN = /https?:\/\/[^\s"'<>]+/g;

/**
 * Assumption (schema.md gap, same as the Batch TODO in airtable-client.js):
 * merge fields are pulled from lookup fields on Email Contacts named exactly
 * like the People fields they mirror — Name, Company, Designation. Neither
 * schema.md nor api-contract.md names these lookup fields explicitly beyond
 * the illustrative {FirstName}/{Company}/{Designation} example, so this reads
 * emailContact.fields['Name' | 'Company' | 'Designation'] directly.
 */
function renderTemplate(template, emailContact, trackingId) {
  const mergeContext = {
    Name: emailContact.fields['Name'],
    Company: emailContact.fields['Company'],
    Designation: emailContact.fields['Designation'],
  };

  const fillMergeFields = (text) =>
    (text || '').replace(/\{(\w+)\}/g, (match, key) => {
      if (mergeContext[key] === undefined) {
        console.warn(`[gmail-send] Template references {${key}} but Email Contact has no such lookup field — leaving as-is.`);
        return match;
      }
      return mergeContext[key];
    });

  const subject = fillMergeFields(template.fields['Subject Variant']);
  let body = fillMergeFields(template.fields['Body Variant']);

  // Wrap every link through /click/{trackingId}?url=...
  body = body.replace(URL_PATTERN, (url) => `${TRACKING_BASE_URL}/click/${trackingId}?url=${encodeURIComponent(url)}`);

  // Embed the tracking pixel per api-contract.md section 1.
  body += `\n<img src="${TRACKING_BASE_URL}/pixel/${trackingId}.gif" width="1" height="1" alt="" style="display:none" />`;

  return { subject, html: body, to: emailContact.fields['Email'], trackingId };
}

async function loadTemplateFor(emailContact) {
  const templateLinks = emailContact.fields['Template Used'];
  if (!templateLinks || templateLinks.length === 0) {
    return null;
  }
  return getRecord(TABLES.EMAIL_TEMPLATES, templateLinks[0]);
}

/** Picks gmail-sender.js vs mock-sender.js for one specific inbox. */
function resolveSenderFor(inbox, configuredInboxes, globalMock) {
  const matchingConfig = configuredInboxes.find((c) => c.email === inbox.inboxEmail);
  if (globalMock || !matchingConfig) {
    if (!globalMock && !matchingConfig) {
      console.warn(
        `[gmail-send] Inbox ${inbox.inboxEmail} is Active in Airtable but has no matching GMAIL_INBOX_N_* credentials — falling back to mock for this send.`,
      );
    }
    return { sender: mockSender, inboxConfig: matchingConfig || { email: inbox.inboxEmail } };
  }
  return { sender: gmailSender, inboxConfig: matchingConfig };
}

async function run() {
  const configuredInboxes = loadConfiguredInboxes();
  const globalMock = isMockMode(configuredInboxes);
  console.log(
    `[gmail-send] Starting run — ${configuredInboxes.length} inbox(es) configured, mode: ${globalMock ? 'MOCK' : 'LIVE'}.`,
  );

  // TODO(Agent A/B): no Batches table is defined in schema.md's 5 tables, so
  // "today's active batch" can't be resolved yet — running unfiltered by
  // batch until that's designed. See the same TODO in airtable-client.js.
  const queued = await getQueuedEmailContacts();

  const seenPersonIds = new Set();
  const toSend = [];
  for (const record of queued) {
    const personLink = record.fields['Person'];
    const personId = Array.isArray(personLink) ? personLink[0] : personLink;
    if (!personId) {
      console.warn(`[gmail-send] Skipping Email Contact ${record.id} — no linked Person.`);
      continue;
    }
    if (seenPersonIds.has(personId)) {
      console.warn(`[gmail-send] Skipping Email Contact ${record.id} — Person ${personId} already queued in this run (Is Primary enforcement should prevent this).`);
      continue;
    }
    seenPersonIds.add(personId);
    toSend.push(record);
  }

  console.log(`[gmail-send] ${toSend.length} Email Contact(s) queued to send (deduped by Person).`);

  const results = [];
  for (const emailContact of toSend) {
    const inbox = await selectInboxForSend();
    if (!inbox) {
      console.warn('[gmail-send] Stopping run — no inbox has remaining capacity.');
      break;
    }

    const template = await loadTemplateFor(emailContact);
    if (!template) {
      console.warn(`[gmail-send] Skipping Email Contact ${emailContact.id} — no Template Used assigned.`);
      continue;
    }

    const trackingId = crypto.randomUUID();
    const renderedContent = renderTemplate(template, emailContact, trackingId);

    const { sender, inboxConfig } = resolveSenderFor(inbox, configuredInboxes, globalMock);

    const sendResult = await sender.sendEmail(inboxConfig, emailContact, renderedContent);

    await recordSendResult(emailContact.id, {
      assignedInboxRecordId: inbox.recordId,
      trackingId: sendResult.trackingId,
      sentDate: sendResult.sentDate,
    });

    // Keep rotation accurate for the rest of this run (and later runs today).
    await updateRecord(TABLES.INBOXES, inbox.recordId, {
      'Emails Sent Today': (inbox.sentToday || 0) + 1,
    });

    console.log(`[gmail-send] Sent Email Contact ${emailContact.id} (${renderedContent.to}) via ${inbox.inboxEmail}, tracking ID ${trackingId}.`);
    results.push({ emailContactId: emailContact.id, inboxEmail: inbox.inboxEmail, trackingId });
  }

  console.log(`[gmail-send] Run complete — ${results.length} email(s) sent.`);
  return results;
}

if (require.main === module) {
  run()
    .then(() => process.exit(0))
    .catch((err) => {
      console.error('[gmail-send] Fatal error:', err);
      process.exit(1);
    });
}

module.exports = { run, renderTemplate };
