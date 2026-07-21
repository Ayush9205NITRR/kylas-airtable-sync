/**
 * Real Gmail API sender. Only ever invoked when a matching GMAIL_INBOX_N_*
 * config exists for the chosen inbox (see inboxes.config.js / gmail-send.js) —
 * gmail-send.js is responsible for routing to this vs. mock-sender.js, this
 * module never checks mock mode itself.
 *
 * Same function signature as mock-sender.js so callers don't care which one
 * is active: sendEmail(inboxConfig, emailContact, renderedContent).
 *
 * Requires the googleapis package (not yet a dependency — add it when real
 * inbox credentials land, e.g. `npm install googleapis`).
 */

/**
 * @param {{email: string, clientId: string, clientSecret: string, refreshToken: string}} inboxConfig
 *   - one entry from inboxes.config.js's loadConfiguredInboxes()
 * @param {object} emailContact - the Email Contacts record being sent to (Airtable record shape)
 * @param {{subject: string, html: string, to: string}} renderedContent - output of the template renderer
 * @returns {Promise<{trackingId: string, sentDate: string, providerMessageId: string}>}
 */
async function sendEmail(inboxConfig, emailContact, renderedContent) {
  // Lazy require so environments without real credentials (and without the
  // googleapis dependency installed yet) never hit a module-load error —
  // this file is only imported, not necessarily executed, until an inbox is configured.
  const { google } = require('googleapis');

  const oauth2Client = new google.auth.OAuth2(inboxConfig.clientId, inboxConfig.clientSecret);
  oauth2Client.setCredentials({ refresh_token: inboxConfig.refreshToken });

  const gmail = google.gmail({ version: 'v1', auth: oauth2Client });

  const messageParts = [
    `From: ${inboxConfig.email}`,
    `To: ${renderedContent.to}`,
    'Content-Type: text/html; charset=utf-8',
    'MIME-Version: 1.0',
    `Subject: ${renderedContent.subject}`,
    '',
    renderedContent.html,
  ];
  const rawMessage = Buffer.from(messageParts.join('\n'))
    .toString('base64')
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '');

  const res = await gmail.users.messages.send({
    userId: 'me',
    requestBody: { raw: rawMessage },
  });

  return {
    trackingId: renderedContent.trackingId,
    sentDate: new Date().toISOString(),
    providerMessageId: res.data.id,
  };
}

module.exports = { sendEmail };
