Fleet Pi workers retry a gateway HTTP 429 (pool or queue full) with bounded backoff and a capped Retry-After, instead of ending the session on the first refusal.
