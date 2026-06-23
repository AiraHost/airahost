# Implementation Prompt: Advanced Speed Optimization for Scraping Pipeline

**Context:**
While the scraping logic correctly identifies comparable listings and handles complex constraints (exact capacity, dynamic fallback, ghost pricing), the overall execution time remains a critical bottleneck. Your singular goal for this task is to ruthlessly optimize the fetching and parsing process to achieve the maximum possible speed, strictly ensuring the entire pipeline runs well under the 45-second limit.

**Your Task:**

You are fully authorized to overhaul the network, concurrency, and architecture layers of the scraping pipeline. Below is a list of required strategies you must evaluate and implement. However, you are **not limited** to this list. You are highly encouraged to explore the broader solution space and implement any alternative optimization techniques that yield significant performance gains.

### 1. Concurrent and Asynchronous Network I/O
- **Parallel Fetching:** Ensure all independent network requests (e.g., fallback PDP fetching, date-range price queries) are executed concurrently using `asyncio.gather` or thread pools. Eradicate any sequential `for` loops making network calls.
- **Connection Pooling:** Ensure HTTP sessions use connection pooling and Keep-Alive (e.g., `aiohttp.ClientSession` or `httpx.AsyncClient` if using Python). Avoid the overhead of establishing new TCP/TLS handshakes for every request.

### 2. Bypassing the Browser (API-First Approach)
- **Direct GraphQL/REST Calls:** Launching and orchestrating headless browsers (e.g., Playwright/Puppeteer) is inherently slow. Prioritize reverse-engineering and directly calling the internal APIs (GraphQL/REST) used by the platform.
- **Browser Context Reuse:** If falling back to a headless browser is absolutely unavoidable for certain tokens or endpoints, ensure browser instances and contexts are kept warm and reused across requests rather than launching a new browser per query.

### 3. Payload and Query Optimization
- **GraphQL Field Pruning:** If querying GraphQL endpoints, strip the payload down to the absolute bare minimum fields required (e.g., capacity, price, availability). Do not request heavy nested objects (reviews, host details) unless strictly necessary.
- **Batched Endpoints:** Investigate if the platform supports batched queries (e.g., fetching pricing for 10 listing IDs in a single API request instead of 10 separate requests).

### 4. Advanced Caching Strategies
- **Aggressive Memoization:** Cache all static or semi-static listing data (capacity, exact location, amenities) locally or in the database. Never fetch a listing's detail page just to verify static data if it has been verified before.
- **Pre-computation:** If possible, pre-fetch or asynchronously update the fixed comparable pool in the background (outside of the 45-second user-facing request window).

### 5. Open Exploration (Carte Blanche)
- You have the freedom to explore alternative frameworks, optimize JSON parsing libraries (e.g., using `orjson`), or restructure the entire scraping architecture. 
- If you discover a faster path that bypasses current structural limitations without compromising the data integrity requirements (like exact capacity matching), you are authorized to implement it.

**Success Criteria:**
- The time spent fetching and validating network resources must drop drastically. 
- The entire 7-day report generation (including any dynamic fallback queries) must confidently execute in under 45 seconds. 
- Please document which speed optimizations yielded the highest time savings in your final report.
