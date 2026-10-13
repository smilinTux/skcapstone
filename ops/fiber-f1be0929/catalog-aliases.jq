# Merge descriptors only. Credentials remain in the existing host catalog.
.providers.skgateway as $gateway
| if $gateway.baseUrl != "http://chiap01:18790/v1"
     or $gateway.api != "openai-completions"
     or ($gateway.apiKey | type) != "string"
     or ($gateway.apiKey | length) == 0
  then error("existing gateway catalog is not usable") else . end
| reduce ($aliases[0].providers | to_entries[]) as $entry (.;
    ($entry.value + {apiKey: $gateway.apiKey}) as $desired
    | if .providers[$entry.key] == null then .providers[$entry.key] = $desired
      elif .providers[$entry.key] == $desired then .
      else error("existing named alias conflicts") end)
