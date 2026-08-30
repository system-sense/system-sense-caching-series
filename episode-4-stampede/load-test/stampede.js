// The boss fight, in one event.
//
// Two hundred virtual users, one iteration each, all released at the same
// instant against a key that has just expired. This is not a rate and not a
// ramp: it is the shape of a hot key going cold underneath live traffic, and
// it is the smallest test that can produce the number this episode is about.
//
//   STAMPEDE_DEFENSE=none    200 requests -> 200 identical aggregate queries
//   STAMPEDE_DEFENSE=lock    200 requests -> 1
//
// The two hundred readers are already reading the key before it dies, which is
// what a viral profile actually looks like: the connections exist, the traffic
// exists, and then the TTL runs out underneath it. Getting that order right
// matters more than it sounds -- opening two hundred fresh TCP connections at
// the moment of expiry takes this single-process app long enough that half the
// readers arrive after the first rebuild has already landed, and the stampede
// measures smaller than it is.
//
// So each VU makes one warm-up request (a hit), a separate one-shot scenario
// expires the key, and every VU then fires its measured request at the same
// wall-clock instant.
import http from 'k6/http';
import { sleep } from 'k6';
import { Counter, Trend } from 'k6/metrics';
import { line } from './summary.js';

const BASE = __ENV.BASE || 'http://app:8000';
const HOT = parseInt(__ENV.HOT || '42', 10);
const VUS = parseInt(__ENV.VUS || '200', 10);
const LABEL = __ENV.LABEL || 'stampede';
const START_DELAY_MS = parseInt(__ENV.START_DELAY_MS || '8000', 10);
const EXPIRE_LEAD_MS = parseInt(__ENV.EXPIRE_LEAD_MS || '500', 10);
const JSON_HEADERS = { headers: { 'content-type': 'application/json' } };

const served = {
  HIT: new Counter('served_hit'),            // beat everyone else to the cache
  MISS: new Counter('served_miss'),          // undefended: ran the query itself
  LEADER: new Counter('served_leader'),      // won the lock, rebuilt the key
  WAIT_HIT: new Counter('served_wait_hit'),  // lost the lock, read what it wrote
  WAIT_TIMEOUT: new Counter('served_wait_timeout'),
};

// Every reader, whichever layer answered it. k6's own http_req_duration also
// covers the single setup() call that expires the key, which is not a reader.
const readers = new Trend('latency_reader_ms');

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
    burst: {
      // Every VU does exactly one measured request, and they all do it at the
      // same instant. No arrival rate to smooth the edge off the event.
      executor: 'per-vu-iterations',
      vus: VUS,
      iterations: 1,
      maxDuration: __ENV.MAX_DURATION || '120s',
    },
    // One reader whose only job is to be the TTL running out.
    killer: {
      executor: 'per-vu-iterations',
      vus: 1,
      iterations: 1,
      exec: 'expire',
      maxDuration: __ENV.MAX_DURATION || '120s',
    },
  },
};

export function setup() {
  // One wall-clock instant for all of them. k6 starts virtual users a few
  // milliseconds apart, and a few milliseconds is long enough for an early one
  // to finish its rebuild and turn the later ones into hits.
  return { startAt: Date.now() + START_DELAY_MS };
}

// The expiry, half a second before the readers go. Nobody is reading the key in
// between, so this is the same event as a TTL reaching zero -- just at a second
// somebody chose.
export function expire(data) {
  const wait = (data.startAt - EXPIRE_LEAD_MS - Date.now()) / 1000;
  if (wait > 0) sleep(wait);
  http.post(`${BASE}/api/cache/expire`, JSON.stringify({ uid: HOT }), JSON_HEADERS);
}

export default function (data) {
  // Establish the connection and take a hit off the live key. This request is
  // not measured; it is this reader arriving before the trouble starts.
  http.get(`${BASE}/api/users/${HOT}`);

  const wait = (data.startAt - Date.now()) / 1000;
  if (wait > 0) sleep(wait);

  const res = http.get(`${BASE}/api/users/${HOT}`);
  readers.add(res.timings.duration);
  const layer = res.headers['X-Cache'];
  const c = served[layer];
  if (c) c.add(1);
  const t = latency[layer];
  if (t) t.add(res.timings.duration);
}

export function handleSummary(data) {
  return { stdout: line(LABEL, data, { hot_key: `user:${HOT}`, vus: VUS }) };
}
