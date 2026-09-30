# Exact model-local settings only. The existing installer supplies lock/backup/CAS.
def route($provider; $model):
  .providers[$provider] as $p
  | [$p.models[] | select(.id == $model)] as $matches
  | if $p.baseUrl != "http://chiap01:18790/v1"
       or $p.api != "openai-completions"
       or ($p.apiKey | type) != "string" or ($p.apiKey | length) == 0
       or ($matches | length) != 1 or $matches[0].reasoning != true
    then error("required gateway route missing or conflicting") else $matches[0] end;
route("skgw-deepseek"; "deepseek-flash") as $ds
| route("skgw-zai"; "sk-zai-m") as $glm
| (.providers["skgw-deepseek"].compat + ($ds.compat // {})) as $dc
| (.providers["skgw-zai"].compat + ($glm.compat // {})) as $gc
| if (($dc.thinkingFormat // "deepseek") != "deepseek")
     or (($ds.thinkingLevelMap // {}) | has("off"))
     or $gc.supportsReasoningEffort != true
     or (($gc.thinkingFormat // "openai") != "openai")
     or (($glm.thinkingLevelMap.off // "low") != "low")
  then error("existing thinking intent conflicts") else . end
| (.providers["skgw-deepseek"].models[] | select(.id == "deepseek-flash")
    | .compat.thinkingFormat) = "deepseek"
| (.providers["skgw-zai"].models[] | select(.id == "sk-zai-m")
    | .thinkingLevelMap.off) = "low"
