// Pelago OS optional module fallbacks.
// Real modules (such as meetings-v2.js) override these hooks when loaded.
(function () {
  const noop = () => {};
  const hooks = ['meetingViewGuard', 'resetMeetingView', 'resetMeetingIdentity', 'openMeetingRoom',
    'openMeetingsV2', 'openMeetingV2', 'openMeetingSession', 'openRoundtable', 'openCodexMeter', 'leaveCodexMeter'];
  for (const name of hooks) {
    if (typeof window[name] !== 'function') {
      window[name] = name === 'meetingViewGuard' ? () => () => true : noop;
    }
  }
})();
