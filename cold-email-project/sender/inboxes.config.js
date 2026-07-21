/**
 * Plug-and-play inbox config loader.
 *
 * For each inbox N (starting at 1), reads:
 *   GMAIL_INBOX_N_CLIENT_ID
 *   GMAIL_INBOX_N_CLIENT_SECRET
 *   GMAIL_INBOX_N_REFRESH_TOKEN
 *   GMAIL_INBOX_N_EMAIL
 *
 * Auto-detects how many inboxes are configured by scanning upward from 1
 * until a fully-missing slot is found — adding a 4th inbox later is just
 * adding 4 more env vars (GMAIL_INBOX_4_*), no code change required.
 *
 * A partially-configured inbox (some but not all of the 4 vars set) is
 * treated as a config error and throws, rather than silently skipping it —
 * that's almost always a typo, not an intentional gap.
 *
 * MOCK_SEND=true forces mock mode even when real inbox credentials exist
 * (for safe testing later). With MOCK_SEND unset/false, mock mode is
 * whatever configuredInboxes.length === 0 implies.
 */

const REQUIRED_VARS = ['CLIENT_ID', 'CLIENT_SECRET', 'REFRESH_TOKEN', 'EMAIL'];

function readInboxSlot(index) {
  const prefix = `GMAIL_INBOX_${index}_`;
  const values = {};
  for (const key of REQUIRED_VARS) {
    values[key] = process.env[`${prefix}${key}`];
  }
  const present = REQUIRED_VARS.filter((key) => values[key]);
  if (present.length === 0) {
    return null; // fully absent — this is where the scan stops
  }
  if (present.length < REQUIRED_VARS.length) {
    const missing = REQUIRED_VARS.filter((key) => !values[key]).map((key) => `${prefix}${key}`);
    throw new Error(
      `inboxes.config: GMAIL_INBOX_${index}_* is partially configured — missing ${missing.join(', ')}`,
    );
  }
  return {
    index,
    email: values.EMAIL,
    clientId: values.CLIENT_ID,
    clientSecret: values.CLIENT_SECRET,
    refreshToken: values.REFRESH_TOKEN,
  };
}

/**
 * Scans GMAIL_INBOX_1_*, GMAIL_INBOX_2_*, ... until a fully-missing slot is
 * found. Returns the list of configured inboxes (possibly empty).
 */
function loadConfiguredInboxes() {
  const inboxes = [];
  let index = 1;
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const inbox = readInboxSlot(index);
    if (!inbox) break;
    inboxes.push(inbox);
    index += 1;
  }
  return inboxes;
}

/**
 * True if the sender should simulate sends instead of calling the real Gmail API:
 * either no inboxes are configured at all, or MOCK_SEND is explicitly set.
 */
function isMockMode(configuredInboxes = loadConfiguredInboxes()) {
  if (String(process.env.MOCK_SEND).toLowerCase() === 'true') return true;
  if (String(process.env.MOCK_SEND).toLowerCase() === 'false') return configuredInboxes.length === 0;
  return configuredInboxes.length === 0;
}

module.exports = {
  REQUIRED_VARS,
  loadConfiguredInboxes,
  isMockMode,
};
