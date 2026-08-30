// Not an attack -- a measurement.
//
// How much can each layer actually take? Every claim in this episode about the
// database "falling over" is relative to these two numbers, so both are
// measured rather than assumed.
//
//   MODE=db      the uncached endpoint, straight to Postgres, never Redis.
//   MODE=cache   the normal endpoint against warmed keys, so Redis answers.
//
// The gap between them is the episode: Redis is bored at a rate Postgres
// cannot survive.
import http from 'k6/http';
import { line } from './summary.js';

const BASE = __ENV.BASE || 'http://app:8000';
const KEYS = parseInt(__ENV.KEYS || '5000', 10);
const MODE = __ENV.MODE || 'db';
const PATH = MODE === 'cache' ? '' : '/uncached';

export const options = {
  scenarios: {
    ceiling: MODE === 'cache'
      ? {
          // Redis is nowhere near saturated, so a fixed VU count would measure
          // the load generator. Offer a rate instead and check it was met.
          executor: 'constant-arrival-rate',
          rate: parseInt(__ENV.RPS || '2500', 10),
          timeUnit: '1s',
          duration: __ENV.DURATION || '10s',
          preAllocatedVUs: 200,
          maxVUs: 1000,
        }
      : {
          executor: 'constant-vus',
          vus: parseInt(__ENV.VUS || '32', 10),
          duration: __ENV.DURATION || '15s',
        },
  },
};

export default function () {
  const id = 1 + Math.floor(Math.random() * KEYS);
  http.get(`${BASE}/api/users/${id}${PATH}`);
}

export function handleSummary(data) {
  return {
    stdout: line(MODE === 'cache' ? 'cache-ceiling' : 'db-ceiling', data, {
      note: MODE === 'cache'
        ? 'warmed keys, answered by Redis'
        : 'uncached reads, never hits Redis',
    }),
  };
}
