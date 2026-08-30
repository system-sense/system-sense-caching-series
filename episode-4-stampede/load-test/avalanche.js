// Failure 2 -- Cache Avalanche.
//
// Steady, ordinary traffic across the keys a batch job warmed. Nothing about
// this load is hostile: same rate before, during and after. The only thing
// that changes is that at one instant every key the job wrote reaches the end
// of the identical TTL it was given, and the shape of the damage is entirely
// decided by whether that TTL had jitter on it.
import http from 'k6/http';
import { Counter, Trend } from 'k6/metrics';
import { line } from './summary.js';

const BASE = __ENV.BASE || 'http://app:8000';
const KEYS = parseInt(__ENV.KEYS || '5000', 10);   // the warmed set: ids 1..KEYS
const LABEL = __ENV.LABEL || 'avalanche';

const served = {
  HIT: new Counter('served_hit'),
  MISS: new Counter('served_miss'),
  NEG_HIT: new Counter('served_neg_hit'),
  BLOOM_REJECT: new Counter('served_bloom_reject'),
};

const latency = {
  HIT: new Trend('latency_hit_ms'),
  MISS: new Trend('latency_miss_ms'),
};

export const options = {
  scenarios: {
    traffic: {
      executor: 'constant-arrival-rate',
      // A fixed arrival rate, not a fixed VU count: when the database stalls,
      // the queue must be allowed to build. Closed-loop load would quietly
      // throttle itself and hide the incident.
      rate: parseInt(__ENV.RPS || '200', 10),
      timeUnit: '1s',
      duration: __ENV.DURATION || '60s',
      preAllocatedVUs: 200,
      maxVUs: parseInt(__ENV.MAX_VUS || '2000', 10),
    },
  },
};

export default function () {
  const id = 1 + Math.floor(Math.random() * KEYS);
  const res = http.get(`${BASE}/api/users/${id}`);
  const layer = res.headers['X-Cache'];
  const c = served[layer];
  if (c) c.add(1);
  const t = latency[layer];
  if (t) t.add(res.timings.duration);
}

export function handleSummary(data) {
  return { stdout: line(LABEL, data, { warmed_keys: KEYS }) };
}
