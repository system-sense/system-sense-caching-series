// Failure 3 -- Cache Breakdown. The hot key.
//
// One key. Every request in this test asks for the same user, which is what a
// viral profile looks like from the database's point of view. While the key is
// cached this is the cheapest traffic in the world: Redis answers all of it.
//
// Then the key expires. Not many keys -- one. Every request in flight at that
// instant misses, and every one of them runs the same expensive aggregate,
// because nothing in Cache-Aside says "someone else is already fetching this".
//
// This episode does not fix it. Episode 4 does.
import http from 'k6/http';
import { Counter, Trend } from 'k6/metrics';
import { line } from './summary.js';

const BASE = __ENV.BASE || 'http://app:8000';
const HOT = parseInt(__ENV.HOT || '42', 10);
const LABEL = __ENV.LABEL || 'breakdown';

const served = {
  HIT: new Counter('served_hit'),
  MISS: new Counter('served_miss'),
};

const latency = {
  HIT: new Trend('latency_hit_ms'),
  MISS: new Trend('latency_miss_ms'),
};

export const options = {
  scenarios: {
    hot_key: {
      executor: 'constant-arrival-rate',
      rate: parseInt(__ENV.RPS || '300', 10),
      timeUnit: '1s',
      duration: __ENV.DURATION || '30s',
      preAllocatedVUs: 200,
      maxVUs: parseInt(__ENV.MAX_VUS || '1000', 10),
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
