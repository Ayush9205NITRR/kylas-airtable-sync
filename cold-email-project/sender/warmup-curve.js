/**
 * Warm-up curve — reference constant from /docs/schema.md ("Warm-up Curve" table).
 * Per-inbox daily send cap, keyed by week number since warm-up start.
 */
const WARMUP_CURVE = [
  { week: 1, cap: 15 },
  { week: 2, cap: 25 },
  { week: 3, cap: 35 },
  { week: 4, cap: 45 },
  { week: 5, cap: 55 }, // week 5 and beyond
];

/**
 * Computes Current Daily Cap for an inbox given how many days it's been warming up.
 * @param {number} daysSinceWarmupStart - e.g. Inboxes."Days Since Warmup" (TODAY() - Warmup Start Date)
 * @returns {number} the daily send cap for that inbox today
 */
function getDailyCapForDaysSinceWarmup(daysSinceWarmupStart) {
  if (daysSinceWarmupStart == null || Number.isNaN(daysSinceWarmupStart)) {
    throw new Error('getDailyCapForDaysSinceWarmup: daysSinceWarmupStart is required');
  }
  const days = Math.max(0, daysSinceWarmupStart);
  const week = Math.floor(days / 7) + 1;
  const band = WARMUP_CURVE.find((b) => b.week === week) || WARMUP_CURVE[WARMUP_CURVE.length - 1];
  return band.cap;
}

/**
 * Convenience wrapper: computes the cap directly from a Warmup Start Date.
 * @param {string|Date} warmupStartDate
 * @param {Date} [now] - injectable for tests
 */
function getDailyCapForWarmupStartDate(warmupStartDate, now = new Date()) {
  const start = new Date(warmupStartDate);
  const msPerDay = 24 * 60 * 60 * 1000;
  const daysSinceWarmupStart = Math.floor((now.getTime() - start.getTime()) / msPerDay);
  return getDailyCapForDaysSinceWarmup(daysSinceWarmupStart);
}

module.exports = {
  WARMUP_CURVE,
  getDailyCapForDaysSinceWarmup,
  getDailyCapForWarmupStartDate,
};
