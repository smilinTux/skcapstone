- Route preflight results now expire when the gateway capacity revision changes,
  so a transient failure cannot keep later picks on a refreshed route snapshot
  blocked for the rest of that cycle.
