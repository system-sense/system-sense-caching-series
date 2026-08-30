// One summary format, shared by all three attacks.
//
// k6 prints its own table to stdout. This adds a single machine-readable line
// that scripts/capture-demo.sh lifts straight into capture/, so the numbers in
// the episode and the numbers on your screen come from the same run.

export function line(label, data, extra) {
  const m = data.metrics;
  const d = m.http_req_duration ? m.http_req_duration.values : {};
  const out = {
    attack: label,
    ...extra,
    requests: m.http_reqs ? m.http_reqs.values.count : 0,
    rps: m.http_reqs ? Math.round(m.http_reqs.values.rate * 10) / 10 : 0,
    failed_pct: m.http_req_failed
      ? Math.round(m.http_req_failed.values.rate * 1000) / 10
      : 0,
    // Requests the load generator wanted to send and could not, because every
    // VU was still waiting on the last one. Part of the incident, not noise.
    dropped_iterations: m.dropped_iterations ? m.dropped_iterations.values.count : 0,
    latency_ms: {
      min: round(d.min),
      med: round(d.med),
      avg: round(d.avg),
      p90: round(d['p(90)']),
      p95: round(d['p(95)']),
      p99: round(d['p(99)']),
      max: round(d.max),
    },
    served_by: {},
    latency_by_layer: {},
  };

  // Which layer answered, counted from the X-Cache header on every response.
  for (const name of Object.keys(m)) {
    if (name.startsWith('served_')) {
      out.served_by[name.slice(7)] = m[name].values.count;
    }
    // A run's overall median is meaningless when most requests are hits and
    // the story is in the misses. Keep them apart.
    if (name.startsWith('latency_') && name.endsWith('_ms')) {
      const v = m[name].values;
      out.latency_by_layer[name.slice(8, -3)] = {
        n: v.count,
        med: round(v.med),
        p95: round(v['p(95)']),
        p99: round(v['p(99)']),
        max: round(v.max),
      };
    }
  }

  return 'K6_SUMMARY ' + JSON.stringify(out) + '\n';
}

function round(v) {
  return v === undefined ? null : Math.round(v * 100) / 100;
}
