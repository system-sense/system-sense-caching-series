# System Sense — Caching Mini-Series

Runnable blueprints for a four-part series on caching. Every episode is a
self-contained folder that starts with one command and *shows you the numbers*
rather than asserting them.

Each episode ends by opening the wound the next one closes.

```
[ Episode 1: Fundamentals ]
        │  "Caching makes things fast"
        └── The Librarian's Desk (RAM vs Disk, Cache-Aside)
        ▼
[ Episode 2: Mutation & Consistency ]
        │  "...but what happens when data changes?"
        └── The Split-Brain Balance (Write-Through vs Write-Behind vs Invalidation)
        ▼
[ Episode 3: Traffic Disasters ]
        │  "...and what happens under hostile load?"
        └── The Triple Threat (Penetration, Avalanche, Breakdown)
        ▼
[ Episode 4: The Boss Fight ]
           "...here is how you survive the worst of them."
        └── Cache Stampede & Distributed Mutex (Redlock, Single-Flight, XFetch)
```

| Episode | Folder | Thesis |
| --- | --- | --- |
| 1. The Basics | [`episode-1-basics/`](episode-1-basics/) | Cache-Aside turns an expensive profile read into a sub-millisecond memory read. |
| 2. The Invalidation Nightmare | `episode-2-invalidation/` | Dual writes race; **deleting** the key beats **updating** the key. |
| 3. The 3 Classic Failures | `episode-3-failures/` | Penetration, Avalanche and Breakdown are three *different* bugs with three *different* fixes. |
| 4. The Boss Fight | `episode-4-stampede/` | One lock holder regenerates; the other 199 requests wait and read from cache. |

## Stack

Python + FastAPI, PostgreSQL 16 and Redis 7, identical across all four episodes
so you only learn the plumbing once. Episodes 3 and 4 add a k6 load generator.

## Requirements

Docker with Compose. That is the whole list — no local Python, no cloud
credentials, no manual seeding.

## License

MIT. Use it, fork it, break it on purpose.
