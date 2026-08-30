// Failure 1 -- Cache Penetration.
//
// Requests for ids that were never issued. Redis has never heard of them,
// Postgres has nothing to return, and there is nothing to cache afterwards --
// so every single request goes all the way to disk. The cache is not failing
// here. It is being walked past.
//
//   MODE=random   a different phantom id every time.        The real attack.
//   MODE=repeat   a small pool of phantom ids, reused.      The easy case.
//
// The two modes exist because they separate the defenses. Null caching fixes
// `repeat` and barely touches `random`; the Bloom filter fixes both.
import http from 'k6/http';
import { Counter } from 'k6/metrics';
import { line } from './summary.js';

const BASE = __ENV.BASE || 'http://app:8000';
const MODE = __ENV.MODE || 'random';
const POOL = parseInt(__ENV.POOL || '50', 10);
const LABEL = __ENV.LABEL || `penetration-${MODE}`;

// Ids well past the 100,000 that exist. Nothing here was ever a user.
const PHANTOM_LO = 1000000;
const PHANTOM_HI = 2000000;

const served = {
  MISS: new Counter('served_miss'),
  NEG_HIT: new Counter('served_neg_hit'),
  BLOOM_REJECT: new Counter('served_bloom_reject'),
  HIT: new Counter('served_hit'),
};

export const options = {
  scenarios: {
    attack: {
      executor: 'constant-vus',
      vus: parseInt(__ENV.VUS || '40', 10),
      duration: __ENV.DURATION || '30s',
    },
  },
  // A 404 is the correct answer to "give me user 1,481,922". Only a 5xx or a
  // dropped connection counts as failure.
  thresholds: { 'http_req_failed{expected_response:true}': ['rate<0.01'] },
};

export default function () {
  const id =
    MODE === 'repeat'
      ? PHANTOM_LO + Math.floor(Math.random() * POOL)
      : PHANTOM_LO + Math.floor(Math.random() * (PHANTOM_HI - PHANTOM_LO));

  const res = http.get(`${BASE}/api/users/${id}`, {
    responseCallback: http.expectedStatuses(200, 404),
  });

  const c = served[res.headers['X-Cache']];
  if (c) c.add(1);
}

export function handleSummary(data) {
  return {
    stdout: line(LABEL, data, { mode: MODE, pool: MODE === 'repeat' ? POOL : null }),
  };
}
