- GLM workers now advertise a 128,000-token Pi context window and reserve
  32,768 tokens for native compaction, keeping large requests below the gateway
  payload ceiling.
