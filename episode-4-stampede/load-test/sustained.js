// The same hot key, but left running.
//
// A burst is one expiry. Real traffic keeps arriving, and the key keeps
// expiring -- every CACHE_TTL_SECONDS, for as long as anyone is reading it.
// Over a minute at a short TTL that is several stampedes, which is what makes
// the three defenses comparable on the one metric that matters to a user:
// how slow was the slowest request.
//
//   none     every expiry is a cliff
//   lock     one rebuild per expiry, and 199 readers asleep for the length of it
//   xfetch   the key is rebuilt before it expires, so there is no cliff to be
//            first over. Nobody ever waits.
import http from 'k6/http';
import { Counter, Trend } from 'k6/metrics';
import { line } from './summary.js';

const BASE = __ENV.BASE || 'http://app:8000';
const HOT = parseInt(__ENV.HOT || '42', 10);
const LABEL = __ENV.LABEL || 'sustained';

const served = {
  HIT: new Counter('served_hit'),
  MISS: new Counter('served_miss'),
  LEADER: new Counter('served_leader'),
  WAIT_HIT: new Counter('served_wait_hit'),
  WAIT_TIMEOUT: new Counter('served_wait_timeout'),
};

const latency = {
  HIT: new Trend('latency_hit_ms'),
  MISS: new Trend('latency_miss_ms'),
  LEADER: new Trend('latency_leader_ms'),
  WAIT_HIT: new Trend('latency_waithit_ms'),
  WAIT_TIMEOUT: new Trend('latency_waittimeout_ms'),
};

export const options = {
  summaryTrendStats: ['min', 'med', 'avg', 'p(90)', 'p(95)', 'p(99)', 'max'],
  scenarios: {
    traffic: {
      // An arrival rate, not a VU count: when a rebuild stalls the readers,
      // the queue has to be allowed to build. Closed-loop load would throttle
      // itself and quietly hide the incident.
      executor: 'constant-arrival-rate',
      rate: parseInt(__ENV.RPS || '2000', 10),
      timeUnit: '1s',
      duration: __ENV.DURATION || '60s',
      preAllocatedVUs: 300,
      maxVUs: parseInt(__ENV.MAX_VUS || '4000', 10),
    },
  },
};

export default function () {
  const res = http.get(`${BASE}/api/users/${HOT}`);
  const layer = res.headers['X-Cache'];
  const c = served[layer];
  if (c) c.add(1);
  const t = latency[layer];
  if (t) t.add(res.timings.duration);
}

export function handleSummary(data) {
  return { stdout: line(LABEL, data, { hot_key: `user:${HOT}` }) };
}
