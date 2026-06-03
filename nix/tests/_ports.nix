# Shared port assignments for the agent-mcp VM tests.
#
# Held in one place so the multi-tenant + single-tenant scaffolds
# don't drift apart and so the fake-openai sidecar's hardcoded
# 11434 has a documented sibling.
{
  routerPort = 1337;
  fakeOpenAIPort = 11434;
}
