/**
 * Mock sender — simulates a send without calling any real API. Used whenever
 * inboxes.config.isMockMode() is true (zero inboxes configured, or MOCK_SEND=true).
 *
 * Same function signature as gmail-sender.js so gmail-send.js doesn't care
 * which one is active: sendEmail(inboxConfig, emailContact, renderedContent).
 *
 * inboxConfig here may be a real configured inbox (MOCK_SEND=true override)
 * or a synthetic placeholder gmail-send.js builds when zero inboxes are
 * configured at all — this module doesn't care which, it never touches
 * inboxConfig's credentials.
 */

const crypto = require('crypto');

/**
 * @param {{email: string}} inboxConfig
 * @param {object} emailContact - the Email Contacts record being "sent" to
 * @param {{subject: string, html: string, to: string, trackingId: string}} renderedContent
 * @returns {Promise<{trackingId: string, sentDate: string, providerMessageId: string}>}
 */
async function sendEmail(inboxConfig, emailContact, renderedContent) {
  const sentDate = new Date().toISOString();
  const providerMessageId = `mock-${crypto.randomUUID()}`;

  // eslint-disable-next-line no-console
  console.log(
    '[mock-sender] Simulated send\n' +
      `  From:        ${inboxConfig.email}\n` +
      `  To:          ${renderedContent.to}\n` +
      `  Subject:     ${renderedContent.subject}\n` +
      `  Tracking ID: ${renderedContent.trackingId}\n` +
      `  Sent Date:   ${sentDate}\n` +
      `  Body length: ${renderedContent.html.length} chars`,
  );

  return {
    trackingId: renderedContent.trackingId,
    sentDate,
    providerMessageId,
  };
}

module.exports = { sendEmail };
