// Pelago OS v0.1 ships without the meeting room and the quota meter.
// app.js still calls these hooks; keep them as no-ops until those modules return.
(function () {
  const noop = () => {};
  const hooks = ['resetMeetingView', 'resetMeetingIdentity', 'openMeetingRoom', 'openMeetingSession',
    'openRoundtable', 'openCodexMeter', 'leaveCodexMeter'];
  for (const name of hooks) {
    if (typeof window[name] !== 'function') window[name] = noop;
  }
})();
